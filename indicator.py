# REVIEW GUIDE - comments explain the current implementation, not validated alpha.
# Start with run_pipeline() for the data flow, then evidence_classification() for
# the conclusion. Read each analytics function to challenge the choices below.
#
# DATA TYPES: a DataFrame is a table; a Series is one aligned column; a dict is a
# named result object; a list holds records. None/NaN means unknown, not zero.
# A Boolean mask chooses rows. .loc applies it; .copy keeps edits independent.
# pd.to_datetime parses dates; errors="coerce" makes invalid dates unknown.
# .eq/.gt/.le mean equal/greater-than/less-than-or-equal; & combines row tests.
# .nunique counts distinct nonmissing values, not independent economic actors.
#
# REVIEW ASSUMPTIONS THAT AFFECT THE ANALYSIS:
# 1. COIN is fixed. Windows are consecutive calendar quarter ends: start excluded,
#    end included. as_of is a filing-date cutoff, not an acceptance timestamp.
# 2. Default quarter selection uses a 45-calendar-day reporting allowance.
# 3. P/S totals include private trades, direct/indirect ownership and potentially
#    multiple share classes; overlapping reporting owners are not reconciled.
# 4. Missing transaction values can be derived from reported shares times price;
#    missing values are not inserted as zero into the usable net signal.
# 5. Form 3 reconciliation requires one exact direct-account baseline and an
#    unambiguous ledger. It does not reconstruct every account or corporate action.
# 6. Cluster default = 3 distinct owner CIKs in 30 calendar days. This is configurable
#    descriptive screening, not a calibrated statistical significance threshold.
# 7. Calendar-month recurrence uses three prior years of observed purchases;
#    it is not the research paper's full routine/opportunistic methodology.
# 8. abs(price return) < 2% is the flat band; exactly +/-2% is directional.
#    Net P/S dollar sign and selected-manager share-change sign choose the state.
#    No minimum dollar size, role weighting, confidence score or validated forecast.
# 9. Seven-angle purchase details and Form 3 are context, NOT weighted classifier
#    inputs. Profile-based advice and the LLM/chatbot are not implemented yet.
# 10. Cached observations retain their original refresh times. A stale snapshot
#     may still yield a state, but is flagged; stale does not force Insufficient data.
# 11. Known subtotals, zero reported positions and unavailable data are different.
#     Read each status and coverage note before interpreting a number.
#
# COMMENT LABELS: REVIEW = methodological choice; LIMITATION = unresolved issue;
# DATA FLOW = inputs/outputs between functions; MECHANICS = implementation detail.

"""Single-ticker ownership prototype. Run with --help for configuration.

Requires the existing pandas/requests environment and MASSIVE_API_KEY.
No synthetic observations, chatbot, or investment recommendations are used.
"""
# MECHANICS: argparse reads optional terminal flags; json handles saved/API/report objects.
import argparse
import json
# MECHANICS: math validates numbers; os reads the key and replaces cache files atomically.
import math
import os
# MECHANICS: sys sends progress to stderr; time spaces requests without using calendar time for elapsed waits.
import sys
import time
# MECHANICS: hash query settings into cache filenames; tempfile supports safe snapshot replacement.
import hashlib
import tempfile
# MECHANICS: Path handles local file paths. Date/time helpers define observation and refresh windows.
from pathlib import Path
from datetime import date, timedelta, datetime, timezone
# MECHANICS: Retry-After can be a date string. URL helpers validate/clean pagination links.
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

# MECHANICS: pandas supplies tables/date operations; requests performs authenticated API HTTP calls.
import pandas as pd
import requests

# MECHANICS: endpoint constants identify the datasets; no API key is hard-coded here.
BASE = "https://api.massive.com"
FORM3_URL = BASE + "/stocks/filings/vX/form-3"
FORM4_URL = BASE + "/stocks/filings/vX/form-4"
FORM13F_URL = BASE + "/stocks/filings/vX/13-F"
# MECHANICS: main creates the shared client; the module-level None permits importing analytics without API access.
DATA_CLIENT = None
# REVIEW: this prototype is explicitly Coinbase-only. Ticker COIN was also used
# by another issuer; use Coinbase's CIK observed in its returned filings to scope
# every ownership calculation. A future multi-ticker tool needs date-aware mapping.
COIN_ISSUER_CIK = "0001679788"


