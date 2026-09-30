# Ownership · Insider intelligence

A simple research dashboard that turns Massive filings and price data into an
inspectable ownership analysis. Built with Python, pandas, Flask, and a responsive
HTML/CSS/JavaScript front end. No AI layer or trading execution.

## What a reviewer can do

- Run COIN analysis for consecutive quarters and a filing cutoff.
- Read the classification first, with a prominent **partial analysis** label if 13F is missing.
- Inspect Form 3 baseline context, Form 4 purchases/sales, and seven P-purchase evidence angles.
- Inspect source freshness, unavailable inputs, and reconciliation limitations.
- Export the full report as JSON.

The front end uses the existing Python results. It does not calculate a different
signal or fill missing values. There are no invented prices, charts, holdings, or
sample results in the shipped app.

## Run locally (Windows / PowerShell)

Open a terminal in this project folder. Your existing virtual environment is fine:

```powershell
python -m pip install -r requirements.txt
```

Set the key privately in that terminal. Run the first line, enter the key at the
prompt, then run the remaining lines. Do not replace the empty quotes:

```powershell
$secret = Read-Host "Massive API key" -AsSecureString
$env:MASSIVE_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
Remove-Variable secret
python app.py
```

Open **http://127.0.0.1:8000** and select **Refresh analysis**. Retrieval can take
several minutes because requests are paced. The screen stays responsive and
shows progress. Closing the terminal stops the local server.

Without a key, the dashboard still opens and explains how to configure the
server. It does not substitute demo values. `.env.example` documents variables;
the app does not automatically load `.env` files.

The command-line tool is also included:

```powershell
python indicator.py --refresh
python indicator.py --refresh --track-purchases --json
```

## Classification rules

Price changes within an absolute 2% band are treated as flat. Insider direction
uses the sign of net reported P-purchase value minus S-sale value. An available
institutional comparison uses the sign of one selected manager's reported share
change. Exactly +2% and -2% are directional, not flat.

| State | Full selected-manager reading |
| --- | --- |
| Positive divergence | Price down; insider net buying and institutional share change positive |
| Negative divergence | Price up; insider net selling and institutional share change negative |
| Confirmation | Price and both ownership directions agree, upward or downward |
| Quiet accumulation | Flat price; both ownership directions positive |
| Mixed/neutral | Other usable combinations, including disagreement or unchanged evidence |

When 13F is unavailable, the same price/insider relationship produces a **partial**
state. Institutional evidence remains `null`; it is neither zero nor neutral.
If price or usable insider net is missing, the result is **Insufficient data**.
Adding 13F later can change the conclusion.

Form 3 and P-purchase behavior provide supporting context rather than extra votes.
P activity is already included in Form 4 net activity and must not be counted twice.
The states describe observed alignment, not a probability of future returns.

## Institutional setup is optional

The current backend remains **COIN-only and single-manager**. The web app has no
automatic institution discovery yet. To include an institutional comparison, set
both server environment variables:

| Variable | Meaning |
| --- | --- |
| `INSTITUTIONAL_CIK` | Investing firm's SEC filer CIK, not Coinbase's issuer CIK |
| `SECURITY_CUSIP` | Verified nine-character CUSIP of the Coinbase security being compared |

If either is missing, institutions remain missing and the report can still show a
partial classification. Existing CLI `insider_settings.json` is not automatically
read by the web adapter. The UI never asks for an API key or exposes it.

## Deploy on Render

The included `render.yaml` follows [Render's Flask deployment guide](https://render.com/docs/deploy-flask)
and [Blueprint configuration](https://render.com/docs/blueprint-spec).

1. Publish this folder as a GitHub repository. Keep `app.py`, `indicator.py`,
   `requirements.txt`, and `render.yaml` at the repository root.
2. In Render, choose **New → Blueprint** and connect that repository.
3. Enter `MASSIVE_API_KEY` and a separate `DASHBOARD_PASSWORD` in Render's secret
   environment settings. The password protects access to your paid data allowance.
4. Deploy. Sign in to the hosted app with username **reviewer** and your chosen
   dashboard password. Share the app URL with your supervisor and communicate
   that separate password privately. Never share the Massive API key.

If creating a Web Service manually instead of a Blueprint, use:

```text
Build: pip install -r requirements.txt
Start: gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT --timeout 120
Health check: /healthz
Python: 3.12.10
```

**Keep one worker and one service instance.** The backend uses a process-global
client and the web wrapper runs one background job at a time. Duplicate refreshes
are rejected, with a minimum minute between starts. This is a small reviewer app,
not a distributed job system. The last report remains visible while a new one runs.

Free Render storage is ephemeral: cached data and the last report may disappear
on restart/redeploy. An interrupted analysis must be restarted. A persistent disk
and durable job queue can be added later if needed; neither is required to inspect
the current version. The included free-plan configuration creates no paid disk.

## Public-repository boundaries

Commit source and tests only. `.gitignore` excludes credentials, `.env` files,
local API snapshots, runtime reports, virtual environments, and manager settings.
The public repository contains no downloaded Massive datasets. The hosted review
app requires a password on Render. API calls originate on the server.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The suite covers classification states, partial results, issuer identity filtering,
unknown-versus-zero behavior, API pagination/retries, caching, web authentication,
input validation, duplicate refresh prevention, exports, and failed-job retention.
HTTP responses in tests are mocked; passing tests does not establish live API
entitlements or complete institutional coverage.

## Structure

```text
app.py              Web API, single-job refresh service, reviewer access
indicator.py        Existing commented analytics and CLI
static/index.html   Dashboard layout
static/style.css    Responsive design
static/app.js       Report rendering and refresh polling
tests/              Backend and web-boundary regression tests
render.yaml         Render deployment configuration
```

## Next development steps

- Verified issuer/security mappings for the Magnificent Seven.
- Multi-manager 13F discovery, quarter coverage, and overlap-aware comparisons.
- Amendment and joint-owner reconciliation; more complete baseline ledgers.
- Review of magnitude, role, and historical context with the analyst.
- Investor-profile guidance and a grounded question-answering layer, separately
  from the current descriptive classifier.

These are remaining work, not features implied by the interface. No predictive
score, backtest, personalized advice, or chatbot is implemented in this version.
