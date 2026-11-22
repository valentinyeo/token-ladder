import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _load import REPO

SCAN = Path(REPO) / 'ci' / 'scan-repos'


def load_scan():
    loader = importlib.machinery.SourceFileLoader('scan_repos', str(SCAN))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('scan_repos', loader))
    loader.exec_module(module)
    return module


class ScanReposTests(unittest.TestCase):
    def test_scan_repo_reports_hit_and_secrets(self):
        scan = load_scan()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'tools' / 'agent.py'
            script.parent.mkdir(parents=True)
            script.write_text(
                "URL = 'https://generativelanguage.googleapis.com/v1beta/models'\n",
                encoding='utf-8',
            )
            workflow = root / '.github' / 'workflows' / 'ai.yml'
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "- uses: valentinyeo/token-ladder/.github/actions/ladder-call@main\n",
                encoding='utf-8',
            )
            with patch.object(scan, 'gh_secret_names', return_value='CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY'):
                rows = scan.scan_repo('valentinyeo/fake', root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], 'valentinyeo/fake')
            self.assertEqual(rows[0][1], 'tools/agent.py:1')
            self.assertIn('generativelanguage', rows[0][2])
            self.assertIn('OPENAI_API_KEY', rows[0][3])
            self.assertEqual(rows[0][4], 'yes')

    def test_print_table_header(self):
        from io import StringIO

        scan = load_scan()
        buf = StringIO()
        with patch('sys.stdout', buf):
            scan.print_table([('valentinyeo/r', 'f:1', 'x', 'S', 'no')])
        text = buf.getvalue()
        self.assertIn('repo\tfile:line', text)
        self.assertIn('valentinyeo/r\tf:1\tx\tS\tno', text)


if __name__ == '__main__':
    unittest.main()