class MassiveClient:
    """Complete snapshots only; paced requests; bounded retries; explicit freshness.

    Filings refresh the last seven filing-date days and replace that entire tail.
    This preserves legitimate equal-looking transaction rows rather than blindly
    deduplicating them. A weekly full refresh catches older provider corrections.
    Amendments remain separate records for the existing analysis to flag.
    """

    # MECHANICS: construct one shared request/cache manager. self holds its state across endpoint calls.
    def __init__(self, cache_dir=None, offline=False, refresh=False, full_refresh=False,
                 min_interval=13.0, retries=3, debug=False):
        # MECHANICS: None disables disk storage; otherwise this Path is the snapshot directory.
        self.cache_dir = Path(cache_dir) if cache_dir else None
        # MECHANICS: offline forbids API calls; refresh bypasses cache age; full_refresh bypasses incremental reuse.
        self.offline, self.refresh, self.full_refresh = offline, refresh, full_refresh
        # REVIEW: default 13-second spacing and three retries are operational settings, not analytical weights.
        self.min_interval, self.retries = min_interval, retries
        # MECHANICS: no previous request exists at startup; pacing is per process, not coordinated across programs.
        self.last_request = None
        # MECHANICS: mutable list of retrieval metadata later exposed under report[data_freshness].
        self.events = []
        # MECHANICS: normal reports hide technical progress; --debug sends it to stderr.
        self.debug = debug

    def log(self, message):
        if self.debug:
            print(message, file=sys.stderr)

    # MECHANICS: use timezone-aware UTC for cache ages; this is download time, not filing acceptance time.
    @staticmethod
    def utc_now():
        return datetime.now(timezone.utc)

    # MECHANICS: validate the API host and strip credential-like query fields before using pagination.
    @staticmethod
    def safe_url(url):
        # Pagination can include an API key. Use bearer auth and never persist it.
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.massive.com":
            raise ValueError("Unexpected pagination host")
        query = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in ("apikey", "api_key", "token")]
        return urlunparse(parsed._replace(query=urlencode(query)))

    # DATA FLOW: one HTTP page in, decoded response dict out; retry selected transient failures only.
    def request(self, url, params):
        # MECHANICS: read the existing terminal environment key; do not print or write it into the cache.
        key = os.getenv("MASSIVE_API_KEY")
        if not key:
            raise ValueError("MASSIVE_API_KEY environment variable is missing")
        # MECHANICS: includes the initial attempt plus up to retries additional attempts.
        for attempt_number in range(self.retries+1):
            if self.last_request is not None:
                # MECHANICS: wait only the remaining interval since the previous request started.
                time.sleep(max(0, self.min_interval-(time.monotonic()-self.last_request)))
            self.last_request = time.monotonic()
            try:
                # MECHANICS: send Bearer authentication, query parameters and a 30-second timeout; do not follow redirects.
                response = requests.get(url, headers={"Authorization": f"Bearer {key}"},
                                        params=params, timeout=30, allow_redirects=False)
            except requests.RequestException:
                if attempt_number == self.retries:
                    raise ValueError("Massive network request failed after retries") from None
                delay = 2**(attempt_number+1)
            else:
                if response.status_code == 200:
                    # Reliability: reject malformed JSON without displaying arbitrary
                    # server bodies or treating them as an empty successful dataset.
                    try:
                        data = response.json()
                    except ValueError:
                        raise ValueError("Massive returned invalid JSON; snapshot was not updated") from None
                    if not isinstance(data, dict) or data.get("status") not in ("OK", "DELAYED"):
                        raise ValueError("Massive returned an unsuccessful response")
                    return data
                if response.status_code not in (429, 500, 502, 503, 504) or attempt_number == self.retries:
                    raise ValueError(f"Massive HTTP {response.status_code}; request did not complete")
                # MECHANICS: a 429 waits 65/130/195 seconds; selected server errors use 2/4/8 seconds.
                delay = 65*(attempt_number+1) if response.status_code == 429 else 2**(attempt_number+1)
                # MECHANICS: honor a server-requested longer delay, subject to the five-minute bound below.
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        requested = float(retry_after)
                    except (TypeError, ValueError):
                        try:
                            requested = (parsedate_to_datetime(retry_after)-self.utc_now()).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            requested = 0
                    if math.isfinite(requested):
                        delay = max(delay, requested)
                # MECHANICS: abort this refresh instead of hanging indefinitely; a saved complete snapshot can still be used.
                if delay > 300:
                    raise ValueError("Server asks for a retry after more than five minutes; try a later refresh")
            self.log(f"Waiting {delay:.0f}s before retry {attempt_number+1}/{self.retries}...")
            time.sleep(delay)
        raise ValueError("Request retry limit reached")

    # DATA FLOW: collect every page into a list of row dicts; any page failure discards this attempt.
    def download(self, url, params):
        # MECHANICS: rows accumulates records; seen detects a pagination loop rather than silently repeating data.
        rows, seen = [], set()
        while url:
            url = self.safe_url(url)
            if url in seen:
                raise ValueError("Repeated pagination URL; results are incomplete")
            seen.add(url)
            self.log(f"Fetching {urlparse(url).path}, page {len(seen)}...")
            data = self.request(url, params)
            page = data.get("results", [])
            if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
                raise ValueError("Unexpected results schema")
            # MECHANICS: append page rows exactly as returned; equal-looking rows are not automatically duplicates.
            rows.extend(page)
            # MECHANICS: the next_url carries its own cursor; do not resend first-page filters with that cursor.
            url, params = data.get("next_url"), None
        return rows

    # MECHANICS: read a cache file and validate its format, query identity and timestamps; no API calls.
    def load(self, path, identity):
        if path is None:
            return None
        try:
            # MECHANICS: parse locally saved data, then verify identity/version before allowing reuse.
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("version") != 1 or saved.get("identity") != identity:
                return None
            if not isinstance(saved["rows"], list) or any(not isinstance(r, dict) for r in saved["rows"]):
                return None
            for field in ("refreshed_at", "full_refreshed_at"):
                stamp = datetime.fromisoformat(saved[field])
                if stamp.tzinfo is None or stamp > self.utc_now():
                    return None
            if "/filings/" in identity["url"]:
                date.fromisoformat(saved["through"])
            return saved
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            self.log("Cache unavailable or invalid; a complete download is needed.")
            return None

    # MECHANICS: atomically store a complete snapshot; a half-written file must not replace the previous one.
    def save(self, path, payload):
        if path is None:
            return
        # Replace only after all pages and serialization succeed; preserve last good file.
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, allow_nan=False)
            # MECHANICS: replace the destination only after writing a complete JSON file in the same directory.
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                # MECHANICS: remove only this temporary file if it remains; do not delete the prior cache.
                temporary.unlink()

    # DATA FLOW: choose cache/full download/incremental refresh; return a DataFrame and record freshness separately.
    def get(self, url, params):
        url = self.safe_url(url)
        params = dict(params)
        # MECHANICS: filing datasets permit incremental filing-date refreshes; price ranges are fetched as a whole.
        filings = "/filings/" in url
        # Upper date is coverage metadata so the same filing history extends tomorrow.
        # MECHANICS: identify the dataset by endpoint/filters, excluding only the filing upper cutoff so coverage can grow.
        identity = {"url": url, "params": {k: v for k, v in params.items() if not (filings and k == "filing_date.lte")}}
        # MECHANICS: deterministic hash yields a reusable filename, not a security/encryption mechanism.
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        path = self.cache_dir / (digest+".json") if self.cache_dir else None
        saved = self.load(path, identity)
        now = self.utc_now()
        requested_end = params.get("filing_date.lte") if filings else None
        # Never reuse a snapshot downloaded with a later as-of cutoff for an older request.
        # REVIEW: never reuse a cache with a future filing cutoff for a request about an earlier cutoff.
        compatible = saved is not None and (not filings or saved.get("through") <= requested_end)
        if not compatible:
            saved = None
        # MECHANICS: age measures seconds since download, not age of the newest filing in the dataset.
        age = (now-datetime.fromisoformat(saved["refreshed_at"])).total_seconds() if saved else None
        # MECHANICS: matching requested cutoff means query coverage, not proof every SEC filing is in the provider.
        covers = saved is not None and (not filings or saved.get("through") == requested_end)
        # REVIEW: reuse filing snapshots for 3,600 seconds and price snapshots for 900; these are configurable-in-code policies.
        ttl = 3600 if filings else 900
        # MECHANICS: one metadata object is appended now and updated as the retrieval succeeds/fails.
        event = {"endpoint": urlparse(url).path, "requested_through": requested_end,
                 "source": "unavailable", "refreshed_at": None, "refresh_error": None}
        self.events.append(event)
        if saved and (self.offline or (covers and age >= 0 and age < ttl and not self.refresh and not self.full_refresh)):
            event.update(source="offline_cache" if self.offline else "cache", refreshed_at=saved["refreshed_at"],
                         data_through=saved.get("through"), age_seconds=round(age),
                         # REVIEW: cache age alone does not mean historic prices are wrong.
                         # Track scheduled refresh separately from actual coverage gaps.
                         refresh_due=age >= ttl, coverage_complete=covers, stale=not covers)
            return pd.DataFrame(saved["rows"])
        if self.offline:
            raise ValueError("No compatible complete cache; run online once to download this dataset")
        # REVIEW: only dated filing snapshots less than seven days after their full sweep can refresh incrementally.
        incremental = bool(saved and filings and not self.full_refresh and
                           (now-datetime.fromisoformat(saved["full_refreshed_at"])).total_seconds() < 7*86400 and
                           all(isinstance(r.get("filing_date"), str) for r in saved["rows"]))
        query = dict(params)
        if incremental:
            # REVIEW: re-fetch the last seven filing-date days to pick up late additions/corrections within that overlap.
            tail = (date.fromisoformat(saved["through"])-timedelta(days=7)).isoformat()
            query["filing_date.gte"] = max(tail, params.get("filing_date.gte", "0001-01-01"))
        try:
            rows = self.download(url, query)
            if filings and any(not isinstance(r.get("filing_date"), str) or not query.get("filing_date.gte", "0001-01-01") <= r["filing_date"] <= requested_end for r in rows):
                raise ValueError("Filing dates missing or outside requested coverage")
            if incremental:
                # Replace overlap rather than append or deduplicate individual trade rows.
                # REVIEW: replace the entire overlap, retaining older rows; do not append overlapping downloads twice.
                rows = [r for r in saved["rows"] if r["filing_date"] < query["filing_date.gte"]] + rows
        except (ValueError, TypeError, KeyError) as exc:
            # MECHANICS: expose refresh failure even if the last complete snapshot remains usable.
            event["refresh_error"] = str(exc)
            if not saved:
                raise
            event.update(source="stale_cache", refreshed_at=saved["refreshed_at"],
                         data_through=saved.get("through"), age_seconds=round(age), stale=True,
                         refresh_due=True, coverage_complete=covers)
            self.log("Refresh failed; using the previous COMPLETE snapshot, marked stale.")
            return pd.DataFrame(saved["rows"])
        # MECHANICS: successful download time becomes refreshed_at; an incremental refresh preserves full_refreshed_at.
        timestamp = self.utc_now().isoformat()
        # MECHANICS: store query identity, filing coverage, refresh times and rows; no auth header or pagination URL.
        payload = {"version": 1, "identity": identity, "through": requested_end,
                   "refreshed_at": timestamp, "full_refreshed_at": saved["full_refreshed_at"] if incremental else timestamp,
                   "rows": rows}
        try:
            self.save(path, payload)
        except (OSError, ValueError) as exc:
            # MECHANICS: a disk-write failure does not discard successful API data, but the report warns it was not saved.
            event["cache_write_error"] = type(exc).__name__ + "; data retrieved but not saved"
        event.update(source="api_incremental" if incremental else "api_full", refreshed_at=timestamp,
                     data_through=requested_end, age_seconds=0, stale=False,
                     refresh_due=False, coverage_complete=True)
        return pd.DataFrame(rows)


# DATA FLOW: all four data sources enter through this shared retrieval function.
def get_massive_data(url, params):
    """Shared transport and cache used by Form 3, Form 4, 13F and prices."""
    # MECHANICS: normal CLI runs reuse the shared client; direct library calls without it create a non-caching client.
    return (DATA_CLIENT or MassiveClient()).get(url, params)


# DATA FLOW: retrieve initial ownership rows and amendments for the ticker through the requested filing date.
def get_form3(ticker, as_of):
    return get_massive_data(FORM3_URL, {"tickers": ticker, "filing_date.lte": as_of,
                           "form_type.any_of": "3,3/A", "limit": 10000, "sort": "filing_date.asc"})


# DATA FLOW: retrieve ownership-change history by filing date; analytics later filter by transaction date.
def get_form4(ticker, start, as_of):
    # Fetch later filings too, then select transactions by their economic date.
    return get_massive_data(FORM4_URL, {"tickers": ticker, "filing_date.gte": start,
                           "filing_date.lte": as_of, "form_type.any_of": "4,4/A", "limit": 10000,
                           "sort": "filing_date.asc"})


# DATA FLOW: return (table, error). A failed download stays distinct from a successful empty table.
def fetch_layer(function, *args):
    """Keep failed downloads distinct from successful empty results."""
    try:
        return function(*args), None
    except (ValueError, KeyError, TypeError) as exc:
        return pd.DataFrame(), {"status": "unavailable", "reason": str(exc)}


# DATA FLOW: request one manager, not all institutions holding COIN; match the target CUSIP later.
def get_13f(filer_cik, start_date, end_date):
    # This documented endpoint filters by filer and filing date, not ticker.
    return get_massive_data(FORM13F_URL, {"filer_cik": filer_cik,
                           "filing_date.gte": start_date, "filing_date.lte": end_date,
                           "form_type.any_of": "13F-HR,13F-HR/A",
                           "limit": 1000, "sort": "filing_date.asc"})


# MECHANICS: safely get a column; absent fields become an index-aligned Series of unknowns.
def col(df, name):
    values = df[name] if name in df else pd.Series(None, index=df.index, dtype=object)
    # MECHANICS: the live API uses non_derivative, while docs/examples may use
    # non-derivative. Accept exactly those aliases; unknown categories stay unknown.
    return values.replace({"non_derivative": "non-derivative"}) if name == "security_type" else values


def issuer_records(df):
    # REVIEW: retain only the intended SEC issuer, never infer identity from a
    # reused ticker. Missing identifiers are excluded and counted in the report.
    ids = col(df, "issuer_cik").astype("string").str.zfill(10)
    keep = ids.eq(COIN_ISSUER_CIK).fillna(False)
    return df.loc[keep].copy(), {
        "issuer_cik": COIN_ISSUER_CIK, "rows_retained": int(keep.sum()),
        "other_issuer_rows_excluded": int((ids.notna() & ~keep).sum()),
        "missing_issuer_rows_excluded": int(ids.isna().sum())}


# MECHANICS: coerce a quantity/value column to finite nonnegative numbers; invalid entries become NaN.
def numbers(df, name):
    # REVIEW: invalid numeric text is unknown; negative quantities are excluded rather than converted with abs().
    values = pd.to_numeric(col(df, name), errors="coerce")
    return values.where(values.map(lambda x: pd.notna(x) and math.isfinite(x) and x >= 0))


# MECHANICS: select output columns and convert table rows into JSON-compatible dicts; missing fields become null.
def records(df, fields):
    # pandas JSON converts NaN into null and preserves numeric types.
    return json.loads(df.reindex(columns=fields).to_json(orient="records"))


# REVIEW: report initial records/owner counts without treating large initial holdings as a bullish signal.
def analyze_form3(df):
    # REVIEW: owner counts use CIKs; the raw baseline records are retained instead of summed into company ownership.
    return {"status": "ok" if len(df) else "no_data", "source": "Massive Form 3",
            "rows": len(df), "filings": int(col(df, "accession_number").nunique()),
            "owners": int(col(df, "owner_cik").nunique()),
            "initial_ownership_records": records(df, ["owner_name", "owner_cik",
                "filing_date", "form_type", "security_title", "security_type",
                "shares_owned", "direct_or_indirect", "is_director", "is_officer",
                "is_ten_percent_owner", "filing_url"]),
            "note": "Historical initial disclosures, including amendments; not current holdings. "
                    "Rows are not summed across dates, securities or overlapping owners."}


