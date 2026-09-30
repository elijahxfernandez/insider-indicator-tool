"""Web-boundary regression tests. No real API calls or user credentials."""
import base64
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import AnalysisService, create_app, validate_parameters

PARAMETERS = dict(previous_period='2025-12-31', current_period='2026-03-31', as_of='2026-06-01')
HEADERS = {'X-Requested-With':'ownership-dashboard'}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {'MASSIVE_API_KEY':'', 'DASHBOARD_PASSWORD':'', 'RENDER':''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = AnalysisService(self.directory.name, runner=lambda *args: {'classification':{'signal':'Mixed/neutral'}})
        self.app = create_app(self.service)
        self.client = self.app.test_client()

    def test_empty_state_does_not_invent_data(self):
        state = self.client.get('/api/state').get_json()
        self.assertIsNone(state['report'])
        self.assertFalse(state['api_configured'])
        self.assertEqual(self.client.get('/api/report').status_code, 404)
        with self.client.get('/') as response:
            self.assertEqual(response.status_code, 200)

    def test_missing_api_key_stops_refresh(self):
        response = self.client.post('/api/refresh', json=PARAMETERS, headers=HEADERS)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.service.job['status'], 'idle')

    def test_review_auth_and_health_check(self):
        with patch.dict(os.environ, {'DASHBOARD_PASSWORD':'test-password', 'MASSIVE_API_KEY':'secret-fixture'}):
            self.assertEqual(self.client.get('/healthz').status_code, 200)
            self.assertEqual(self.client.get('/api/state').status_code, 401)
            auth = 'Basic ' + base64.b64encode(b'reviewer:test-password').decode()
            response = self.client.get('/api/state', headers={'Authorization':auth})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(b'secret-fixture', response.data)
            self.assertNotIn(b'test-password', response.data)

    def test_render_fails_closed_without_review_password(self):
        with patch.dict(os.environ, {'RENDER':'true'}):
            self.assertEqual(self.client.get('/').status_code, 503)

    def test_cross_site_and_missing_header_rejected(self):
        self.assertEqual(self.client.post('/api/refresh', json=PARAMETERS).status_code, 403)
        self.assertEqual(self.client.post('/api/refresh', json=PARAMETERS,
            headers={**HEADERS,'Origin':'https://other.example'}).status_code, 403)

    def test_bad_periods_and_unsupported_tickers(self):
        for changes in ({'ticker':'MSFT'},{'current_period':'2026-03-30'},
                        {'previous_period':'2025-09-30'},{'as_of':'2026-01-01'}, {'as_of':[]}, {'as_of':'2999-01-01'}):
            with self.assertRaises(ValueError):
                validate_parameters({**PARAMETERS,**changes})

    def test_background_result_export_and_restart(self):
        with patch.dict(os.environ, {'MASSIVE_API_KEY':'test-only'}):
            response = self.client.post('/api/refresh',json=PARAMETERS,headers=HEADERS)
            self.assertEqual(response.status_code, 202)
            for _ in range(100):
                if self.service.snapshot()['job']['status'] != 'running':
                    break
                time.sleep(.01)
            self.assertEqual(self.service.snapshot()['job']['status'], 'done')
            exported = self.client.get('/api/report')
            self.assertEqual(exported.status_code, 200)
            self.assertEqual(exported.get_json()['classification']['signal'],'Mixed/neutral')
            self.assertIn('attachment',exported.headers['Content-Disposition'])
            restored = AnalysisService(self.directory.name)
            self.assertEqual(restored.report, self.service.report)
            self.assertEqual(self.client.post('/api/refresh',json=PARAMETERS,headers=HEADERS).status_code,409)

    def test_duplicate_jobs_are_not_queued(self):
        release = threading.Event()
        self.service.runner = lambda *args: release.wait(2) or None
        try:
            self.assertTrue(self.service.start(PARAMETERS)[0])
            self.assertFalse(self.service.start(PARAMETERS)[0])
        finally:
            release.set()
            for _ in range(100):
                if self.service.snapshot()['job']['status'] != 'running': break
                time.sleep(.01)

    def test_failed_job_retains_previous_report_and_hides_exception(self):
        self.service.report = {'classification':{'signal':'Confirmation'}}
        def fail(*args):
            raise ValueError('private-key-must-not-leak')
        self.service.runner = fail
        self.service._work(PARAMETERS)
        state = self.service.snapshot()
        self.assertEqual(state['job']['status'],'error')
        self.assertEqual(state['report']['classification']['signal'],'Confirmation')
        self.assertNotIn('private-key-must-not-leak',str(state))


if __name__ == '__main__':
    unittest.main()
