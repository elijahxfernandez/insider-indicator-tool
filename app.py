"""Small web adapter around the unchanged ownership-analysis pipeline.

One process / one analysis at a time preserves the backend's global client and
request pacing. This is a supervisor-review app, not a multi-tenant job service.
"""
import calendar
import hmac
import json
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request

ROOT = Path(__file__).resolve().parent


def default_dates():
    # Match the CLI: latest quarter with the normal 45-day filing allowance.
    today = date.today()
    cutoff = today - timedelta(days=45)
    month = ((cutoff.month - 1) // 3 + 1) * 3
    end = date(cutoff.year, month, calendar.monthrange(cutoff.year, month)[1])
    if end > cutoff:
        end = date(end.year, end.month - 2, 1) - timedelta(days=1)
    previous = date(end.year, end.month - 2, 1) - timedelta(days=1)
    return dict(as_of=today.isoformat(), previous_period=previous.isoformat(), current_period=end.isoformat())


def validate_parameters(payload):
    # Only dates and the currently supported ticker enter the pipeline.
    # Browser input cannot change paths, API endpoints, credentials, or commands.
    if not isinstance(payload, dict) or payload.get('ticker', 'COIN') != 'COIN':
        raise ValueError('This version supports COIN only.')
    result = {}
    for name, default in default_dates().items():
        value = payload.get(name, default)
        if not isinstance(value, str):
            raise ValueError('Use dates in YYYY-MM-DD format.')
        result[name] = date.fromisoformat(value)
    previous, current, as_of = (result[k] for k in ('previous_period', 'current_period', 'as_of'))
    for value in (previous, current):
        if value.month not in (3, 6, 9, 12) or value.day != calendar.monthrange(value.year, value.month)[1]:
            raise ValueError('Choose consecutive calendar quarter-end dates.')
    if current.year * 12 + current.month - (previous.year * 12 + previous.month) != 3:
        raise ValueError('Choose consecutive calendar quarter-end dates.')
    if not previous < current <= as_of <= date.today():
        raise ValueError('Quarter ends must precede the filing cutoff; future dates are not supported.')
    return {key: value.isoformat() for key, value in result.items()}


def run_analysis(parameters, progress, data_dir):
    # Import the existing backend, without changing its analytics or CLI behavior.
    import indicator

    class ReportingClient(indicator.MassiveClient):
        def log(self, message):
            # Backend log messages contain endpoint progress, not credentials.
            # Keep detail server-side; publish a short human-readable phase.
            message = str(message)
            if 'form-3' in message:
                progress('Reading initial ownership filings')
            elif 'form-4' in message:
                progress('Reading insider transactions and P purchases')
            elif '13-F' in message:
                progress('Comparing institutional holdings')
            elif '/aggs/' in message:
                progress('Reading price context')
            elif '429' in message or 'retry' in message.lower():
                progress('Waiting for the data provider; retrieval will retry')

    # Optional identifiers remain server settings. Missing 13F stays missing.
    cik, cusip = os.getenv('INSTITUTIONAL_CIK'), os.getenv('SECURITY_CUSIP')
    if cik and cusip:
        checked = indicator.validate_institutional_settings(cik, cusip)
        cik, cusip = checked['filer_cik'], checked['cusip']
    else:
        cik = cusip = None
    indicator.DATA_CLIENT = ReportingClient(data_dir / 'cache', refresh=True, debug=True)
    args = SimpleNamespace(**parameters, filer_cik=cik, cusip=cusip,
                           track_purchases=True, cluster_days=30, cluster_buyers=3)
    return indicator.run_pipeline(args)


class AnalysisService:
    """Serialize refreshes and retain the previous report if a refresh fails."""
    def __init__(self, data_dir, runner=run_analysis):
        self.data_dir, self.runner = Path(data_dir), runner
        self.lock = threading.Lock()
        self.last_start = None
        self.report = None
        self.report_at = None
        self.job = dict(status='idle', message='Ready when you are')
        # This local runtime file is never checked into GitHub.
        try:
            saved = json.loads((self.data_dir / 'latest-report.json').read_text(encoding='utf-8'))
            if isinstance(saved.get('report'), dict) and 'classification' in saved['report']:
                self.report, self.report_at = saved['report'], saved.get('generated_at')
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def snapshot(self):
        with self.lock:
            return dict(job=dict(self.job), report=self.report, generated_at=self.report_at)

    def progress(self, message):
        with self.lock:
            self.job['message'] = message

    def start(self, parameters):
        with self.lock:
            if self.job['status'] == 'running':
                return False, 'An analysis is already running. Its progress is shown below.'
            if self.last_start is not None and time.monotonic() - self.last_start < 60:
                return False, 'Please wait one minute between refresh starts to protect your API allowance.'
            self.last_start = time.monotonic()
            self.job = dict(id=uuid.uuid4().hex, status='running', message='Connecting to Massive', parameters=parameters)
        threading.Thread(target=self._work, args=(parameters,), daemon=True).start()
        return True, None

    def _work(self, parameters):
        try:
            report = self.runner(parameters, self.progress, self.data_dir)
            if not isinstance(report, dict) or 'classification' not in report:
                raise ValueError('Invalid report')
            generated_at = datetime.now(timezone.utc).isoformat()
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = dict(report=report, generated_at=generated_at)
            temporary = self.data_dir / 'latest-report.tmp'
            temporary.write_text(json.dumps(payload, allow_nan=False), encoding='utf-8')
            temporary.replace(self.data_dir / 'latest-report.json')
            with self.lock:
                self.report, self.report_at = report, generated_at
                self.job.update(status='done', message='Analysis ready')
        except Exception:
            # Do not expose exception strings, request URLs, environment values,
            # or API credentials in the browser. Layer errors remain in reports.
            with self.lock:
                self.job.update(status='error', message='Analysis could not finish. Check server configuration and retry. Your previous report is retained.')


def create_app(service=None):
    app = Flask(__name__, static_folder='static')
    app.config['MAX_CONTENT_LENGTH'] = 4096
    service = service or AnalysisService(os.getenv('DATA_DIR', str(ROOT / '.runtime')))
    app.extensions['analysis_service'] = service

    @app.before_request
    def protect_review():
        if request.path == '/healthz':
            return None
        password = os.getenv('DASHBOARD_PASSWORD', '')
        # Require a private reviewer password on Render; optional on localhost.
        if os.getenv('RENDER') and not password:
            return jsonify(error='Set DASHBOARD_PASSWORD in the server environment.'), 503
        if password:
            auth = request.authorization
            if not auth or auth.username != 'reviewer' or not hmac.compare_digest(auth.password or '', password):
                return Response('Reviewer sign-in required', 401, {'WWW-Authenticate': 'Basic realm="Ownership review"'})
        if request.method == 'POST':
            # A custom same-origin header prevents cross-site refresh requests.
            if request.headers.get('X-Requested-With') != 'ownership-dashboard':
                return jsonify(error='Refresh from this dashboard.'), 403
            origin = request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error='Cross-site refresh is not allowed.'), 403

    @app.after_request
    def response_headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/healthz')
    def health():
        return jsonify(status='ok')

    @app.get('/api/state')
    def state():
        return jsonify(**service.snapshot(), defaults=default_dates(),
                       api_configured=bool(os.getenv('MASSIVE_API_KEY')),
                       supported_tickers=['COIN'])

    @app.post('/api/refresh')
    def refresh():
        if not os.getenv('MASSIVE_API_KEY'):
            return jsonify(error='MASSIVE_API_KEY is missing on the server. Add it to the terminal environment or Render settings, then retry.'), 503
        try:
            parameters = validate_parameters(request.get_json(silent=True))
        except (TypeError, ValueError):
            return jsonify(error='Choose COIN and consecutive quarter ends, with a filing cutoff no later than today.'), 400
        started, error = service.start(parameters)
        return (jsonify(status='running'), 202) if started else (jsonify(error=error), 409)

    @app.get('/api/report')
    def download_report():
        report = service.snapshot()['report']
        if report is None:
            return jsonify(error='Run an analysis first.'), 404
        return Response(json.dumps(report, indent=2, allow_nan=False), mimetype='application/json',
                        headers={'Content-Disposition': 'attachment; filename="COIN-ownership-report.json"'})

    return app


app = create_app()

if __name__ == '__main__':
    # Waitress works on Windows. Render uses the single-worker Gunicorn command.
    from waitress import serve
    print('Ownership dashboard: http://127.0.0.1:8000', flush=True)
    serve(app, host='127.0.0.1', port=int(os.getenv('PORT', '8000')), threads=4)