# DATA FLOW: Form 4 history -> current-quarter P/S summaries and optional purchase records.
def analyze_insider_activity(df, start, end, track_purchases=False):
    # MECHANICS: initialize a result dict with an unknown net; populate it only after checking current-window data.
    result = {"source": "Massive Form 4", "status": "no_data", "rows_fetched": len(df),
              "net_insider_value": None,
              "note": "P/S codes include open-market OR private transactions. Non-derivative "
                      "transactions only. No inference of motive or 10b5-1 intent. "
                      "Joint reporting owners may overlap; totals are reported-row activity."}
    # REVIEW: empty provider results are unavailable/no-data here, not evidence of zero economic activity.
    if df.empty:
        if track_purchases:
            result["p_purchase_tracker"] = {"status": "no_data", "transactions": [],
                                            "note": "No Form 4 data returned; purchase activity is unknown."}
        return result
    dates = pd.to_datetime(col(df, "transaction_date"), errors="coerce")
    # REVIEW: non-derivative transactions only, start-exclusive/end-inclusive; holdings and derivatives are excluded.
    tx = df.loc[col(df, "record_type").eq("transaction") &
                col(df, "security_type").eq("non-derivative") &
                dates.gt(pd.Timestamp(start)) & dates.le(pd.Timestamp(end))].copy()
    # MECHANICS: count excluded history rows to disclose the scope of the current-quarter filter.
    result["excluded_or_outside_window_rows"] = len(df) - len(tx)
    # LIMITATION: this diagnostic counts missing dates across all fetched rows, including holding rows.
    result["missing_transaction_dates"] = int(dates.isna().sum())
    # REVIEW: show all retained codes, even though only P/S values feed the net sign.
    result["transaction_codes"] = col(tx, "transaction_code").fillna("unknown").value_counts().to_dict()
    # Old amendments outside this measurement window do not invalidate new flows.
    # Undated amendments remain ambiguous and therefore block a directional net.
    # REVIEW: a dated amendment in the window or an undated amendment blocks a usable net; no supersession is guessed.
    # REVIEW: an old amended holding with no transaction date cannot invalidate
    # this quarter's transaction flows. Exclude holdings here, but keep undated
    # amendment transaction rows (or unknown record types) as blocking ambiguity.
    amendments = (col(df, "form_type").eq("4/A") & ~col(df, "record_type").eq("holding") &
                  (dates.isna() | (dates.gt(pd.Timestamp(start)) & dates.le(pd.Timestamp(end))))).any()
    # REVIEW: P and S are purchases/sales, including private transactions; this is not a pure exchange-trade feed.
    ps = tx.loc[col(tx, "transaction_code").isin(["P", "S"])].copy()
    # MECHANICS: prefer the provider transaction_value when it is a usable number.
    values = numbers(ps, "transaction_value")
    # REVIEW: fallback value is reported shares multiplied by reported price, not a market-price estimate.
    derived = numbers(ps, "transaction_shares") * numbers(ps, "transaction_price_per_share")
    # MECHANICS: fill missing reported values only with the calculated fallback; still unknown if inputs are absent.
    ps["value"] = values.fillna(derived)
    result["values_derived_from_shares_and_price"] = int((values.isna() & derived.notna()).sum())
    result["unpriced_ps_rows"] = int(ps["value"].isna().sum())
    # MECHANICS: calculate parallel purchase and sale summaries from the same filtered records.
    for code, label in [("P", "purchases"), ("S", "sales")]:
        side = ps.loc[ps["transaction_code"].eq(code)]
        shares = numbers(side, "transaction_shares")
        # REVIEW: known subtotals skip unknown values; row/owner counts are not a deduplicated economic-flow estimate.
        result[label] = {"rows": len(side),
                         "unique_owner_ciks": int(col(side, "owner_cik").nunique()),
                         "owners_missing_cik": int(col(side, "owner_cik").isna().sum()),
                         "known_value_usd": float(side["value"].sum()),
                         "known_shares": float(shares.sum()),
                         "missing_share_rows": int(shares.isna().sum())}
    # REVIEW: keep reported 10b5-1 true/false/unknown separate; do not infer intent or adjust value weights.
    flags = col(ps, "aff_10b5_one")
    result["reported_10b5_1_flags"] = {"true": int(flags.eq(True).sum()),
                                       "false": int(flags.eq(False).sum()),
                                       "unknown": int((~flags.isin([True, False])).sum())}
    # Fail closed when data quality prevents a meaningful signed net value.
    # A holding row need not have a transaction date. Only potential transactions
    # can make transaction totals incomplete; uncertain record types are flagged.
    # REVIEW: holding rows do not require a transaction date; potentially relevant incomplete transactions still block net.
    potential = df.loc[~col(df, "record_type").eq("holding") &
                       (dates.isna() | (dates.gt(pd.Timestamp(start)) & dates.le(pd.Timestamp(end))))]
    required = ["record_type", "security_type", "transaction_date", "transaction_code", "form_type"]
    uncertain = any(col(potential, field).isna().any() for field in required)
    # REVIEW: reject the directional net when amendments, missing required fields or unpriced P/S rows remain.
    result["status"] = "incomplete" if amendments or uncertain or result["unpriced_ps_rows"] else "ok"
    result["amendment_reconciliation"] = "required; not implemented" if amendments else "none observed"
    # DATA FLOW: expose the specific blockers rather than only 'incomplete'.
    result["incomplete_reasons"] = []
    if amendments:
        result["incomplete_reasons"].append("An amendment transaction may affect this window.")
    if uncertain:
        result["incomplete_reasons"].append("Potential transactions have missing required fields.")
    if result["unpriced_ps_rows"]:
        result["incomplete_reasons"].append("Some purchase/sale rows have no usable transaction value.")
    if result["status"] == "ok":
        # REVIEW: usable net = known purchase USD minus known sale USD. No role, company-size or historical normalization.
        result["net_insider_value"] = result["purchases"]["known_value_usd"] - result["sales"]["known_value_usd"]
    if track_purchases:
        # Reuse the same dated non-derivative rows as the summary. Do not filter
        # the API request to P: sales and other codes still inform the pipeline.
        buys = ps.loc[ps["transaction_code"].eq("P")].copy()
        # MECHANICS: expose whether each purchase value was reported, derived or unavailable for auditability.
        buys["value_source"] = "unavailable"
        buys.loc[buys["value"].notna(), "value_source"] = "reported_shares_times_price"
        buys.loc[numbers(buys, "transaction_value").notna(), "value_source"] = "reported_transaction_value"
        # MECHANICS: newest purchase first is a display choice; it does not affect classification.
        buys = buys.sort_values("transaction_date", ascending=False)
        # REVIEW: D/I refers to ownership structure; P refers to transaction type. They are different fields.
        ownership = col(buys, "direct_or_indirect")
        result["p_purchase_tracker"] = {
            "status": result["status"] if result["status"] != "ok" else
                      "ok" if len(buys) else "no_matching_purchases",
            "window": {"after": start, "through": end},
            "purchase_rows": len(buys),
            "direct_ownership_rows": int(ownership.eq("D").sum()),
            "indirect_ownership_rows": int(ownership.eq("I").sum()),
            "unknown_ownership_rows": int((~ownership.isin(["D", "I"])).sum()),
            "unpriced_rows": int(buys["value"].isna().sum()),
            "transactions": records(buys, ["transaction_date", "filing_date", "owner_name",
                "owner_cik", "is_director", "is_officer", "officer_title", "is_ten_percent_owner",
                "transaction_code", "security_title", "transaction_shares",
                "transaction_price_per_share", "value", "value_source", "direct_or_indirect",
                "aff_10b5_one", "form_type", "accession_number", "filing_url"]),
            "note": "P identifies open-market or private purchases. D/I separately describes "
                    "direct/indirect ownership. Values are USD; missing values are null. "
                    "Amendments and joint-owner rows are not reconciled. This lists the selected "
                    "window on each run; it is not a background alert or saved purchase history."}
    return result


# REVIEW: percent change = 100*(new/old-1); a zero baseline has no defined percentage.
def analyze_institutional_change(previous_shares, current_shares):
    return None if previous_shares == 0 else 100 * (current_shares / previous_shares - 1)


# DATA FLOW: select one quarter and one CUSIP from a complete manager report; reject ambiguous reports.
def institutional_snapshot(df, period, cusip):
    # REVIEW: match the reporting quarter (period), not the date the manager filed it.
    q = df.loc[col(df, "period").eq(period)]
    if q.empty:
        raise ValueError(f"No report for {period}; missing is not zero holdings")
    # REVIEW: reject amended or unknown report types; adding amendments to originals could double-count holdings.
    if col(q, "form_type").isna().any() or not col(q, "form_type").eq("13F-HR").all():
        raise ValueError(f"{period}: amendments/other forms need reconciliation (not implemented)")
    # REVIEW: require one complete filing accession for the quarter before treating absent target shares as zero reported.
    if col(q, "accession_number").isna().any() or col(q, "accession_number").nunique() != 1:
        raise ValueError(f"{period}: ambiguous filing accessions")
    if col(q, "cusip").isna().any():
        raise ValueError(f"{period}: missing security identifiers")
    # MECHANICS: filter by the supplied exact CUSIP; no issuer-name or ticker guessing.
    security = q.loc[col(q, "cusip").eq(cusip)]
    if "put_call" not in q or col(security, "shares_or_principal_type").isna().any():
        raise ValueError(f"{period}: missing option/share-unit schema")
    # REVIEW: exclude PUT/CALL positions and PRN principal units; retain SH amounts with blank option type.
    stock = security.loc[col(security, "put_call").fillna("").eq("") &
                         col(security, "shares_or_principal_type").eq("SH")]
    shares = numbers(stock, "shares_or_principal_amount")
    if shares.isna().any():
        raise ValueError(f"{period}: unknown share amounts")
    # Absence is zero *reported* shares only after a complete, unambiguous report.
    return {"period": period, "reported_shares": float(shares.sum()),
            "accession": q["accession_number"].iloc[0],
            "filing_dates": col(q, "filing_date").dropna().unique().tolist(),
            "target_rows": len(stock), "excluded_option_or_nonshare_rows": len(security)-len(stock)}


