"""Offline regression tests. All fixtures here are synthetic, never live results."""
import json
import os
import tempfile
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from datetime import datetime, timezone, timedelta
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock

import pandas as pd
import indicator as tool


class IndicatorTests(unittest.TestCase):
    def transaction(self, **changes):
        row = dict(issuer_cik=tool.COIN_ISSUER_CIK, record_type="transaction", security_type="non-derivative",
                   transaction_date="2026-05-01", transaction_code="P", form_type="4",
                   transaction_shares=10, transaction_price_per_share=20,
                   owner_cik="123", aff_10b5_one=None)
        row.update(changes)
        return row

    def insider(self, rows):
        return tool.analyze_insider_activity(pd.DataFrame(rows), "2026-03-31", "2026-06-30")

    def holding(self, period, shares, **changes):
        row = dict(period=period, shares_or_principal_amount=shares, cusip="TEST00001",
                   accession_number=period, form_type="13F-HR", put_call=None,
                   shares_or_principal_type="SH", filing_date="2026-08-01")
        row.update(changes)
        return row

    def test_quiet_and_negative_confirmation(self):
        self.assertEqual(tool.classify_market_state(1, 100, 1)["signal"], "Quiet Accumulation")
        self.assertEqual(tool.classify_market_state(-5, -100, -1)["signal"], "Negative Ownership Confirmation")
        self.assertEqual(tool.classify_market_state(None, 100, 1)["signal"], "Insufficient data")

    def test_values_and_date_exclusions(self):
        result = self.insider([self.transaction(), self.transaction(transaction_code="S", transaction_value=50),
                               self.transaction(transaction_date="2026-03-31"),
                               self.transaction(security_type="derivative")])
        self.assertEqual(result["net_insider_value"], 150)
        self.assertEqual(result["reported_10b5_1_flags"]["unknown"], 2)

    def test_unknown_price_is_not_zero(self):
        result = self.insider([self.transaction(transaction_price_per_share=None)])
        self.assertIsNone(result["net_insider_value"])
        self.assertEqual(result["unpriced_ps_rows"], 1)

    def test_amendment_blocks_net(self):
        self.assertIsNone(self.insider([self.transaction(form_type="4/A")])["net_insider_value"])

    def test_empty_is_missing(self):
        self.assertIsNone(self.insider([])["net_insider_value"])

    def test_live_security_type_alias(self):
        result = self.insider([self.transaction(security_type="non_derivative", transaction_code="S")])
        self.assertEqual(result["sales"]["rows"], 1)
        self.assertEqual(result["net_insider_value"], -200)

    def test_old_amended_holding_does_not_block_current_flows(self):
        result = self.insider([self.transaction(), self.transaction(form_type="4/A", record_type="holding", transaction_date=None)])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["net_insider_value"], 200)

    def test_reused_ticker_issuer_filter(self):
        df = pd.DataFrame([self.transaction(), self.transaction(issuer_cik="0001366340"), self.transaction(issuer_cik=None)])
        kept, scope = tool.issuer_records(df)
        self.assertEqual(len(kept), 1)
        self.assertEqual(scope["other_issuer_rows_excluded"], 1)
        self.assertEqual(scope["missing_issuer_rows_excluded"], 1)

    def test_purchase_tracker_filters_and_preserves_summary(self):
        df = pd.DataFrame([self.transaction(direct_or_indirect="D"),
                           self.transaction(direct_or_indirect="I", transaction_date="2026-06-01"),
                           self.transaction(transaction_code="S"),
                           self.transaction(transaction_code="A"),
                           self.transaction(transaction_code="M"),
                           self.transaction(security_type="derivative"),
                           self.transaction(transaction_date="2026-07-01")])
        normal = tool.analyze_insider_activity(df, "2026-03-31", "2026-06-30")
        tracked = tool.analyze_insider_activity(df, "2026-03-31", "2026-06-30", True)
        tracker = tracked.pop("p_purchase_tracker")
        self.assertEqual(normal, tracked)
        self.assertEqual(tracker["purchase_rows"], 2)
        self.assertEqual(tracker["direct_ownership_rows"], 1)
        self.assertEqual(tracker["indirect_ownership_rows"], 1)
        self.assertEqual(tracker["transactions"][0]["transaction_date"], "2026-06-01")
        self.assertEqual(tracker["transactions"][0]["value_source"], "reported_shares_times_price")

    def test_purchase_tracker_unknowns_and_amendments(self):
        df = pd.DataFrame([self.transaction(transaction_price_per_share=None, form_type="4/A")])
        tracker = tool.analyze_insider_activity(df, "2026-03-31", "2026-06-30", True)["p_purchase_tracker"]
        self.assertEqual(tracker["status"], "incomplete")
        self.assertEqual(tracker["unknown_ownership_rows"], 1)
        self.assertIsNone(tracker["transactions"][0]["value"])
        self.assertEqual(tracker["transactions"][0]["value_source"], "unavailable")
        empty = tool.analyze_insider_activity(pd.DataFrame(), "2026-03-31", "2026-06-30", True)
        self.assertEqual(empty["p_purchase_tracker"]["status"], "no_data")
        sales = tool.analyze_insider_activity(pd.DataFrame([self.transaction(transaction_code="S")]), "2026-03-31", "2026-06-30", True)
        self.assertEqual(sales["p_purchase_tracker"]["status"], "no_matching_purchases")

    def test_13f_options_excluded(self):
        df = pd.DataFrame([self.holding("2026-03-31", 100), self.holding("2026-06-30", 120),
                           self.holding("2026-06-30", 9999, put_call="CALL")])
        result = tool.analyze_13f(df, "2026-03-31", "2026-06-30", "TEST00001")
        self.assertEqual(result["share_change"], 20)
        self.assertAlmostEqual(result["change_pct"], 20)

    def test_13f_missing_and_amendment_fail(self):
        with self.assertRaises(ValueError):
            tool.institutional_snapshot(pd.DataFrame(), "2026-06-30", "TEST00001")
        with self.assertRaises(ValueError):
            tool.institutional_snapshot(pd.DataFrame([self.holding("2026-06-30", 10, form_type="13F-HR/A")]), "2026-06-30", "TEST00001")

    def test_new_position_undefined_percentage(self):
        df = pd.DataFrame([self.holding("2026-03-31", 100, cusip="OTHER0001"), self.holding("2026-06-30", 20)])
        result = tool.analyze_13f(df, "2026-03-31", "2026-06-30", "TEST00001")
        self.assertIsNone(result["change_pct"])
        self.assertEqual(result["position_state"], "new reported position")

    def test_pagination_and_auth(self):
        responses = [Mock(status_code=200, json=lambda: {"status": "OK", "results": [{"x": 1, "filing_date": "2026-09-01"}], "next_url": tool.FORM4_URL+"?cursor=two&apiKey=secret"}),
                     Mock(status_code=200, json=lambda: {"status": "OK", "results": [{"x": 2, "filing_date": "2026-09-02"}]})]
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "synthetic-test-key"}), patch.object(tool.requests, "get", side_effect=responses) as get, patch.object(tool, "DATA_CLIENT", tool.MassiveClient(min_interval=0)):
            self.assertEqual(len(tool.get_massive_data(tool.FORM4_URL, {"filing_date.lte": "2026-09-30"})), 2)
            self.assertIsNone(get.call_args.kwargs["params"])
            self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-test-key")
            self.assertNotIn("secret", get.call_args.args[0])

    def test_pipeline_offline(self):
        args = SimpleNamespace(previous_period="2026-03-31", current_period="2026-06-30", as_of="2026-09-01", filer_cik="0000000001", cusip="TEST00001")
        def fetch(url, params):
            if url == tool.FORM3_URL:
                return pd.DataFrame()
            if url == tool.FORM4_URL:
                return pd.DataFrame([self.transaction()])
            if url == tool.FORM13F_URL:
                return pd.DataFrame([self.holding("2026-03-31", 100), self.holding("2026-06-30", 120)])
            return pd.DataFrame([{"t": pd.Timestamp("2026-03-31", tz="America/New_York").timestamp()*1000, "c": 100},
                                 {"t": pd.Timestamp("2026-06-30", tz="America/New_York").timestamp()*1000, "c": 90}])
        with patch.object(tool, "get_massive_data", side_effect=fetch):
            report = tool.run_pipeline(args)
        self.assertEqual(report["classification"]["signal"], "Positive divergence")
        json.dumps(report, allow_nan=False)

    def baseline(self, shares=100):
        return dict(issuer_cik="issuer", owner_cik="123", security_title="Common Stock",
                    direct_or_indirect="D", security_type="non-derivative", form_type="3",
                    period_of_report="2025-01-01", shares_owned=shares, accession_number="baseline")

    def ledger_event(self, **changes):
        row = self.transaction(issuer_cik="issuer", security_title="Common Stock", direct_or_indirect="D",
                               transaction_acquired_disposed="A", shares_owned_following_transaction=110)
        row.update(changes)
        return row

    def test_baseline_ledger_and_purchase_ratio(self):
        result = tool.reconcile_baselines(pd.DataFrame([self.baseline()]),
                                         pd.DataFrame([self.ledger_event()]), "2026-06-30")
        account = result["accounts"][0]
        self.assertEqual(result["reconciled_accounts"], 1)
        self.assertEqual(account["latest_verified_shares"], 110)
        self.assertEqual(account["purchase_position_increases"][0]["position_increase_pct"], 10)

    def test_ledger_gap_and_same_day_fail_closed(self):
        baseline = pd.DataFrame([self.baseline()])
        for rows in ([self.ledger_event(shares_owned_following_transaction=200)],
                     [self.ledger_event(), self.ledger_event()]):
            result = tool.reconcile_baselines(baseline, pd.DataFrame(rows), "2026-06-30")
            self.assertEqual(result["reconciled_accounts"], 0)
            self.assertIsNone(result["accounts"][0]["change_pct"])

    def test_cluster_is_distinct_buyers_not_transaction_count(self):
        rows = [self.transaction(owner_cik=str(n), filing_date="2026-05-02", transaction_date=f"2026-05-0{n+1}") for n in range(3)]
        result = tool.purchase_behavior(pd.DataFrame(rows), "2026-03-31", "2026-06-30", {}, {})
        self.assertEqual(result["cluster_buying"]["windows"][0]["distinct_buyers"], 3)
        for row in rows:
            row["owner_cik"] = "same"
        result = tool.purchase_behavior(pd.DataFrame(rows), "2026-03-31", "2026-06-30", {}, {})
        self.assertEqual(result["cluster_buying"]["windows"], [])

    def test_holding_row_and_old_amendment_do_not_poison_current_net(self):
        result = self.insider([self.transaction(), self.transaction(record_type="holding", transaction_date=None),
                               self.transaction(form_type="4/A", transaction_date="2025-01-01")])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["net_insider_value"], 200)

    def test_all_five_states_and_missing(self):
        cases = [(-10, 1, 1, "Positive divergence"), (10, -1, -1, "Negative divergence"),
                 (10, 1, 1, "Confirmation"), (-10, -1, -1, "Confirmation"),
                 (1, 1, 1, "Quiet accumulation"), (10, 1, -1, "Mixed/neutral"),
                 (10, 1, None, "Confirmation")]
        for price, insider, shares, state in cases:
            result = tool.evidence_classification({"change_pct": price}, {"net_insider_value": insider}, {"share_change": shares}, {})
            self.assertEqual(result["signal"], state)

    def test_recurrence_requires_prior_observed_years(self):
        rows = [self.transaction(filing_date="2026-05-02")]
        for year in (2023, 2024, 2025):
            rows.append(self.transaction(transaction_date=f"{year}-05-01", filing_date=f"{year}-05-02"))
        result = tool.purchase_behavior(pd.DataFrame(rows), "2026-03-31", "2026-06-30", {}, {})
        self.assertIn("each of prior three years", result["who_bought"][0]["calendar_pattern"])

    def test_failed_form4_preserves_other_layers(self):
        args = SimpleNamespace(previous_period="2026-03-31", current_period="2026-06-30", as_of="2026-09-01", filer_cik=None, cusip=None)
        with patch.object(tool, "get_form3", return_value=pd.DataFrame()), \
             patch.object(tool, "get_form4", side_effect=ValueError("HTTP 403")), \
             patch.object(tool, "price_context", return_value={"status": "ok", "change_pct": 10}):
            report = tool.run_pipeline(args)
        self.assertEqual(report["form4"]["status"], "unavailable")
        self.assertEqual(report["purchase_behavior"]["status"], "unavailable")
        self.assertEqual(report["price"]["status"], "ok")
        self.assertEqual(report["classification"]["signal"], "Insufficient data")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = tool.MassiveClient(self.directory.name, min_interval=0)
        self.params = {"tickers": "COIN", "filing_date.gte": "2026-01-01", "filing_date.lte": "2026-09-30"}
        self.rows = [{"filing_date": "2026-09-01", "value": 1},
                     {"filing_date": "2026-09-29", "value": 2}]

    def seed(self):
        with patch.object(self.client, "download", return_value=self.rows):
            self.client.get(tool.FORM4_URL, self.params)

    def test_second_run_reuses_cache_without_key_or_request(self):
        self.seed()
        with patch.dict(os.environ, {}, clear=True), patch.object(self.client, "download") as download:
            result = self.client.get(tool.FORM4_URL, self.params)
        download.assert_not_called()
        self.assertEqual(len(result), 2)
        self.assertEqual(self.client.events[-1]["source"], "cache")

    def test_overlap_replaced_not_appended_and_amendments_preserved(self):
        self.seed()
        self.client.refresh = True
        new = [{"filing_date": "2026-09-29", "value": 3},
               {"filing_date": "2026-09-30", "form_type": "4/A", "value": 4}]
        with patch.object(self.client, "download", return_value=new) as download:
            result = self.client.get(tool.FORM4_URL, self.params)
        self.assertEqual(download.call_args.args[1]["filing_date.gte"], "2026-09-23")
        self.assertEqual(result.value.tolist(), [1, 3, 4])
        self.assertEqual(self.client.events[-1]["source"], "api_incremental")

    def test_failed_refresh_retains_previous_complete_snapshot(self):
        self.seed()
        saved = next(Path(self.directory.name).glob("*.json"))
        before = saved.read_bytes()
        self.client.refresh = True
        with patch.object(self.client, "download", side_effect=ValueError("HTTP 429")):
            result = self.client.get(tool.FORM4_URL, self.params)
        self.assertEqual(len(result), 2)
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(self.client.events[-1]["source"], "stale_cache")
        self.assertTrue(self.client.events[-1]["stale"])

    def test_offline_cold_cache_fails_and_later_asof_is_labeled(self):
        self.client.offline = True
        with self.assertRaisesRegex(ValueError, "No compatible"):
            self.client.get(tool.FORM4_URL, self.params)
        self.client.offline = False
        self.seed()
        self.client.offline = True
        self.client.get(tool.FORM4_URL, dict(self.params, **{"filing_date.lte": "2026-10-01"}))
        self.assertTrue(self.client.events[-1]["stale"])
        self.assertEqual(self.client.events[-1]["data_through"], "2026-09-30")

    def test_older_asof_does_not_use_future_snapshot(self):
        self.seed()
        self.client.offline = True
        with self.assertRaisesRegex(ValueError, "No compatible"):
            self.client.get(tool.FORM4_URL, dict(self.params, **{"filing_date.lte": "2026-09-01"}))

    def test_full_refresh_and_weekly_correction_sweep(self):
        self.seed()
        saved = next(Path(self.directory.name).glob("*.json"))
        payload = json.loads(saved.read_text())
        payload["full_refreshed_at"] = (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
        saved.write_text(json.dumps(payload))
        self.client.refresh = True
        with patch.object(self.client, "download", return_value=self.rows) as download:
            self.client.get(tool.FORM4_URL, self.params)
        self.assertEqual(download.call_args.args[1]["filing_date.gte"], "2026-01-01")
        self.assertEqual(self.client.events[-1]["source"], "api_full")

    def test_pacing_and_429_retry_after(self):
        retry = Mock(status_code=429, headers={"Retry-After": "90"})
        success = Mock(status_code=200, json=lambda: {"status": "OK", "results": []})
        client = tool.MassiveClient(min_interval=13)
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test"}), patch.object(tool.requests, "get", side_effect=[retry, success]), \
             patch.object(tool.time, "sleep") as sleep, patch.object(tool.time, "monotonic", return_value=100):
            client.request(tool.FORM4_URL, {})
        sleep.assert_any_call(90.0)
        sleep.assert_any_call(13)

    def test_partial_pagination_is_not_saved(self):
        page1 = Mock(status_code=200, json=lambda: {"status": "OK", "results": self.rows, "next_url": tool.FORM4_URL+"?cursor=two"})
        page2 = Mock(status_code=403)
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test"}), patch.object(tool.requests, "get", side_effect=[page1, page2]):
            with self.assertRaisesRegex(ValueError, "403"):
                self.client.get(tool.FORM4_URL, self.params)
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])

    def test_overdue_offline_snapshot_is_not_missing_coverage(self):
        self.seed()
        path = next(Path(self.directory.name).glob("*.json"))
        saved = json.loads(path.read_text())
        saved["refreshed_at"] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        saved["full_refreshed_at"] = saved["refreshed_at"]
        path.write_text(json.dumps(saved))
        self.client.offline = True
        self.client.get(tool.FORM4_URL, self.params)
        event = self.client.events[-1]
        self.assertTrue(event["refresh_due"])
        self.assertTrue(event["coverage_complete"])
        self.assertFalse(event["stale"])

    def test_corrupt_cache_offline_never_becomes_empty_success(self):
        self.seed()
        next(Path(self.directory.name).glob("*.json")).write_text("broken JSON")
        self.client.offline = True
        with self.assertRaisesRegex(ValueError, "No compatible"):
            self.client.get(tool.FORM4_URL, self.params)

    def test_invalid_json_and_wrong_rows_preserve_saved_snapshot(self):
        self.seed()
        self.client.refresh = True
        invalid = Mock(status_code=200)
        invalid.json.side_effect = ValueError("private server content")
        bad_rows = Mock(status_code=200, json=lambda: {"status": "OK", "results": {"bad": "shape"}})
        for response in (invalid, bad_rows):
            with patch.dict(os.environ, {"MASSIVE_API_KEY": "test"}), patch.object(tool.requests, "get", return_value=response):
                result = self.client.get(tool.FORM4_URL, self.params)
            self.assertEqual(len(result), 2)
            self.assertEqual(self.client.events[-1]["source"], "stale_cache")
            self.assertNotIn("private server content", self.client.events[-1]["refresh_error"])

    def test_rate_limit_exhaustion_is_bounded(self):
        response = Mock(status_code=429, headers={})
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test"}), patch.object(tool.requests, "get", return_value=response) as get, \
             patch.object(tool.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "429"):
                self.client.request(tool.FORM4_URL, {})
        self.assertEqual(get.call_count, 4)

    def test_network_failure_is_bounded_and_preserves_snapshot(self):
        self.seed()
        self.client.refresh = True
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test"}), patch.object(tool.requests, "get", side_effect=tool.requests.RequestException()) as get, \
             patch.object(tool.time, "sleep"):
            self.client.get(tool.FORM4_URL, self.params)
        self.assertEqual(get.call_count, 4)
        self.assertTrue(self.client.events[-1]["stale"])

    def test_cursor_loop_not_committed(self):
        response = Mock(status_code=200, json=lambda: {"status": "OK", "results": self.rows, "next_url": tool.FORM4_URL})
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test"}), patch.object(tool.requests, "get", return_value=response):
            with self.assertRaisesRegex(ValueError, "Repeated pagination"):
                self.client.get(tool.FORM4_URL, self.params)
        self.assertEqual(list(Path(self.directory.name).glob("*.json")), [])

    def test_progress_hidden_unless_debug(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.client.log("hidden diagnostic")
        self.assertEqual(output.getvalue(), "")
        self.client.debug = True
        with redirect_stderr(output):
            self.client.log("visible diagnostic")
        self.assertIn("visible diagnostic", output.getvalue())


class SetupAndReadingTests(unittest.TestCase):
    def test_separate_reading_does_not_fabricate_institutions(self):
        price, insider = {"change_pct": -16}, {"net_insider_value": -100}
        limited = tool.insider_price_reading(price, insider)
        combined = tool.evidence_classification(price, insider, {"status": "not_configured"}, {})
        self.assertEqual(limited["signal"], "Confirmation")
        self.assertEqual(limited["direction"], "downward")
        self.assertEqual(combined["signal"], "Confirmation")
        self.assertEqual(combined["analysis_status"], "partial")
        self.assertEqual(combined["combined_signal"], "Insufficient data")
        self.assertIsNone(combined["evidence"]["selected_manager_share_change"])

    def test_partial_states_and_missing_reasons(self):
        for price, net, expected in [(-10, 1, "Positive divergence"), (10, -1, "Negative divergence"),
                                     (1, 1, "Quiet accumulation"), (1, -1, "Mixed/neutral"),
                                     (-10, -1, "Confirmation"), (10, 0, "Mixed/neutral")]:
            for status in ("not_configured", "unavailable", "incomplete"):
                result = tool.evidence_classification({"change_pct": price}, {"net_insider_value": net},
                    {"status": status, "reason": "Unresolved security mapping", "share_change": 99}, {})
                self.assertEqual(result["signal"], expected)
                self.assertEqual(result["analysis_status"], "partial")
                self.assertEqual(result["institutional_missing_reason"], "Unresolved security mapping")
                self.assertIsNone(result["evidence"]["selected_manager_share_change"])

    def test_available_institutions_still_change_combined_classification(self):
        result = tool.evidence_classification({"change_pct": -10}, {"net_insider_value": -100},
                                              {"status": "ok", "share_change": 20}, {})
        self.assertEqual(result["signal"], "Mixed/neutral")
        self.assertEqual(result["analysis_status"], "complete_for_selected_scope")

    def test_missing_insider_remains_unknown(self):
        self.assertEqual(tool.insider_price_reading({"change_pct": 5}, {})["signal"], "Insufficient data")

    def test_setup_roundtrip_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            with patch("builtins.input", side_effect=["123", "TEST00001"]), redirect_stdout(io.StringIO()):
                tool.configure_institutional_settings(path)
            saved = tool.load_institutional_settings(path)
            self.assertEqual(saved, {"ticker": "COIN", "filer_cik": "0000000123", "cusip": "TEST00001"})
            self.assertEqual(set(json.loads(path.read_text())), {"ticker", "filer_cik", "cusip"})

    def test_invalid_setup_does_not_overwrite_existing_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('original')
            with patch("builtins.input", side_effect=["bad", "TEST00001"]), redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    tool.configure_institutional_settings(path)
            self.assertEqual(path.read_text(), 'original')

    def test_wrong_ticker_settings_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"ticker": "OTHER", "filer_cik": "123", "cusip": "TEST00001"}))
            with self.assertRaises(ValueError):
                tool.load_institutional_settings(path)


