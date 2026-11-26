import io
import os
import shlex
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from _load import REPO, load_bin

pp = load_bin('plan-pick')


class WorkerUsageExportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.status = 200
        self.authorized = False
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                case.authorized = self.headers.get('Authorization') == 'Bearer test-export-key'
                self.send_response(case.status, 'test-export-key')
                self.end_headers()
                self.wfile.write(b'day,provider,cost_usd\n2026-11-25,opencode-go,1.5\n')

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        script = self.home / 'plan-pick'
        source = Path(REPO, 'bin/plan-pick').read_text()
        script.write_text(source.replace(pp.OPENCODE_USAGE_URL,
                                        f'http://127.0.0.1:{server.server_port}/usage/export'))
        local_bin = self.home / '.local/bin'
        local_bin.mkdir(parents=True)
        key = local_bin / 'yeo-key'
        key.write_text('#!/usr/bin/env python3\n'
                       'import os, sys\n'
                       'assert sys.argv[1:4] == ["--env", "OpenCode Check usage", "--"]\n'
                       'assert not os.environ.get("OPENCODE_API_KEY")\n'
                       'assert not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")\n'
                       'os.environ["OPENCODE_API_KEY"] = "test-export-key"\n'
                       'os.execvpe(sys.argv[4], sys.argv[4:], os.environ)\n')
        key.chmod(0o755)
        shims = self.home / 'worker-shims'
        shims.mkdir()
        python = shims / 'python3'
        python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        python.chmod(0o755)
        for name, value in (('__file__', str(script)), ('YEO_KEY', str(key)),
                            ('WORKER_SHIMS', str(shims))):
            patch = mock.patch.object(pp, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.env = {'HOME': str(self.home), 'PATH': f'{local_bin}{os.pathsep}{os.defpath}',
                    'CODEX_THREAD_ID': 'test-worker'}

    def launch(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(output):
            code, tail = pp.run_command(shlex.join(pp.usage_export_command()))
        self.assertNotIn('test-export-key', output.getvalue())
        return code, str(tail)

    def test_worker_shell_and_python_shim_preserve_the_injected_key(self):
        code, output = self.launch()
        self.assertEqual(code, 0, output)
        self.assertEqual(pp.usage_rows(output),
                         [{'day': '2026-11-25', 'provider': 'opencode-go', 'usd': 1.5}])
        self.assertTrue(self.authorized)

    def test_worker_http_failure_names_the_status_without_exposing_the_key(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.status = status
                code, output = self.launch()
                self.assertEqual(code, 1)
                self.assertTrue(self.authorized)
                self.assertEqual(output.strip(),
                                 f'plan-pick: cannot read the OpenCode usage export: HTTP {status}')


class UsageExportErrorTests(unittest.TestCase):
    def test_transport_errors_name_the_underlying_cause_without_secret_text(self):
        for cause in (ConnectionRefusedError('test-export-key'), TimeoutError('test-export-key')):
            with self.subTest(cause=type(cause).__name__):
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.dict(os.environ, OPENCODE_API_KEY='test-export-key'), \
                        mock.patch.object(pp, 'fetch_usage_export', side_effect=urllib.error.URLError(cause)), \
                        redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(pp.usage_export_child(), 1)
                self.assertEqual(output.getvalue(), '')
                self.assertEqual(errors.getvalue().strip(),
                                 'plan-pick: cannot read the OpenCode usage export: '
                                 f'URLError ({type(cause).__name__})')

    def test_missing_key_is_a_distinct_error_and_does_not_fetch(self):
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(pp, 'fetch_usage_export') as fetch, redirect_stderr(errors):
            self.assertEqual(pp.usage_export_child(), 1)
        fetch.assert_not_called()
        self.assertEqual(errors.getvalue(), 'plan-pick: OPENCODE_API_KEY missing\n')