# DATA FLOW: compare validated prior/current reported shares; return delta, percentage and position state.
def analyze_13f(df, previous, current, cusip):
    before = institutional_snapshot(df, previous, cusip)
    after = institutional_snapshot(df, current, cusip)
    # MECHANICS: old/new are reported share totals; difference is a position change, not confirmed trade volume.
    old, new = before["reported_shares"], after["reported_shares"]
    return {"status": "ok", "source": "Massive 13F", "previous": before, "current": after,
            "share_change": new-old, "change_pct": analyze_institutional_change(old, new),
            "position_state": "new reported position" if old == 0 and new > 0 else
                              "exited reported position" if old > 0 and new == 0 else
                              "increased" if new > old else "decreased" if new < old else "unchanged"}


# DATA FLOW: adjusted daily bars -> quarter return, observed trailing range, liquidity proxy and internal bars.
def price_context(ticker, start, end):
    # Include trailing history for purchase-date context and the start boundary.
    # REVIEW: 370 prior calendar days provide the trailing context; availability can still be shorter for new listings.
    first = (date.fromisoformat(start)-timedelta(days=370)).isoformat()
    df = get_massive_data(f"{BASE}/v2/aggs/ticker/{ticker}/range/1/day/{first}/{end}",
                          {"adjusted": "true", "sort": "asc", "limit": 50000})
    # REVIEW: empty provider results are unavailable/no-data here, not evidence of zero economic activity.
    if df.empty:
        raise ValueError("No daily price bars returned")
    # MECHANICS: interpret API milliseconds as UTC then label each bar using the New York calendar date.
    df["day"] = pd.to_datetime(col(df, "t"), unit="ms", utc=True, errors="coerce").dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    df["close"] = numbers(df, "c")
    df = df.sort_values("day")
    # REVIEW: price boundaries are last available closes on/before quarter ends, which may be non-trading days.
    before, after = df.loc[df.day.le(start)], df.loc[df.day.le(end)]
    if before.empty or after.empty:
        raise ValueError("Missing price boundary")
    # MECHANICS: select the final row in each boundary subset after chronological sorting.
    a, b = before.iloc[-1], after.iloc[-1]
    if not pd.notna(a.close) or not pd.notna(b.close) or a.close <= 0 or b.close <= 0 or a.day == b.day:
        raise ValueError("Invalid price boundary closes")
    # REVIEW: reject a last price more than seven calendar days before the end; beginning staleness has no equivalent check.
    if (date.fromisoformat(end)-date.fromisoformat(b.day)).days > 7:
        raise ValueError("Ending price is stale")
    # REVIEW: last 365 calendar days of returned closing prices; not intraday highs/lows or a guaranteed full year.
    trailing = df.loc[df.day.gt((date.fromisoformat(end)-timedelta(days=365)).isoformat())]
    low, high = trailing.close.min(), trailing.close.max()
    # REVIEW: last 20 returned sessions feed a close-times-volume liquidity proxy, not a tradability test.
    volume = numbers(df.tail(20), "v")
    return {"status": "ok", "source": "Massive adjusted daily closes", "start_date": a.day,
            "end_date": b.day, "start_close": float(a.close), "end_close": float(b.close),
            "change_pct": float(100*(b.close/a.close-1)),
            "trailing_close_context": {"observed_sessions": len(trailing),
                "coverage": "at least 240 sessions" if len(trailing) >= 240 else "short history; not a full 52-week range",
                "observed_low_close": float(low), "observed_high_close": float(high),
                "pct_above_observed_low": float(100*(b.close/low-1)),
                "drawdown_from_observed_high_pct": float(100*(b.close/high-1))},
            "average_daily_dollar_volume_last_20_sessions":
                float((volume * df.tail(20).close).mean()) if len(volume) == 20 and volume.notna().all() else None,
            "_bars": records(df, ["day", "close"]),
            "note": "Split-adjusted closing-price context, not intraday highs/lows or dividend total return."}


# REVIEW: only the signs of the three numeric inputs select the state; size/roles do not change it.
def classify_market_state(price_change_pct, insider_net_value, institutional_change_pct):
    # LIMITATION: institutional_change_pct is a legacy name. The pipeline passes share DELTA because only its sign is used.
    values = [price_change_pct, insider_net_value, institutional_change_pct]
    # REVIEW: a missing/nonfinite input is Insufficient data, never silently Mixed/neutral.
    if any(x is None or not math.isfinite(x) for x in values):
        return {"signal": "Insufficient data", "rule": "All three usable inputs are required."}
    # MECHANICS: p=price percent return, i=insider net USD, h=selected-manager share-change direction.
    p, i, h = values
    # Apply the flat band BEFORE up/down branches (fixes unreachable quiet accumulation).
    # REVIEW: both ownership signs positive -> flat price is quiet accumulation, down is divergence, up is confirmation.
    if i > 0 and h > 0:
        signal = "Quiet Accumulation" if abs(p) < 2 else "Positive Ownership Divergence" if p < 0 else "Positive Ownership Confirmation"
    # REVIEW: both signs negative -> rising price is negative divergence, falling price confirms weakness; flat remains mixed.
    elif i < 0 and h < 0:
        # REVIEW: opposing signs or zero signs are mixed/neutral; zero is not treated as missing.
        signal = "Mixed / Neutral" if abs(p) < 2 else "Negative Ownership Divergence" if p > 0 else "Negative Ownership Confirmation"
    else:
        # REVIEW: opposing signs or zero signs are mixed/neutral; zero is not treated as missing.
        signal = "Mixed / Neutral"
    return {"signal": signal, "rule": "Heuristic: insider and selected-filer signs must agree; price flat band is +/-2% (exclusive)."}


# MECHANICS: wrap an analytical call as an unavailable result on expected data errors; preserve other layers.
def attempt(function, *args):
    try:
        return function(*args)
    except (ValueError, KeyError, TypeError) as exc:
        return {"status": "unavailable", "reason": str(exc)}


# REVIEW: include non-derivative transaction rows strictly after start and on/before end.
def transaction_window(df, start, end):
    """Economic-date window, separate from the filing-date availability cutoff."""
    dates = pd.to_datetime(col(df, "transaction_date"), errors="coerce")
    return df.loc[col(df, "record_type").eq("transaction") &
                  col(df, "security_type").eq("non-derivative") &
                  dates.gt(pd.Timestamp(start)) & dates.le(pd.Timestamp(end))].copy()


# MECHANICS: validate one scalar share/value quantity; invalid/negative/infinite values return None.
def finite_number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


# DATA FLOW: exact Form 3 account baselines + comparable Form 4 events -> reconciled balances or reasons.
def reconcile_baselines(form3, form4, end):
    """Conservative direct-account ledger. Never pool classes, trusts or owners.

    Only an unambiguous Form 3 baseline followed by one transaction per date
    can be checked here. Each signed share change must match the next reported
    balance. Unsupported cases stay visible for review instead of being guessed.
    This is arithmetic reconciliation of returned records, not proof that all
    filings/corporate actions exist in the vendor's historical coverage.
    """
    output = []
    # REVIEW: require exact issuer CIK, owner CIK, security title and D/I; title aliases are not normalized.
    keys = ["issuer_cik", "owner_cik", "security_title", "direct_or_indirect"]
    # REVIEW: only non-derivative Form 3 baseline rows enter this ledger.
    initial = form3.loc[col(form3, "security_type").eq("non-derivative")]
    for key, group in initial.reindex(columns=list(dict.fromkeys(list(initial.columns)+keys))).groupby(keys, dropna=False):
        # MECHANICS: create a result object for this account; even an unresolved account remains in the output.
        row = dict(zip(keys, [None if pd.isna(v) else v for v in key]))
        # MECHANICS: initialize unknown balances and an empty list of verified purchase ratios.
        row.update(status="unresolved", baseline_shares=None, latest_verified_shares=None,
                   change_pct=None, purchase_position_increases=[], reason=None)
        output.append(row)
        # REVIEW: direct ownership only and exactly one baseline; multiple baselines or indirect accounts are unresolved.
        if any(pd.isna(v) for v in key) or key[3] != "D" or len(group) != 1:
            row["reason"] = "Requires one exact direct-ownership baseline; ambiguous/indirect groups are not pooled."
            continue
        baseline = group.iloc[0]
        # REVIEW: the baseline uses period_of_report, not filing_date; filing cutoff was already applied at retrieval.
        day = pd.to_datetime(baseline.get("period_of_report"), errors="coerce")
        shares = finite_number(baseline.get("shares_owned"))
        if baseline.get("form_type") != "3" or pd.isna(day) or shares is None or day > pd.Timestamp(end):
            row["reason"] = "Missing, amended or future baseline."
            continue
        # MECHANICS: start with all Form 4 rows eligible, then require each account-key equality.
        mask = pd.Series(True, index=form4.index)
        for field, value in zip(keys, key):
            # MECHANICS: intersect each key match without combining different securities or owners.
            mask &= col(form4, field).eq(value)
        matches = form4.loc[mask & col(form4, "security_type").eq("non-derivative")]
        dates = pd.to_datetime(col(matches, "transaction_date"), errors="coerce")
        tx = matches.loc[col(matches, "record_type").eq("transaction") & dates.ge(day) & dates.le(pd.Timestamp(end))].copy()
        row.update(baseline_shares=shares, baseline_date=day.date().isoformat(),
                   baseline_accession=baseline.get("accession_number"),
                   baseline_filing_url=baseline.get("filing_url"))
        # LIMITATION: any amendment in this matching account history blocks reconciliation, even outside the chosen quarter.
        if col(matches, "form_type").eq("4/A").any() or (col(matches, "record_type").eq("transaction") & dates.isna()).any():
            row["reason"] = "Amendment or undated transaction requires review."
            continue
        if tx.empty:
            row["reason"] = "No subsequent comparable transaction; current balance is not assumed."
            continue
        # REVIEW: refuse multiple same-day events because row ordering/balance conventions are not safely reconstructed.
        if col(tx, "transaction_date").duplicated().any() or dates.loc[tx.index].eq(day).any():
            row["reason"] = "Multiple same-day rows or baseline-day transaction; order is ambiguous."
            continue
        # MECHANICS: running balance starts at Form 3; checked collects purchase ratios only if the whole chain succeeds.
        balance, checked = shares, []
        for _, event in tx.sort_values("transaction_date").iterrows():
            amount = finite_number(event.get("transaction_shares"))
            reported = finite_number(event.get("shares_owned_following_transaction"))
            # REVIEW: acquired/disposed A/D determines signed share movement for ALL transaction codes, not just P/S.
            direction = event.get("transaction_acquired_disposed")
            if amount is None or reported is None or direction not in ("A", "D") or event.get("form_type") != "4":
                row["reason"] = "Incomplete share-ledger fields."
                break
            # REVIEW: add acquisitions, subtract disposals, then compare against the next reported ownership balance.
            expected = balance + (amount if direction == "A" else -amount)
            # REVIEW: tiny tolerances handle floating point; larger mismatches flag gaps/splits/transfers rather than auto-correcting.
            if not math.isclose(expected, reported, rel_tol=1e-8, abs_tol=1e-6):
                row["reason"] = "Reported balance does not match ledger; possible gap, split, transfer or account ambiguity."
                break
            if event.get("transaction_code") == "P" and direction == "A":
                # REVIEW: purchase position increase = 100*purchased shares/prior comparable shares; zero denominator yields unknown.
                checked.append({"date": event.get("transaction_date"), "accession": event.get("accession_number"),
                                "shares_before": balance, "shares_purchased": amount,
                                "position_increase_pct": 100*amount/balance if balance else None,
                                "zero_baseline": balance == 0})
            balance = reported
        else:
            # MECHANICS: Python for/else reaches this only if no event caused break; publish the successfully checked chain.
            row.update(status="reconciled_returned_records", latest_verified_shares=balance,
                       latest_verified_date=tx["transaction_date"].max(),
                       change_pct=100*(balance/shares-1) if shares else None,
                       purchase_position_increases=checked,
                       reason="Exact direct-account arithmetic matched; not current company-wide ownership.")
    return {"status": "ok" if output else "no_baselines", "accounts": output,
            "reconciled_accounts": sum(r["status"] == "reconciled_returned_records" for r in output),
            "note": "Initial ownership is context, not a bullish vote. Unresolved accounts do not feed a directional score."}


