import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gateway_burn', REPO / 'bin/gateway_burn.py')
burn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(burn)
FIXTURE = json.loads((REPO / 'tests/fixtures/gateway-report.json').read_text())
NOW = datetime(2026, 11, 22, 23, tzinfo=timezone.utc)


class GatewayBurnTests(unittest.TestCase):
    def test_parses_charged_cost_and_ignores_overlapping_noncaller_tags(self):
        models, callers = burn.parse_reports(FIXTURE['models'], FIXTURE['tags'])
        self.assertEqual(models['moonshotai/kimi-k3'], (Decimal('21.67'), 1059))
        self.assertEqual(models['byok/model'], (Decimal('0'), 2))
        self.assertEqual(callers, {'caller=multiprompt-verifier': (Decimal('0.75'), 5),
                                   'caller=plan-pick-worker': (Decimal('0.5'), 5),
                                   'untagged': (Decimal('21.67'), 1061)})

    def test_empty_report_is_zero_not_an_error(self):
        self.assertEqual(burn.parse_reports({'results': []}, {'results': []}),
                         ({}, {'untagged': (Decimal(0), 0)}))

    def test_missing_caller_tags_count_as_untagged_even_if_other_tags_exist(self):
        _, callers = burn.parse_reports(FIXTURE['models'], {'results': [
            {'tag': 'pr=42', 'total_cost': 22.92, 'request_count': 1071}]})
        self.assertEqual(callers, {'untagged': (Decimal('22.92'), 1071)})

    def test_malformed_or_inconsistent_reports_fail_instead_of_showing_zero(self):
        for bad in ({}, {'results': None}, {'results': [{}]}, {'results': [
                {'model': 'm', 'total_cost': 'NaN', 'request_count': 1}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                burn.parse_reports(bad, FIXTURE['tags'])
        with self.assertRaises(ValueError):
            burn.parse_reports({'results': []}, FIXTURE['tags'])

    def test_cycle_uses_previous_calendar_month_not_thirty_days(self):
        start, reset = burn.cycle_window(FIXTURE['credits'], NOW)
        self.assertEqual(start.isoformat(), '2026-11-05T08:00:00+00:00')
        self.assertEqual(reset.isoformat(), '2026-12-05T08:00:00+00:00')
        credits = {'token_pass': {'resets_at': '2026-03-31T08:00:00Z'}}
        self.assertEqual(burn.cycle_window(credits, datetime(2026, 3, 1, tzinfo=timezone.utc))[0].day, 28)
        credits['token_pass']['resets_at'] = '2027-01-05T08:00:00Z'
        self.assertEqual(burn.cycle_window(credits, datetime(2026, 12, 22, tzinfo=timezone.utc))[0].year, 2026)

    def test_expired_or_missing_cycle_is_not_silently_guessed(self):
        for credits in ({}, {'token_pass': {}}, {'token_pass': {'resets_at': 'bad'}}):
            with self.subTest(credits=credits), self.assertRaises(ValueError):
                burn.cycle_window(credits, NOW)
        with self.assertRaises(ValueError):
            burn.cycle_window(FIXTURE['credits'], datetime(2026, 12, 6, tzinfo=timezone.utc))

    def test_report_queries_both_groupings_for_same_cycle(self):
        calls = []

        def get(url, key):
            self.assertEqual(key, 'stub-private-key')
            calls.append(url)
            if url.endswith('/credits'):
                return FIXTURE['credits']
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query['start_date'], ['2026-11-05'])
            self.assertEqual(query['end_date'], ['2026-11-22'])
            self.assertNotIn('api_key_id', query)
            return FIXTURE['models' if query['group_by'] == ['model'] else 'tags']

        output = '\n'.join(burn.report_lines(get=get, key='stub-private-key', now=NOW))
        self.assertEqual(len(calls), 3)
        self.assertIn('moonshotai/kimi-k3: $21.670000', output)
        self.assertIn('caller=plan-pick-worker: $0.500000', output)
        self.assertIn('untagged: $21.670000', output)
        self.assertIn('UTC dates', output)
        self.assertIn('before the reset hour', output)
        self.assertNotIn('stub-private-key', output)

    def test_http_denial_is_visible_without_server_body_or_key(self):
        def get(url, key):
            if url.endswith('/credits'):
                return FIXTURE['credits']
            raise HTTPError(url, 403, 'stub-private-key', {}, None)

        output = '\n'.join(burn.report_lines(get=get, key='stub-private-key', now=NOW))
        self.assertIn('HTTP 403', output)
        self.assertIn('AI_GATEWAY_API_KEY', output)
        self.assertNotIn('stub-private-key', output)
        self.assertNotIn('$0.00', output)

    def test_environment_key_wins_without_running_pi(self):
        with mock.patch.dict(os.environ, {'AI_GATEWAY_API_KEY': 'stub-env-key'}), mock.patch.object(
                burn.subprocess, 'run') as run:
            self.assertEqual(burn.gateway_key(), 'stub-env-key')
        run.assert_not_called()

    def test_existing_report_has_gateway_only_mode_without_cpu_sampling(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run([sys.executable, str(REPO / 'tools/measure-burn.py'), '--gateway'],
                                    env={**os.environ, 'HOME': home, 'AI_GATEWAY_API_KEY': ''},
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Vercel report unavailable', result.stdout)
        self.assertNotIn('Traceback', result.stderr)
