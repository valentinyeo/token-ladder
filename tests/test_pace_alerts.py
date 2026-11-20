import io
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _load import tl

NOW = 1793010000
ROLES = ('Product Lead', 'Support Lead', 'Design Lead', 'Growth Lead', 'Finance')


def usage(used=20, gone=10):
    return {'week': {'used': used, 'resets_at': NOW + 168 * 3600 * (1 - gone / 100)},
            'five_hour': {'used': None}}


class PaceAlertsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.state = self.home / 'state'
        for name, value in (('STATE', str(self.state)), ('HOME', str(self.home)),
                            ('STATUS', str(self.state / 'status.json'))):
            patcher = patch.object(tl, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        self.delivery = 'delivered'
        patcher = patch.object(tl.subprocess, 'run', side_effect=self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(tl, 'read_grok', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_run(self, command, **kwargs):
        self.calls.append(command)
        if command[0] == 'tmux':
            return SimpleNamespace(returncode=0, stdout='\n'.join(
                f'manager-{i}\t✳ HT {role}' for i, role in enumerate(ROLES)) +
                '\nrunner\tRUNNER 17\nother\tSF Design Lead\nmanager-0\tHT Product Lead')
        self.assertEqual(command[0], 'session-say')
        self.assertEqual(command[-2:], ['--from', 'token-ladder'])
        self.assertTrue(command[2].startswith('Claude pace '))
        self.assertTrue(command[2].endswith('Shift new runners to Codex.'))
        return SimpleNamespace(returncode=0, stdout=self.delivery + '\n')

    def test_collect_fanout_and_two_hour_dedup(self):
        real_config = tl.load_config()
        config = dict(real_config, publish=False)
        with ExitStack() as stack:
            for name, value in (
                ('load_config', config), ('decide', ('codex', 'fixture', [])),
                ('read_claude', usage()), ('read_codex', usage(5)),
                ('read_openrouter', {}), ('claude_email', None), ('codex_email', None),
                ('read_override', None), ('read_reset_feed', {}), ('read_onepassword', {}),
                ('build_accounts', []), ('dashboard_url', 'fixture'), ('record_resources', {}),
                ('burn_plan', {}), ('current_pools', {}),
                ('render_dashboard', ''), ('mirror', None), ('push_usage', False), ('mode_line', 'fixture')):
                stack.enter_context(patch.object(tl, name, return_value=value))
            stack.enter_context(patch.object(tl.time, 'time', return_value=NOW))
            stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
            tl.collect()
            tl.collect()
        calls = [c for c in self.calls if c[0] == 'session-say']
        self.assertEqual([c[1] for c in calls], ['manager-0', 'manager-1'])
        records = [json.loads(x) for x in (self.state / 'pace-alerts.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r['status'] == 'delivered' and r['pace'] == 2 for r in records))
        self.assertEqual(tl.notify_pace({'claude': usage()}, NOW + 7199), [])
        self.assertEqual(len(tl.notify_pace({'claude': usage()}, NOW + 7200)), 2)

    def test_busy_or_typed_input_uses_safe_mail_and_fallback(self):
        self.delivery = 'queued'
        records = tl.notify_pace({'claude': usage()}, NOW)
        self.assertEqual([r['status'] for r in records], ['queued'] * 2)
        text = (self.home / 'poteto-handover/ladder-alerts.md').read_text()
        self.assertEqual(text.count('[from token-ladder]'), 2)
        self.assertEqual(tl.notify_pace({'claude': usage()}, NOW + 1), [])
        self.assertTrue(all(c[1] == 'list-panes' for c in self.calls if c[0] == 'tmux'))

    def test_unavailable_delivery_is_logged_and_deduplicated(self):
        with patch.object(tl.subprocess, 'run', side_effect=FileNotFoundError):
            records = tl.notify_pace({'claude': usage()}, NOW)
        self.assertEqual(records[0]['target'], 'HT Product (offline)')
        self.assertEqual(len(records), 2)
        self.assertIn('[from token-ladder]', (self.home / 'poteto-handover/ladder-alerts.md').read_text())
        self.assertEqual(tl.notify_pace({'claude': usage()}, NOW + 1), [])

    def test_thresholds_windows_and_unknowns(self):
        self.assertEqual(tl.over_pace({'claude': usage(11.5), 'codex': usage(13)}, NOW), {})
        ratios = tl.over_pace({'claude': usage(11.6), 'codex': usage(13.1)}, NOW)
        self.assertAlmostEqual(ratios['claude'], 1.16)
        self.assertAlmostEqual(ratios['codex'], 1.31)
        self.assertEqual(tl.over_pace({'claude': usage(20, 0)}, NOW), {})
        self.assertEqual(tl.over_pace({'claude': usage(20, 100)}, NOW), {})
        self.assertEqual(tl.over_pace({'claude': usage(None)}, NOW), {})
        self.assertEqual(tl.over_pace({}, NOW), {})
        with patch.object(tl, 'read_grok', return_value=usage(14)['week']):
            self.assertAlmostEqual(tl.over_pace({}, NOW)['grok'], 1.4)
        short = {'used': 20, 'resets_at': NOW + 4.5 * 3600}
        self.assertAlmostEqual(tl.over_pace({'claude': {'five_hour': short}}, NOW)['claude'], 2)

    def test_cooldowns_are_per_brain(self):
        with patch.object(tl.subprocess, 'run', side_effect=FileNotFoundError):
            tl.notify_pace({'claude': usage()}, NOW)
            records = tl.notify_pace({'claude': usage(), 'codex': usage()}, NOW + 10)
        self.assertEqual([r['brain'] for r in records], ['codex'] * 2)

    def test_delivery_failure_does_not_break_collection(self):
        original = self.fake_run

        def fail(command, **kwargs):
            if command[0] == 'session-say':
                raise subprocess.TimeoutExpired(command, 20)
            return original(command, **kwargs)

        with patch.object(tl.subprocess, 'run', side_effect=fail):
            records = tl.notify_pace({'claude': usage()}, NOW)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r['status'] == 'queued' for r in records))

    def test_missing_support_is_written_without_losing_online_managers(self):
        original = self.fake_run

        def no_support(command, **kwargs):
            result = original(command, **kwargs)
            if command[0] == 'tmux':
                result.stdout = '\n'.join(line for line in result.stdout.splitlines() if 'HT Support' not in line)
            return result

        with patch.object(tl.subprocess, 'run', side_effect=no_support):
            records = tl.notify_pace({'claude': usage()}, NOW)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]['target'], 'HT Support (offline)')
        self.assertEqual(records[-1]['status'], 'queued')
        self.assertIn('HT Support (offline)', (self.home / 'poteto-handover/ladder-alerts.md').read_text())

    def test_leads_without_runners_are_never_pinged(self):
        tl.notify_pace({'claude': usage()}, NOW)
        targets = [c[1] for c in self.calls if c[0] == 'session-say']
        self.assertEqual(targets, ['manager-0', 'manager-1'])
        log = (self.state / 'pace-alerts.jsonl').read_text()
        for role in ('Design', 'Growth', 'Finance'):
            self.assertNotIn(role, log)