# DATA FLOW: history + price context + reconciled accounts -> seven descriptive evidence angles.
def purchase_behavior(history, start, end, price, baseline, cluster_days=30, cluster_buyers=3):
    """Seven evidence angles; no role weighting, motive inference or alpha score."""
    # DATA FLOW: reuse the same quarter and non-derivative filter as the insider summary.
    tx = transaction_window(history, start, end)
    buys = tx.loc[col(tx, "transaction_code").eq("P")].copy()
    buys["value"] = numbers(buys, "transaction_value").fillna(
        numbers(buys, "transaction_shares") * numbers(buys, "transaction_price_per_share"))
    # REVIEW: compare against the immediately preceding calendar quarter, not an equal-day rolling window.
    previous_start = (pd.Timestamp(start).to_period("Q")-1).end_time.date().isoformat()
    prior = transaction_window(history, previous_start, start)
    prior_buys = prior.loc[col(prior, "transaction_code").eq("P")]
    prior_values = numbers(prior_buys, "transaction_value").fillna(
        numbers(prior_buys, "transaction_shares") * numbers(prior_buys, "transaction_price_per_share"))
    dates = pd.to_datetime(col(buys, "transaction_date"), errors="coerce")
    history_dates = pd.to_datetime(col(history, "transaction_date"), errors="coerce")
    history_filings = pd.to_datetime(col(history, "filing_date"), errors="coerce")
    # MECHANICS: one buyer summary per known CIK; rows without CIK remain in counts/known value but not buyer identity groups.
    buyers = []
    for cik, rows in buys.loc[col(buys, "owner_cik").notna()].groupby("owner_cik"):
        # Role memberships overlap; counts must not be added into distinct buyers.
        titles = col(rows, "officer_title").dropna().astype(str).unique().tolist()
        first_day = pd.to_datetime(rows["transaction_date"]).min()
        # REVIEW: historical purchases must predate the first current-quarter purchase in both transaction and filing dates.
        earlier = history.loc[col(history, "owner_cik").eq(cik) & history_dates.lt(first_day) &
                              history_filings.lt(first_day) & col(history, "record_type").eq("transaction") &
                              col(history, "security_type").eq("non-derivative") & col(history, "transaction_code").eq("P")]
        earlier_dates = pd.to_datetime(col(earlier, "transaction_date"), errors="coerce")
        last = earlier_dates.max()
        # A descriptive recurrence check only, not a replication of the 2012 paper.
        # REVIEW: examine the three prior calendar years relative to that first purchase; history completeness is not certified.
        years = [first_day.year-offset for offset in (1, 2, 3)]
        # REVIEW: same-month occurrence in every prior year is descriptive recurrence; absence is not proof of opportunism.
        repeated = all(((earlier_dates.dt.year == y) & (earlier_dates.dt.month == first_day.month)).any() for y in years)
        buyers.append({"owner_cik": cik, "owner_names": col(rows, "owner_name").dropna().unique().tolist(),
                       "officer_titles": titles, "is_officer": bool(col(rows, "is_officer").eq(True).any()),
                       "is_director": bool(col(rows, "is_director").eq(True).any()),
                       "is_ten_percent_owner": bool(col(rows, "is_ten_percent_owner").eq(True).any()),
                       "known_purchase_value_usd": float(rows.value.sum()),
                       "last_earlier_observed_purchase": None if pd.isna(last) else last.date().isoformat(),
                       "days_since_last_observed_purchase": None if pd.isna(last) else (first_day-last).days,
                       "calendar_pattern": "same-month purchases observed in each of prior three years" if repeated else
                                           "no three-year recurrence established; not classified as opportunistic"})
    # MECHANICS: list of qualifying rolling windows; overlapping windows are retained, not independent signals.
    clusters = []
    for day in sorted(dates.dropna().unique()):
        # REVIEW: inclusive calendar-day interval: start plus days-1. This is not a trading-day window.
        finish = pd.Timestamp(day) + pd.Timedelta(days=cluster_days-1)
        members = buys.loc[dates.ge(day) & dates.le(finish)]
        # REVIEW: distinct known CIKs prevent one person with many rows from meeting the cluster threshold alone.
        ids = col(members, "owner_cik").dropna().unique().tolist()
        if len(ids) >= cluster_buyers:
            clusters.append({"start": pd.Timestamp(day).date().isoformat(),
                             "end": min(finish, pd.Timestamp(end)).date().isoformat(),
                             "distinct_buyers": len(ids), "owner_ciks": ids})
    known = float(buys.value.sum())
    # REVIEW: concentration requires every purchase to have a value and owner CIK; otherwise it stays unknown.
    complete_values = buys.value.notna().all() and col(buys, "owner_cik").notna().all()
    # REVIEW: largest buyer known USD / total purchase USD *100; not buyer wealth or portfolio concentration.
    concentration = 100*max((r["known_purchase_value_usd"] for r in buyers), default=0)/known if known and complete_values else None
    # Preserve filing text as evidence. Keyword flags request review, never prove intent.
    review_rows = []
    for _, row in buys.iterrows():
        text = json.dumps(row.get("footnotes", []), default=str) + " " + str(row.get("remarks", ""))
        # LIMITATION: substring screening (including broad words like issuer) can false-positive; flags request manual review.
        terms = [term for term in ("private placement", "subscription agreement", "purchase agreement", "issuer", "pipe") if term in text.lower()]
        if terms:
            review_rows.append({"accession": row.get("accession_number"), "filing_url": row.get("filing_url"),
                                "matched_terms": terms, "footnotes": row.get("footnotes"), "remarks": row.get("remarks")})
    purchase_price_context = []
    # DATA FLOW: turn internal price records into a table for purchase-date context; no additional API call.
    bars = pd.DataFrame(price.get("_bars", []))
    for day in sorted(col(buys, "transaction_date").dropna().unique()):
        if bars.empty:
            break
        # REVIEW: use only closes on/before the purchase date, but same-day closing data is retrospective at execution time.
        observed = bars.loc[bars.day.le(day) & bars.day.gt((pd.Timestamp(day)-pd.Timedelta(days=365)).date().isoformat())]
        observed = observed.loc[pd.to_numeric(observed.close, errors="coerce").gt(0)]
        if observed.empty:
            continue
        close, low, high = observed.close.iloc[-1], observed.close.min(), observed.close.max()
        purchase_price_context.append({"purchase_date": day, "price_date": observed.day.iloc[-1],
            "observed_sessions": len(observed), "adjusted_close": float(close),
            "pct_above_observed_low_close": float(100*(close/low-1)),
            "drawdown_from_observed_high_close_pct": float(100*(close/high-1)),
            "coverage": "short history" if len(observed) < 240 else "at least 240 sessions"})
    # DATA FLOW: reuse summary quality checks for current/prior quarter so comparison does not hide a missing-price issue.
    current_status = analyze_insider_activity(history, start, end)["status"]
    prior_status = analyze_insider_activity(history, previous_start, start)["status"]
    observations = [f"{len(buys)} reported P rows from {len(buyers)} identified buyers across {dates.nunique()} dates.",
                    f"Known purchase value is ${known:,.2f}; {int(buys.value.isna().sum())} rows lack a usable value."]
    if clusters:
        observations.append(f"At least one {cluster_days}-day window meets the configured {cluster_buyers}-buyer cluster threshold.")
    if col(tx, "transaction_code").eq("S").any() and len(buys):
        observations.append("Purchases and sales coexist; buying does not erase the selling evidence.")
    if price.get("change_pct") is not None and len(buys):
        observations.append(f"Quarter price return was {price['change_pct']:.2f}%; this is period context, not purchase-day timing.")
    if current_status == prior_status == "ok":
        prior_count = int(col(prior_buys, "owner_cik").nunique())
        observations.append(f"Previous quarter: {prior_count} identified buyers and ${float(prior_values.sum()):,.2f} in known purchases; "
                            f"current quarter: {len(buyers)} buyers and ${known:,.2f}. Returned-record comparison only.")
    if concentration is not None:
        observations.append(f"The largest identified buyer accounts for {concentration:.1f}% of purchase value.")
    if current_status != "ok":
        observations.append("These descriptive counts are incomplete or unavailable; do not treat them as a confirmed directional signal.")
    # DATA FLOW: package seven angles. Unconnected compensation, market cap and earnings inputs remain explicit unknowns.
    return {"status": current_status, "observations": observations,
            "who_bought": buyers,
            "relative_size": {"known_purchase_value_usd": known, "unpriced_rows": int(buys.value.isna().sum()),
                              "largest_buyer_pct_of_value": concentration,
                              "reconciled_position_changes": [dict(owner_cik=r["owner_cik"], **p) for r in baseline.get("accounts", [])
                                  for p in r["purchase_position_increases"] if start < p["date"] <= end],
                              "compensation_ratio": {"status": "unavailable", "reason": "Compensation dataset not connected."},
                              "market_cap_ratio": {"status": "unavailable", "reason": "Date-matched market capitalization not connected."}},
            "cluster_buying": {"minimum_buyers": cluster_buyers, "calendar_days": cluster_days,
                               "windows": clusters, "note": "Overlapping windows are not independent signals; CIKs may share economic ownership."},
            "historical_comparison": {"previous_window": {"after": previous_start, "through": start},
                "status": "comparable_returned_records" if current_status == prior_status == "ok" else "incomplete",
                "previous_purchase_rows": len(prior_buys), "previous_known_value_usd": float(prior_values.sum()),
                "previous_distinct_buyers": int(col(prior_buys, "owner_cik").nunique()),
                "note": "Absence in returned history is not proof of a first-ever purchase or complete historical coverage."},
            "routine_context": "Calendar recurrence is descriptive only; no routine/opportunistic prediction or weighting is assigned.",
            "price_and_time": {"quarter_price_change_pct": price.get("change_pct"),
                               "quarter_end_trailing_range": price.get("trailing_close_context"),
                               "purchase_day_context": purchase_price_context,
                               "timing_note": "Retrospective end-of-day price context, not a signal available at execution time.",
                               "earnings_blackouts": "not available; no earnings calendar or company blackout policy connected",
                               "filing_acceptance_timestamp": "not supplied by these rows; no tradable backtest timing assumed"},
            "firm_characteristics": {"market_cap": None, "size_category": "unavailable",
                                     "average_daily_dollar_volume_last_20_sessions": price.get("average_daily_dollar_volume_last_20_sessions"),
                                     "note": "No size premium, liquidity filter or predictive strength inferred."},
            "ownership_type": {"direct_rows": int(col(buys, "direct_or_indirect").eq("D").sum()),
                               "indirect_rows": int(col(buys, "direct_or_indirect").eq("I").sum()),
                               "unknown_rows": int((~col(buys, "direct_or_indirect").isin(["D", "I"])).sum())},
            "private_purchase_review": {"flagged_rows": review_rows,
                                        "note": "Keyword screening is incomplete; no hit does not establish an exchange purchase."},
            "limitations": ["Reported rows may overlap across joint owners.", "Amendments require reconciliation, not blind deduplication.",
                            "10b5-1 flags describe reported plan status, not motive.", "No return prediction or risk-personalized recommendation."]}


