import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LADDER = REPO / 'ci' / 'ladder-call'
LINT = REPO / 'ci' / 'lint-direct-model-calls'
ACTION_LADDER = REPO / '.github' / 'actions' / 'ladder-call'


class StubHandler(BaseHTTPRequestHandler):
    routes = {}
    delay = 0

    def do_POST(self):
        if self.delay:
            time.sleep(self.delay)
        route = self.routes.get(self.path)
        if not route:
            self.send_response(404)
            self.end_headers()
            return
        status, body, content_type = route
        self.send_response(status)
        self.send_header('Content-Type', content_type or 'application/json')
        self.end_headers()
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                self.wfile.write(body)
            else:
                self.wfile.write(json.dumps(body).encode())

    def log_message(self, *_):
        pass


def run_server(routes, delay=0):
    StubHandler.routes = routes
    StubHandler.delay = delay
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


def write_lanes(port, path):
    lanes = [{
        'name': 'vercel-ai-gateway',
        'key_env': 'AI_GATEWAY_API_KEY',
        'url': f'http://127.0.0.1:{port}{path}',
        'model': 'test-model',
    }]
    fh = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
    json.dump(lanes, fh)
    fh.close()
    return fh.name


class LadderCallTests(unittest.TestCase):
    def test_gateway_402_falls_to_opencode(self):
        server, port = run_server({
            '/v1/chat/completions': (402, {'error': 'payment'}, None),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'ok-from-opencode'}}],
            }, None),
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
            '/v1/chat/completions': (429, {'error': 'rate limit'}, None),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'second-lane'}}],
            }, None),
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
            '/v1/chat/completions': (500, {'error': 'down'}, None),
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
            '/v1/chat/completions': (200, {'choices': [{'message': {'content': '   '}}]}, None),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'real'}}],
            }, None),
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

    def test_non_json_200_falls_through(self):
        server, port = run_server({
            '/v1/chat/completions': (200, b'<html>nope</html>', 'text/html'),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'after-html'}}],
            }, None),
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
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('Traceback', result.stderr + result.stdout)
        self.assertIn('invalid response', result.stderr)
        self.assertIn('lane opencode-go answered', result.stderr)

    def test_content_parts_array(self):
        server, port = run_server({
            '/v1/chat/completions': (200, {
                'choices': [{'message': {'content': [
                    {'type': 'text', 'text': 'hello '},
                    {'type': 'text', 'text': 'world'},
                ]}}],
            }, None),
        })
        self.addCleanup(server.shutdown)
        lanes = [{
            'name': 'vercel-ai-gateway',
            'key_env': 'AI_GATEWAY_API_KEY',
            'url': f'http://127.0.0.1:{port}/v1/chat/completions',
            'model': 'm',
        }]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(lanes, fh)
            lanes_path = fh.name
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            {'AI_GATEWAY_API_KEY': 'k'},
            {'messages': [{'role': 'user', 'content': 'hi'}]},
            lanes_file=lanes_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['choices'][0]['message']['content'][0]['text'], 'hello ')

    def test_401_skipped(self):
        server, port = run_server({
            '/v1/chat/completions': (401, {'error': 'auth'}, None),
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'ok'}}],
            }, None),
        })
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{port}'
        env = {
            'AI_GATEWAY_API_KEY': 'k',
            'OPENCODE_API_KEY': 'k2',
            'LADDER_CALL_URL_VERCEL_AI_GATEWAY': base + '/v1/chat/completions',
            'LADDER_CALL_URL_OPENCODE_GO': base + '/zen/go/v1/chat/completions',
        }
        result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipped: 401', result.stderr)

    def test_403_skipped(self):
        server, port = run_server({'/v1/chat/completions': (403, {'error': 'forbidden'}, None)})
        self.addCleanup(server.shutdown)
        lanes_path = write_lanes(port, '/v1/chat/completions')
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            {'AI_GATEWAY_API_KEY': 'k'},
            {'messages': [{'role': 'user', 'content': 'hi'}]},
            lanes_file=lanes_path,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn('skipped: 403', result.stderr)

    def test_400_stops_with_error(self):
        server, port = run_server({'/v1/chat/completions': (400, {'error': 'bad'}, None)})
        self.addCleanup(server.shutdown)
        lanes_path = write_lanes(port, '/v1/chat/completions')
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            {'AI_GATEWAY_API_KEY': 'k'},
            {'messages': [{'role': 'user', 'content': 'hi'}]},
            lanes_file=lanes_path,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('failed: HTTP 400', result.stderr)

    def test_timeout_skipped_with_env(self):
        class SlowHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(3)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        slow = HTTPServer(('127.0.0.1', 0), SlowHandler)
        slow_thread = threading.Thread(target=slow.serve_forever, daemon=True)
        slow_thread.start()
        self.addCleanup(slow.shutdown)
        slow_port = slow.server_address[1]

        server2, port2 = run_server({
            '/zen/go/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'fast'}}],
            }, None),
        })
        self.addCleanup(server2.shutdown)
        lanes = [
            {
                'name': 'vercel-ai-gateway',
                'key_env': 'AI_GATEWAY_API_KEY',
                'url': f'http://127.0.0.1:{slow_port}/v1/chat/completions',
                'model': 'm',
            },
            {
                'name': 'opencode-go',
                'key_env': 'OPENCODE_API_KEY',
                'url': f'http://127.0.0.1:{port2}/zen/go/v1/chat/completions',
                'model': 'm',
            },
        ]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(lanes, fh)
            lanes_path = fh.name
        self.addCleanup(lambda: os.unlink(lanes_path))
        env = {
            'AI_GATEWAY_API_KEY': 'k',
            'OPENCODE_API_KEY': 'k2',
            'LADDER_CALL_TIMEOUT': '1',
            'LADDER_CALL_LANES': lanes_path,
        }
        result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('timeout', result.stderr)


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

    def test_key_env_wiring_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workflow = root / '.github' / 'workflows' / 'verifier.yml'
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "env:\n"
                "  DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}\n"
                "steps:\n"
                "  - uses: valentinyeo/token-ladder/.github/actions/ladder-call@main\n",
                encoding='utf-8',
            )
            result = subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_allow_file_until_future(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'tools' / 'verifier.cjs'
            script.parent.mkdir(parents=True)
            script.write_text(
                "const x = 'https://api.deepseek.com/x';\n",
                encoding='utf-8',
            )
            allow = root / '.github' / 'ladder-call-allow'
            allow.parent.mkdir(parents=True)
            until = (date.today() + timedelta(days=30)).isoformat()
            allow.write_text(f'tools/verifier.cjs:emergency:{until}\n', encoding='utf-8')
            result = subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_expired_allow_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'tools' / 'verifier.cjs'
            script.parent.mkdir(parents=True)
            script.write_text(
                "const x = 'https://api.deepseek.com/x';\n",
                encoding='utf-8',
            )
            allow = root / '.github' / 'ladder-call-allow'
            allow.parent.mkdir(parents=True)
            expired = (date.today() - timedelta(days=1)).isoformat()
            allow.write_text(f'tools/verifier.cjs:emergency:{expired}\n', encoding='utf-8')
            result = subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn('expired allow', result.stdout)

    def test_token_ladder_repo_passes_lint(self):
        result = subprocess.run([str(LINT), str(REPO)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_action_wrapper_uses_action_path(self):
        action_yml = (ACTION_LADDER / 'action.yml').read_text(encoding='utf-8')
        self.assertIn('${{ github.action_path }}/run', action_yml)
        self.assertNotIn('/ci/ladder-call', action_yml)


if __name__ == '__main__':
    unittest.main()