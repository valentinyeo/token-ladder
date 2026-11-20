import copy
import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from _load import tl

NOW = 1793040000


def history():
    rows = []
    for tick in range(289):
        hours = tick / 12
        rows.append({
            'ts': NOW - 86400 + tick * 300,
            'claude_week': 28 + min(hours, 23) * 10 / 23 + max(hours - 23, 0) * 2,
            'claude_5h': (42 + tick) % 60,
            'codex_week': 5 + min(hours, 23) * 4.5 / 23 + max(hours - 23, 0) * 0.5,
        })
    return rows


def account(week, gone, short=None):
    return {
        'week': {'used': week, 'time_gone': gone, 'resets_at': NOW + 168 * 3600 * (1 - gone / 100)},
        'five_hour': {'used': short, 'time_gone': 50 if short is not None else None,
                      'resets_at': NOW + 2.5 * 3600 if short is not None else None},
    }


class UsagePaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name)
        for name, value in (('STATE', str(self.state)), ('STATUS', str(self.state / 'status.json'))):
            patcher = patch.object(tl, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def collect(self, rows, claude=None, codex=None, config=None):
        claude = claude or account(40, 20, 30)
        codex = codex or account(10, 50)
        (self.state / 'history.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        with ExitStack() as stack:
            for name, value in (
                ('load_config', dict(config or {}, publish=False)), ('decide', ('codex', 'fixture', [])),
                ('read_codex', codex), ('read_openrouter', {}),
                ('claude_email', None), ('codex_email', None), ('read_override', None),
                ('read_reset_feed', {}), ('read_onepassword', {}), ('dashboard_url', 'fixture'),
                ('record_resources', {}), ('burn_plan', {}), ('current_pools', {}),
                ('notify_pace', []), ('render_dashboard', ''),
                ('mirror', None), ('push_usage', False), ('due', False), ('mode_line', 'fixture'),
            ):
                stack.enter_context(patch.object(tl, name, return_value=value))
            stack.enter_context(patch.object(tl, 'read_claude', side_effect=lambda *args: copy.deepcopy(claude)))
            stack.enter_context(patch.object(tl.time, 'time', return_value=NOW))
            stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
            tl.collect()
        return json.loads((self.state / 'status.json').read_text())

    def test_collect_writes_measured_pace_for_each_plan_window(self):
        status = self.collect(history())
        expected = (
            ('claude', 'week', 2, 12, 2, 'above', 30),
            ('claude', 'five_hour', 12, 283, 0.6, 'below', 70 / 12),
            ('codex', 'week', 0.5, 5, 0.2, 'below', 180),
        )
        for provider, window, hourly, daily, ratio, verdict, empty in expected:
            with self.subTest(provider=provider, window=window):
                bar = status[provider][window]
                self.assertIn('pace', bar)
                pace = bar['pace']
                self.assertAlmostEqual(pace['per_hour'], hourly, places=3)
                self.assertAlmostEqual(pace['per_day'], daily, places=3)
                self.assertAlmostEqual(pace['ratio'], ratio, places=3)
                self.assertEqual(pace['verdict'], verdict)
                self.assertAlmostEqual(pace['empty_hours'], empty, places=3)
        latest = json.loads((self.state / 'history.jsonl').read_text().splitlines()[-1])
        self.assertEqual(latest['accounts']['claude']['five_hour']['used'], 30)
        self.assertEqual(latest['accounts']['claude']['five_hour']['resets_at'], NOW + 9000)
        self.assertEqual(latest['claude_week'], 40)

    def test_missing_history_is_unknown_not_zero(self):
        pace = self.collect([])['claude']['week']['pace']
        self.assertIsNone(pace['per_hour'])
        self.assertIsNone(pace['per_day'])
        self.assertIsNone(pace['empty_hours'])
        self.assertEqual(pace['verdict'], 'above')

    def test_missing_value_does_not_become_idle_burn(self):
        for value in (None, False, 'unavailable'):
            with self.subTest(value=value):
                rows = history()
                rows[-4]['claude_week'] = value
                pace = self.collect(rows)['claude']['week']['pace']
                self.assertIsNone(pace['per_hour'])
                self.assertIsNone(pace['per_day'])

    def test_missing_window_field_is_unknown(self):
        rows = history()
        del rows[-4]['claude_week']
        pace = self.collect(rows)['claude']['week']['pace']
        self.assertIsNone(pace['per_hour'])
        self.assertIsNone(pace['per_day'])

    def test_gap_outside_last_hour_only_hides_daily_burn(self):
        rows = history()
        del rows[150:153]
        pace = self.collect(rows)['claude']['week']['pace']
        self.assertEqual(pace['per_hour'], 2)
        self.assertIsNone(pace['per_day'])

    def test_no_baseline_for_full_hour_is_unknown(self):
        pace = self.collect(history()[-6:])['claude']['week']['pace']
        self.assertIsNone(pace['per_hour'])
        self.assertIsNone(pace['per_day'])

    def test_known_reset_counts_new_usage_not_expired_quota(self):
        for before in (1, 95):
            with self.subTest(before=before):
                rows = []
                for tick in range(13):
                    ts = NOW - 3600 + tick * 300
                    reset = ts >= NOW - 1800
                    rows.append({'ts': ts, 'accounts': {'claude': {'five_hour': {
                        'used': 3 if reset else before,
                        'resets_at': NOW + 16200 if reset else NOW - 1800,
                    }}}})
                claude = account(40, 20, 3)
                claude['five_hour'].update(time_gone=10, resets_at=NOW + 16200)
                pace = self.collect(rows, claude=claude)['claude']['five_hour']['pace']
                self.assertEqual(pace['per_hour'], 3)
                self.assertIsNone(pace['per_day'])
                self.assertAlmostEqual(pace['empty_hours'], 97 / 3, places=3)

    def test_decrease_without_reset_is_unknown(self):
        claude = account(5, 20, 30)
        rows = [{'ts': NOW - 3600 + tick * 300, 'accounts': {'claude': {'week': {
            'used': 10 if tick < 6 else 5, 'resets_at': claude['week']['resets_at'],
        }}}} for tick in range(13)]
        pace = self.collect(rows, claude=claude)['claude']['week']['pace']
        self.assertIsNone(pace['per_hour'])
        self.assertIsNone(pace['empty_hours'])

    def test_stale_account_keeps_unknowns_in_status_and_history(self):
        claude = account(40, 20, 30)
        claude['error'] = 'collector unavailable'
        status = self.collect(history(), claude=claude)
        pace = status['claude']['week']['pace']
        self.assertEqual(pace['verdict'], 'unknown')
        self.assertIsNone(pace['ratio'])
        self.assertIsNone(pace['per_hour'])
        latest = json.loads((self.state / 'history.jsonl').read_text().splitlines()[-1])
        self.assertIsNone(latest['accounts']['claude']['week']['used'])

    def test_extra_plan_uses_its_own_history(self):
        rows = history()
        for tick, row in enumerate(rows):
            row['accounts'] = {'extra': {'week': {
                'used': 34 + tick / 288 * 6,
                'resets_at': NOW + 168 * 3600 * 0.8,
            }}}
        status = self.collect(rows, config={'extra_claude_accounts': [
            {'id': 'extra', 'label': 'Secondary', 'credentials': 'fixture'},
        ]})
        extra = next(a for a in status['accounts'] if a['id'] == 'extra')
        self.assertEqual(extra['week']['pace']['per_hour'], 0.25)
        self.assertEqual(extra['week']['pace']['per_day'], 6)
        self.assertEqual(status['claude']['week']['pace']['per_hour'], 2)

    def test_zero_burn_differs_from_missing_history_and_empty_quota(self):
        for used, empty in ((40, None), (100, 0)):
            with self.subTest(used=used):
                rows = history()
                for row in rows:
                    row['claude_week'] = used
                pace = self.collect(rows, claude=account(used, 20, 30))['claude']['week']['pace']
                self.assertEqual(pace['per_hour'], 0)
                self.assertEqual(pace['per_day'], 0)
                self.assertEqual(pace['empty_hours'], empty)

    def test_ratio_verdict_and_unknown_elapsed_time(self):
        for used, gone, ratio, verdict in ((55, 50, 1.1, 'on'), (45, 50, 0.9, 'on'),
                                           (56, 50, 1.12, 'above'), (44, 50, 0.88, 'below'),
                                           (0, 0, None, 'unknown')):
            with self.subTest(used=used, gone=gone):
                pace = self.collect([], claude=account(used, gone, 30))['claude']['week']['pace']
                self.assertEqual(pace['ratio'], ratio)
                self.assertEqual(pace['verdict'], verdict)