# DATA FLOW: call the simple classifier, standardize five labels, attach evidence and counterevidence.
def evidence_classification(price, insider, institutional, behavior):
    """Five states plus unavailable. Gross buying/selling remains visible."""
    p = price.get("change_pct")
    net = insider.get("net_insider_value")
    h = institutional.get("share_change")
    # REVIEW: an incomplete/failed institutional layer cannot contribute a direction.
    if institutional.get("status", "ok") != "ok":
        h = None
    # DATA FLOW: only price, insider NET and manager DELTA enter the classifier; baseline and behavior attach afterward.
    result = classify_market_state(p, net, h)
    # One state with an explicit direction, rather than six differently named states.
    if "Confirmation" in result["signal"]:
        result["direction"] = "upward" if p > 0 else "downward"
        result["signal"] = "Confirmation"
    result["signal"] = {"Positive Ownership Divergence": "Positive divergence",
                        "Negative Ownership Divergence": "Negative divergence",
                        "Quiet Accumulation": "Quiet accumulation",
                        "Mixed / Neutral": "Mixed/neutral"}.get(result["signal"], result["signal"])
    result["evidence"] = {"price_change_pct": p, "insider_net_reported_value_usd": net,
                          "purchases": insider.get("purchases"), "sales": insider.get("sales"),
                          "selected_manager_share_change": h}
    # REVIEW: these observations explain context but do not mechanically change the state.
    result["supporting_context"] = behavior.get("observations", [])
    # MECHANICS: accumulate conflicting facts separately; a net direction must not hide purchases coexisting with sales.
    result["counterevidence"] = []
    if insider.get("purchases", {}).get("rows", 0) and insider.get("sales", {}).get("rows", 0):
        result["counterevidence"].append("Both purchase and sale activity exists; net direction compresses opposing behavior.")
    # MECHANICS: name absent numerical inputs so Insufficient data can be diagnosed.
    result["missing_inputs"] = [name for name, value in [("usable price return", p), ("usable insider net", net),
                                                        ("selected-manager quarterly comparison", h)] if value is None]
    result["scope"] = "Single selected manager and reported insider rows; no market-wide institutional claim."
    result["interpretation"] = "Descriptive alignment of observed directions, not a forecast, valuation or inferred investor motive."
    result["rule"] = "Price flat band: abs(return) < 2%; insider net and selected-manager share-change signs must agree. Form 3 and seven-angle context are evidence, not weighted votes."
    # DATA FLOW: retain the full three-input decision separately for auditability.
    result["combined_signal"] = result["signal"]
    result["analysis_status"] = "insufficient" if result["missing_inputs"] else "complete_for_selected_scope"
    # REVIEW: missing 13F does not block a useful two-input classification.
    # Never substitute zero or copy insider activity into institutional holdings.
    if h is None:
        partial = insider_price_reading(price, insider)
        if partial["signal"] != "Insufficient data":
            result.update(signal=partial["signal"], direction=partial["direction"],
                          analysis_status="partial", scope=partial["scope"], rule=partial["rule"])
            result["counterevidence"].append("Institutional evidence is missing; the partial classification may change when 13F is available.")
    result["institutional_status"] = institutional.get("status", "ok" if h is not None else "unavailable")
    result["institutional_missing_reason"] = institutional.get("reason", "No usable quarterly 13F comparison") if h is None else None
    return result


# MECHANICS: recursively convert pandas/numpy values into strict JSON types without NaN or Infinity.
def insider_price_reading(price, insider):
    """REVIEW: separate two-input description, never a substitute 13F value.

    The full combined classifier requires institutional evidence. This reading
    describes price versus net reported P/S value only, with the same flat band.
    It says nothing about investor intent, probabilities or future returns.
    """
    p, net = price.get("change_pct"), insider.get("net_insider_value")
    result = {"scope": "Form 4 and price only; institutions not included",
              "signal": "Insufficient data", "direction": None,
              "rule": "Net reported P/S value sign versus price; abs(price return) < 2% is flat."}
    if p is None or net is None or not all(math.isfinite(v) for v in (p, net)):
        return result
    if net > 0:
        result["signal"] = "Quiet accumulation" if abs(p) < 2 else "Positive divergence" if p < 0 else "Confirmation"
    elif net < 0:
        result["signal"] = "Mixed/neutral" if abs(p) < 2 else "Negative divergence" if p > 0 else "Confirmation"
    else:
        result["signal"] = "Mixed/neutral"
    if result["signal"] == "Confirmation":
        result["direction"] = "upward" if p > 0 else "downward"
    return result


# MECHANICS: normalize setup values without claiming to verify the security mapping.
def validate_institutional_settings(cik, cusip):
    cik, cusip = str(cik).strip(), str(cusip).strip().upper()
    if not cik.isascii() or not cik.isdigit() or not 1 <= len(cik) <= 10:
        raise ValueError("Manager CIK must contain 1-10 ASCII digits")
    if len(cusip) != 9 or not cusip.isascii() or not cusip.isalnum():
        raise ValueError("COIN share-class CUSIP must contain 9 ASCII letters/digits")
    return {"ticker": "COIN", "filer_cik": cik.zfill(10), "cusip": cusip}


def load_institutional_settings(path):
    """DATA FLOW: saved nonsecret identifiers supply defaults for normal runs."""
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or saved.get("ticker") != "COIN":
            raise ValueError("Settings must describe COIN")
        return validate_institutional_settings(saved["filer_cik"], saved["cusip"])
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError("Institutional settings are invalid or unreadable; rerun --configure-institutional or supply both identifiers") from None


def configure_institutional_settings(path):
    """MECHANICS: guided setup stores identifiers only, never the API key.

    REVIEW: the user chooses the manager and verifies the share-class CUSIP;
    this prototype does not silently choose a manager or claim market breadth.
    """
    print("COIN institutional setup (one selected manager)")
    print("Use the manager's SEC CIK and a verified CUSIP for COIN's target share class. No API key is requested.")
    try:
        cik = input("Manager CIK: ")
        cusip = input("COIN share-class CUSIP: ")
    except (EOFError, KeyboardInterrupt):
        raise ValueError("Setup cancelled; existing settings were not changed") from None
    settings = validate_institutional_settings(cik, cusip)
    # Reuse atomic-file writing, not its cache schema. Validation happens first.
    MassiveClient().save(path, settings)
    print("Saved institutional identifiers. Run the program normally to retrieve and compare the two reports.")


