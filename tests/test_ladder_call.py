import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LADDER = REPO / 'ci' / 'ladder-call'
LINT = REPO / 'ci' / 'lint-direct-model-calls'


class StubHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_POST(self):
        route = self.routes.get(self.path)
        if not route:
            self.send_response(404)
            self.end_headers()
            return
        status, body = route
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        if body is not None:
            self.wfile.write(json.dumps(body).encode())

    def log_message(self, *_):
        pass


def run_server(routes):
    StubHandler.routes = routes
    server = HTTPServer(('127.0.0.1', 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def ladder_call(env, stdin_obj, lanes_file=None):
    env = {**os.environ, **env}
    if lanes_file:
        env['LADDER_CALL_LANES'] = str(lanes_file)
    return subprocess.run(
        [str(LADDER), '--purpose', 'test'],
        input=json.dumps(stdin_obj),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


class LadderCallTests(unittest.TestCase):
    def test_gateway_402_falls_to_opencode(self):
        server, port = run_server({
            '/v1/chat/completions': (402, {'error': 'payment'}),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'ok-from-opencode'}}],
            }),
        })
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{port}'
        env = {
            'AI_GATEWAY_API_KEY': 'gw-key',
            'OPENCODE_API_KEY': 'oc-key',
            'LADDER_CALL_URL_VERCEL_AI_GATEWAY': base + '/v1/chat/completions',
            'LADDER_CALL_URL_OPENCODE_GO': base + '/zen/go/v1/chat/completions',
        }
        result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('402', result.stderr)
        self.assertIn('lane opencode-go answered', result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['choices'][0]['message']['content'], 'ok-from-opencode')

    def test_first_lane_429_second_lane_200(self):
        server, port = run_server({
            '/v1/chat/completions': (429, {'error': 'rate limit'}),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'second-lane'}}],
            }),
        })
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{port}'
        env = {
            'AI_GATEWAY_API_KEY': 'gw-key',
            'OPENCODE_API_KEY': 'oc-key',
            'LADDER_CALL_URL_VERCEL_AI_GATEWAY': base + '/v1/chat/completions',
            'LADDER_CALL_URL_OPENCODE_GO': base + '/zen/go/v1/chat/completions',
        }
        result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('lane vercel-ai-gateway skipped: 429', result.stderr)
        self.assertIn('lane opencode-go answered', result.stderr)

    def test_missing_keys_skipped(self):
        result = ladder_call({}, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 3)
        self.assertIn('no lane answered', result.stderr)
        self.assertIn('skipped: no key', result.stderr)

    def test_all_fail_exit_3(self):
        server, port = run_server({
            '/v1/chat/completions': (500, {'error': 'down'}),
        })
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{port}'
        lanes = [{
            'name': 'vercel-ai-gateway',
            'key_env': 'AI_GATEWAY_API_KEY',
            'url': base + '/v1/chat/completions',
            'model': 'deepseek/deepseek-v4.1-flash',
        }]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(lanes, fh)
            lanes_path = fh.name
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call({'AI_GATEWAY_API_KEY': 'k'}, {'messages': []}, lanes_file=lanes_path)
        self.assertEqual(result.returncode, 3)
        self.assertIn('ladder-call: no lane answered', result.stderr.strip().splitlines()[-1])

    def test_empty_answer_skipped(self):
        server, port = run_server({
            '/v1/chat/completions': (200, {'choices': [{'message': {'content': '   '}}]}),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'real'}}],
            }),
        })
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{port}'
        env = {
            'AI_GATEWAY_API_KEY': 'gw-key',
            'OPENCODE_API_KEY': 'oc-key',
            'LADDER_CALL_URL_VERCEL_AI_GATEWAY': base + '/v1/chat/completions',
            'LADDER_CALL_URL_OPENCODE_GO': base + '/zen/go/v1/chat/completions',
        }
        result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 0)
        self.assertIn('skipped: empty answer', result.stderr)
        self.assertIn('lane opencode-go answered', result.stderr)


class LintDirectModelCallsTests(unittest.TestCase):
    def test_flags_multiprompt_android_verifier_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / '.github' / 'scripts' / 'pstack-verifier' / 'verifier.cjs'
            script.parent.mkdir(parents=True)
            script.write_text(
                "const ENDPOINT = 'https://api.deepseek.com/chat/completions';\n",
                encoding='utf-8',
            )
            result = subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn('api.deepseek.com', result.stdout)
            self.assertIn('ladder-call', result.stdout)

    def test_allow_line_until_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'tools' / 'verifier.cjs'
            script.parent.mkdir(parents=True)
            until = (date.today() + timedelta(days=30)).isoformat()
            script.write_text(
                f"const x = 'https://api.deepseek.com/x'; // ladder-call-allow: emergency until {until}\n",
                encoding='utf-8',
            )
            result = subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)


if __name__ == '__main__':
    unittest.main()