class ReportTests(unittest.TestCase):
    def report(self):
        args = SimpleNamespace(previous_period="2026-03-31", current_period="2026-06-30", as_of="2026-09-30", filer_cik=None, cusip=None)
        row = dict(issuer_cik=tool.COIN_ISSUER_CIK, record_type="transaction", security_type="non_derivative",
                   transaction_date="2026-05-01", filing_date="2026-05-03", transaction_code="S",
                   form_type="4", transaction_value=7745886.0958, owner_cik="123")
        with patch.object(tool, "get_form3", return_value=pd.DataFrame()), \
             patch.object(tool, "get_form4", return_value=pd.DataFrame([row])), \
             patch.object(tool, "price_context", return_value={"status": "ok", "change_pct": -16.27627}):
            return tool.run_pipeline(args)

    def test_readable_report_rounds_and_hides_diagnostics(self):
        report = self.report()
        output = io.StringIO()
        with redirect_stdout(output):
            tool.print_summary(report)
        text = output.getvalue()
        self.assertIn("CONCLUSION: Confirmation (downward) | PARTIAL ANALYSIS", text)
        self.assertIn("Missing: 13F quarterly comparison", text)
        self.assertIn("$7,745,886.10", text)
        self.assertIn("-16.28%", text)
        self.assertNotIn("TECHNICAL DIAGNOSTICS", text)
        self.assertNotIn("NOT YET AVAILABLE", text)
        self.assertNotIn("7745886.0958", text)

    def test_missing_and_incomplete_layers_are_explicit(self):
        for layer, expected in (({"status": "unavailable"}, "Unavailable"),
                                ({"status": "incomplete"}, "Incomplete"),
                                ({"status": "not_configured"}, "Not configured"),
                                ({"status": "ok"}, "Using saved data")):
            self.assertEqual(tool.layer_label(layer, {"source": "offline_cache"}), expected)

    def test_failed_refresh_cannot_look_like_current_data(self):
        report = self.report()
        report["data_freshness"] = [{"endpoint": "/stocks/filings/vX/form-4", "source": "stale_cache",
            "refreshed_at": "2026-09-29T00:00:00+00:00", "refresh_error": "HTTP 429",
            "coverage_complete": False, "data_through": "2026-09-29", "stale": True}]
        report["classification"]["uses_stale_data"] = True
        output = io.StringIO()
        with redirect_stdout(output):
            tool.print_summary(report)
        text = output.getvalue()
        self.assertIn("UPDATE WARNING", text)
        self.assertIn("refresh failed", text)
        self.assertIn("2026-09-29", text)

    def test_debug_exposes_audit_metadata(self):
        output = io.StringIO()
        with redirect_stdout(output):
            tool.print_summary(self.report(), debug=True)
        self.assertIn("TECHNICAL DIAGNOSTICS", output.getvalue())


if __name__ == "__main__":
    unittest.main()