def clean_output(value):
    """Convert pandas/numpy scalars and unknowns to portable, strict JSON."""
    if isinstance(value, dict):
        return {str(k): clean_output(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_output(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return clean_output(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


# MECHANICS: display the report already calculated; printing must not change analytical conclusions.
def display_number(value, kind="number"):
    # MECHANICS: format presentation only; calculations retain full precision.
    if value is None:
        return "Unavailable"
    if kind == "money":
        return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"
    if kind == "percent":
        return f"{value:+.2f}%"
    return f"{value:,.0f}"


def layer_label(layer, event):
    # DATA FLOW: analytical validity and delivery source are different dimensions.
    # A downloaded but analytically incomplete table must never read 'Available'.
    labels = {"ok": "Available", "incomplete": "Incomplete", "unavailable": "Unavailable",
              "no_data": "No data returned", "not_configured": "Not configured"}
    status = labels.get(layer.get("status"), "Unavailable")
    if layer.get("status") == "ok" and event.get("source") in ("cache", "offline_cache", "stale_cache"):
        status = "Using saved data"
    if event.get("refresh_error"):
        status += " (refresh failed)"
    elif event.get("coverage_complete") is False:
        status += " (older cutoff)"
    return status


def print_summary(report, debug=False):
    """Analyst-facing report: conclusion, facts, interpretation, then relevant limits.

    REVIEW: this is presentation, not a change to the five-state analytical rules.
    The full evidence object remains available through --json; --debug appends
    operational metadata and all unfinished capabilities for technical inspection.
    """
    result = report["classification"]
    window = report["measurement_window"]
    form3, form4 = report["form3"], report["form4"]
    inst, price = report["institutional"], report["price"]
    behavior = report["purchase_behavior"]
    events = report.get("data_freshness", [])
    def event_for(fragment):
        return next((entry for entry in events if fragment in entry["endpoint"]), {})

    print(f"\n{report['ticker']} | OWNERSHIP ANALYSIS")
    print(f"Period: after {window['after']} through {window['through']} | Filing cutoff requested: {report['as_of']}")
    print("-" * 78)
    # REVIEW: withheld classification needs an actionable reason before any figures.
    if result["signal"] == "Insufficient data":
        print("CONCLUSION: Classification pending")
        friendly = {"usable price return": "usable price data", "usable insider net": "complete insider transaction analysis",
                    "selected-manager quarterly comparison": "the institutional comparison"}
        missing = [friendly.get(item, item) for item in result.get("missing_inputs", [])]
        if inst.get("status") == "not_configured" and missing == ["the institutional comparison"]:
            print("Reason: the institutional comparison is not configured.")
        else:
            print("Needed: " + "; ".join(missing) + ".")
    else:
        direction = f" ({result['direction']})" if result.get("direction") else ""
        partial = result.get("analysis_status") == "partial"
        print(f"CONCLUSION: {result['signal']}{direction}" + (" | PARTIAL ANALYSIS" if partial else ""))
        meaning = {"Positive divergence": "Price declined while insider net buying and the selected manager's reported shares increased.",
                   "Negative divergence": "Price rose while insider net selling and the selected manager's reported shares decreased.",
                   "Quiet accumulation": "Price stayed within the flat band while both ownership measures increased.",
                   "Confirmation": "Price and the two measured ownership directions aligned.",
                   "Mixed/neutral": "The measured directions disagree, are unchanged, or do not meet an aligned-state rule."}
        if partial:
            print("Based on Form 4 net reported purchases/sales and price. Institutional evidence is not included.")
            print("Missing: 13F quarterly comparison. " + str(result.get("institutional_missing_reason") or ""))
        else:
            print(meaning.get(result["signal"], result["interpretation"]))
    if result.get("uses_stale_data"):
        print("UPDATE WARNING: some inputs have an older cutoff or a failed refresh; this is not a refreshed conclusion.")
    reading = report.get("insider_price_reading", {})
    if result["signal"] == "Insufficient data" and reading.get("signal") and reading["signal"] != "Insufficient data":
        direction = f" ({reading['direction']})" if reading.get("direction") else ""
        print(f"Form 4 + price reading: {reading['signal']}{direction}. Institutions are not included in this reading.")
        if reading.get("uses_stale_data"):
            print("This Form 4 + price reading also uses saved inputs with a refresh failure or older cutoff.")

    print("\nEVIDENCE AT A GLANCE")
    print(f"{'Measure':<25} {'Finding':<28} Status")
    def show(label, finding, layer, fragment):
        print(f"{label:<25} {finding:<28} {layer_label(layer, event_for(fragment))}")
    show("Price change", display_number(price.get("change_pct"), "percent"), price, "/aggs/")
    show("Initial reporting owners", display_number(form3.get("owners")), form3, "/form-3")
    purchases, sales = form4.get("purchases", {}), form4.get("sales", {})
    # REVIEW: missing layers use Unavailable; incomplete layers show known subtotals,
    # explicitly marked Incomplete, never a silently complete zero.
    show("P purchases (known USD)", display_number(purchases.get("known_value_usd"), "money"), form4, "/form-4")
    show("Sales (known USD)", display_number(sales.get("known_value_usd"), "money"), form4, "/form-4")
    show("Net reported activity", display_number(form4.get("net_insider_value"), "money"), form4, "/form-4")
    show("Institutional shares", display_number(inst.get("share_change")), inst, "/13-F")

    print("\nWHAT THE RECORDS SHOW")
    if form4.get("status") == "ok":
        if purchases.get("rows", 0) == 0:
            print("- No P-purchase rows were found in the retrieved records for this quarter.")
        else:
            print(f"- {purchases['rows']} P-purchase rows across {purchases['unique_owner_ciks']} identified buyers.")
        print(f"- {sales.get('rows', 0)} sale rows across {sales.get('unique_owner_ciks', 0)} identified sellers.")
    else:
        print("- Insider activity is incomplete or unavailable; displayed subtotals are not complete activity totals.")
    # Keep the main view short; avoid repeating zero-valued purchase metrics.
    if purchases.get("rows", 0):
        clusters = behavior.get("cluster_buying", {})
        if clusters.get("windows"):
            print(f"- Buying meets the configured cluster threshold: {clusters['minimum_buyers']} buyers within {clusters['calendar_days']} calendar days.")
        concentration = behavior.get("relative_size", {}).get("largest_buyer_pct_of_value")
        if concentration is not None:
            print(f"- The largest identified buyer represents {concentration:.1f}% of purchase value.")
    baseline = report["ownership_baseline"]
    if baseline.get("reconciled_accounts", 0):
        print(f"- {baseline['reconciled_accounts']} direct accounts passed baseline reconciliation.")
    else:
        print("- Baseline position changes could not be verified under the current reconciliation rules.")
    for point in result.get("counterevidence", []):
        if not point.startswith("Some inputs are saved snapshots"):
            print("- " + point)

    print("\nCOVERAGE AND NEXT STEP")
    if events:
        saved_events = [entry for entry in events if entry.get("source") in ("cache", "offline_cache", "stale_cache")]
        mode = "Saved snapshots" if len(saved_events) == len(events) else "API and saved snapshots" if saved_events else "API retrieval"
        print(f"- Data mode: {mode}.")
        stamps = sorted(entry['refreshed_at'] for entry in events if entry.get('refreshed_at'))
        if stamps:
            print(f"- Last successful dataset checks (UTC): {stamps[0][:19]} to {stamps[-1][:19]}.")
        if any(entry.get("refresh_due") and not entry.get("stale") for entry in events):
            print("- The refresh interval has elapsed; the next online run can check for updates. Historical values are not invalidated by age alone.")
        for entry in events:
            name = entry["endpoint"].rsplit("/", 1)[-1] if "/filings/" in entry["endpoint"] else "prices"
            if entry.get("refresh_error"):
                print(f"- {name}: refresh failed ({entry['refresh_error']}); saved data was retained where available.")
            if entry.get("coverage_complete") is False:
                print(f"- {name}: saved filing coverage ends {entry.get('data_through')}; requested cutoff is later.")
            if entry.get("cache_write_error"):
                print(f"- {name}: retrieved data could not be saved for the next run.")
    for name, layer in (("Form 3", form3), ("Form 4", form4), ("Price", price), ("13F", inst)):
        if layer.get("reason") and layer.get("status") != "not_configured":
            print(f"- {name}: {layer['reason']}")
        for reason in layer.get("incomplete_reasons", []):
            print(f"- {name}: {reason}")
    if inst.get("status") == "not_configured":
        print("- Next: run --configure-institutional once to save a manager CIK and verified COIN CUSIP.")
    elif result.get("uses_stale_data"):
        print("- Next: run --refresh when the API is available; --debug shows retrieval details.")
    print("- Scope: one company and, when configured, one manager. Reported rows may overlap across owners.")
    print("- This is a descriptive ownership analysis; personalized advice and the chatbot are not implemented.")
    print("\nInspect: --track-purchases for purchase records | --json for full evidence | --debug for diagnostics")
    tracker = form4.get("p_purchase_tracker")
    if tracker and tracker.get("transactions"):
        print("\nINDIVIDUAL P PURCHASE RECORDS\n" + json.dumps(tracker, indent=2, allow_nan=False))
    if debug:
        print("\nTECHNICAL DIAGNOSTICS\n" + json.dumps({
            "data_freshness": events, "issuer_scope": report.get("issuer_scope"),
            "baseline_details": baseline, "not_implemented": report["not_implemented"]}, indent=2, allow_nan=False))


# DATA FLOW: orchestration entry point. Follow the report assignments to see how all layers connect.
def run_pipeline(args):
    if DATA_CLIENT is not None:
        DATA_CLIENT.events.clear()
    # DATA FLOW: start/end define the economic window; as_of caps filing availability for all retrieved filings.
    start, end, as_of = args.previous_period, args.current_period, args.as_of
    # MECHANICS: this dict is the single output object populated by each connected stage.
    report = {"ticker": "COIN", "as_of": as_of,
              "measurement_window": {"after": start, "through": end},
              "data_policy": "API-derived observations, fetched live or reused from a labeled complete cache; null means unavailable."}
    # DATA FLOW STEP 1: obtain initial ownership table, or preserve its retrieval error.
    form3, error3 = fetch_layer(get_form3, "COIN", as_of)
    # Pull multi-year history and extend to the oldest Form 3 baseline for the ledger.
    # REVIEW: start with four years of history, then extend backward to the oldest returned Form 3 event.
    history_start = (pd.Timestamp(start)-pd.DateOffset(years=4)).date().isoformat()
    baseline_dates = pd.to_datetime(col(form3, "period_of_report"), errors="coerce").dropna()
    if len(baseline_dates):
        history_start = min(history_start, baseline_dates.min().date().isoformat())
    # DATA FLOW STEP 2: fetch the shared history table once for summary, ledger and behavioral comparisons.
    form4, error4 = fetch_layer(get_form4, "COIN", history_start, as_of)
    # DATA FLOW: retrieval bounds remain unchanged so existing complete caches
    # remain reusable. Apply issuer identity BEFORE all analytical functions.
    form3, scope3 = issuer_records(form3)
    form4, scope4 = issuer_records(form4)
    report["issuer_scope"] = {"form3": scope3, "form4": scope4}
    # DATA FLOW: summarize the baseline only if retrieval succeeded; otherwise expose error3.
    report["form3"] = error3 or attempt(analyze_form3, form3)
    # DATA FLOW: summarize current-quarter transactions; tracking flag changes detail display, not which calculations run.
    report["form4"] = error4 or attempt(analyze_insider_activity, form4, start, end, getattr(args, "track_purchases", False))
    report["history"] = {"filings_requested_from": history_start, "filings_known_through": as_of,
                         "note": "History may be incomplete at provider; returned-row comparisons only."}
    # DATA FLOW STEP 3: both tables must be available to attempt baseline reconciliation.
    report["ownership_baseline"] = error3 or error4 or attempt(reconcile_baselines, form3, form4, end)
    # DATA FLOW STEP 4: get quarter price context independently, so a filing failure does not discard valid prices.
    report["price"] = attempt(price_context, "COIN", start, end)
    # DATA FLOW STEP 5: always run purchase analysis using history + available price + ledger context.
    report["purchase_behavior"] = error4 or attempt(purchase_behavior, form4, start, end, report["price"],
        report["ownership_baseline"], getattr(args, "cluster_days", 30), getattr(args, "cluster_buyers", 3))
    # MECHANICS: remove internal bars after purchase analysis; retain the summarized price evidence in the output.
    report["price"].pop("_bars", None)
    inst = {"status": "not_configured", "reason": "Supply --filer-cik and --cusip for COIN's target share class."}
    # DATA FLOW STEP 6: only run a manager comparison when BOTH explicit identifiers exist; no default manager invented.
    if args.filer_cik and args.cusip:
        # DATA FLOW: retrieve filer-centric 13F rows, then compare target-CUSIP shares at the two quarter ends.
        inst = attempt(lambda: analyze_13f(get_13f(args.filer_cik, start, as_of), start, end, args.cusip))
    inst.update({"filer_cik": args.filer_cik, "cusip": args.cusip,
                 "scope": "One selected manager only; not market-wide institutional breadth.",
                 "limitations": "Quarter-end reported holdings, not trades or cash flows. "
                 "Confidential/omitted positions and amendments limit coverage. "
                 "No cross-manager discovery, overlap reconciliation, or split normalization implemented."})
    report["institutional"] = inst
    # A zero baseline has no defined percentage; use actual share delta for direction.
    # DATA FLOW STEP 7: produce the state from three numerical layers and attach behavioral evidence.
    result = evidence_classification(report["price"], report["form4"], inst, report["purchase_behavior"])
    result["timing"] = "Transactions and price cover the quarter; 13F compares quarter ends. " + \
                       "Filings known by as_of only; this is not a point-in-time backtest."
    # MECHANICS: store the same result dict; later freshness annotations update this report object too.
    report["classification"] = result
    # DATA FLOW: keep the limited insider reading separate from the combined state.
    report["insider_price_reading"] = insider_price_reading(report["price"], report["form4"])
    report["data_freshness"] = list(DATA_CLIENT.events) if DATA_CLIENT else []
    # REVIEW: any stale retrieval flags the conclusion; it does not erase valid cached observations or suppress the state.
    result["uses_stale_data"] = any(item.get("stale", False) for item in report["data_freshness"])
    report["insider_price_reading"]["uses_stale_data"] = any(
        item.get("stale", False) for item in report["data_freshness"]
        if "/form-4" in item["endpoint"] or "/aggs/" in item["endpoint"])
    if result["uses_stale_data"]:
        result["counterevidence"].append("Some inputs are saved snapshots with stale or incomplete date coverage; this state is not a refreshed conclusion.")
    report["not_implemented"] = ["Market-wide institutional breadth", "Amendment reconciliation",
                                 "Joint-owner transaction reconciliation", "Indirect account and complex same-day ledgers",
                                 "Compensation and historical market capitalization", "Earnings/blackout calendar",
                                 "Point-in-time acceptance timestamps and backtesting", "AI/chatbot layer"]
    # DATA FLOW: return the completed portable report to main for readable or JSON display.
    return clean_output(report)


# MECHANICS: parse command-line settings, validate dates/identifiers, create client, run pipeline, display report.
def main():
    global DATA_CLIENT
    # MECHANICS: create the command-line parser; running with no flags uses the defaults below.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--previous-period", help="Earlier quarter end, YYYY-MM-DD")
    parser.add_argument("--current-period", help="Next quarter end, YYYY-MM-DD")
    parser.add_argument("--filer-cik", help="Selected manager's SEC CIK; no arbitrary manager is assumed")
    parser.add_argument("--cusip", help="Verified COIN share-class CUSIP (9 characters)")
    parser.add_argument("--configure-institutional", action="store_true", help="Save manager/CUSIP identifiers interactively, then exit")
    parser.add_argument("--settings-file", type=Path,
                        default=Path(__file__).resolve().parent / "insider_settings.json",
                        help="Nonsecret institutional setup file")
    parser.add_argument("--track-purchases", action="store_true",
                        help="List individual non-derivative P-code purchases in the selected quarter")
    # REVIEW: user-adjustable screening thresholds; neither threshold has been statistically calibrated here.
    parser.add_argument("--cluster-days", type=int, default=30, help="Rolling calendar-day window for purchase clusters")
    parser.add_argument("--cluster-buyers", type=int, default=3, help="Distinct owner CIKs needed for a purchase cluster")
    parser.add_argument("--json", action="store_true", help="Print the complete structured evidence report instead of the readable summary")
    parser.add_argument("--debug", action="store_true", help="Show API progress and technical diagnostics")
    parser.add_argument("--cache-dir", default=str(Path(__file__).resolve().parent / ".insider_cache"),
                        help="Local API snapshot folder (default: .insider_cache beside the script)")
    # MECHANICS: offline, forced incremental refresh and forced full refresh cannot be requested together.
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--offline", action="store_true", help="Use saved data only; never call Massive")
    modes.add_argument("--refresh", action="store_true", help="Refresh now, using overlapping incremental filing updates where possible")
    modes.add_argument("--full-refresh", action="store_true", help="Re-download full queries to capture older corrections")
    # MECHANICS: build an args object whose attributes supply pipeline configuration.
    args = parser.parse_args()
    if args.configure_institutional:
        try:
            configure_institutional_settings(args.settings_file)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        return
    try:
        # Explicit pairs override saved setup; never mix half a new pair with a saved pair.
        if not args.filer_cik and not args.cusip:
            settings = load_institutional_settings(args.settings_file)
            if settings:
                args.filer_cik, args.cusip = settings["filer_cik"], settings["cusip"]
        as_of = date.fromisoformat(args.as_of)
        if args.cluster_days < 1 or args.cluster_buyers < 2:
            raise ValueError("Cluster window must be positive and cluster buyer count at least 2")
        # Default to the newest quarter whose normal 45-day filing window has elapsed.
        # REVIEW: subtract 45 CALENDAR days, then select the most recent quarter end no later than that cutoff.
        cutoff = pd.Timestamp(as_of-timedelta(days=45))
        current = cutoff.to_period("Q").end_time.normalize()
        if current > cutoff:
            current = (cutoff.to_period("Q")-1).end_time.normalize()
        args.current_period = args.current_period or current.date().isoformat()
        args.previous_period = args.previous_period or (pd.Timestamp(args.current_period).to_period("Q")-1).end_time.date().isoformat()
        prev, curr = pd.Timestamp(args.previous_period), pd.Timestamp(args.current_period)
        if not prev.is_quarter_end or not curr.is_quarter_end or curr.to_period("Q")-1 != prev.to_period("Q") or curr.date() > as_of:
            raise ValueError("Use consecutive quarter-end dates no later than as-of")
        # MECHANICS: require both manager and security identifiers or neither; partial configuration is rejected.
        if bool(args.filer_cik) != bool(args.cusip):
            raise ValueError("Provide both --filer-cik and --cusip")
        if args.filer_cik:
            if not args.filer_cik.isdigit() or len(args.filer_cik) > 10:
                raise ValueError("CIK must contain 1-10 digits")
            # MECHANICS: zero-pad the CIK to 10 digits; formatting does not verify that it names the intended manager.
            args.filer_cik = args.filer_cik.zfill(10)
            # LIMITATION: uppercase/length/alphanumeric checks validate format, not that the CUSIP truly belongs to COIN.
            args.cusip = args.cusip.upper()
            if len(args.cusip) != 9 or not args.cusip.isalnum():
                raise ValueError("CUSIP must contain 9 alphanumeric characters")
    except ValueError as exc:
        parser.error(str(exc))
    # DATA FLOW: create one shared client so spacing, caching and freshness span all endpoint calls.
    DATA_CLIENT = MassiveClient(args.cache_dir, offline=args.offline, refresh=args.refresh,
                                full_refresh=args.full_refresh, debug=args.debug)
    if not args.offline and not args.json:
        # MECHANICS: acknowledge slower paced downloads without flooding the report.
        print("Preparing the report; API updates may take a few minutes. Use --debug for progress.", file=sys.stderr)
    # DATA FLOW: execute the connected analytics once; no background polling thread is started.
    report = run_pipeline(args)
    if args.json:
        print(json.dumps(report, indent=2, allow_nan=False))
    else:
        print_summary(report, debug=args.debug)


# MECHANICS: execute main only when this file is run; importing its functions does not start API downloads.
if __name__ == "__main__":
    main()
