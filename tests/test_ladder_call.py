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
FIXTURE_DIR = REPO / 'tests' / 'fixtures'


def run_lint(root):
    return subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)


def action_run_script():
    lines = (ACTION_LADDER / 'action.yml').read_text(encoding='utf-8').splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() != 'id: run':
            continue
        for j in range(index + 1, len(lines)):
            if lines[j].strip() == 'run: |':
                start = j + 1
                break
        if start is not None:
            break
    if start is None:
        start = lines.index('      run: |') + 1
    block = []
    for line in lines[start:]:
        if line.startswith('        '):
            block.append(line[8:])
        elif not line.strip():
            block.append('')
        else:
            break
    script = '\n'.join(block)
    replacements = {
        '${{ inputs.purpose }}': 'test-purpose',
        '${{ inputs.request-file }}': '${REQUEST_FILE}',
        '${{ inputs.require-verdict }}': 'false',
        '${{ github.action_path }}': str(ACTION_LADDER),
        '${{ github.run_id }}': '123',
        '${{ github.run_attempt }}': '1',
        '${{ github.job }}': 'unit',
    }
    for source, value in replacements.items():
        script = script.replace(source, value)
    return script


class StubHandler(BaseHTTPRequestHandler):
    routes = {}
    delay = 0
    last_bodies = []

    def do_POST(self):
        if self.delay:
            time.sleep(self.delay)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        try:
            StubHandler.last_bodies.append(json.loads(raw))
        except json.JSONDecodeError:
            StubHandler.last_bodies.append(None)
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


def fake_cursor_agent(tmp_dir, mode='ok', answer='cursor-ok'):
    script = Path(tmp_dir) / 'cursor-agent'
    if mode == 'ok':
        body = f'#!/bin/sh\nprintf "%s\\n" "{answer}"\n'
    elif mode == 'fail':
        body = '#!/bin/sh\nexit 1\n'
    elif mode == 'empty':
        body = '#!/bin/sh\nexit 0\n'
    elif mode == 'rate':
        body = '#!/bin/sh\necho "rate limit exceeded"\nexit 0\n'
    else:
        raise ValueError(mode)
    script.write_text(body, encoding='utf-8')
    script.chmod(0o755)
    return str(script)


def ladder_call(env, stdin_obj, lanes_file=None, args=()):
    env = {**os.environ, **env}
    if lanes_file:
        env['LADDER_CALL_LANES'] = str(lanes_file)
    return subprocess.run(
        [str(LADDER), '--purpose', 'test', *args],
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


def write_vercel_then_opencode(port):
    lanes = [
        {
            'name': 'vercel-ai-gateway',
            'key_env': 'AI_GATEWAY_API_KEY',
            'url': f'http://127.0.0.1:{port}/v1/chat/completions',
            'model': 'test-model',
        },
        {
            'name': 'opencode-go',
            'key_env': 'OPENCODE_API_KEY',
            'url': f'http://127.0.0.1:{port}/zen/go/v1/chat/completions',
            'model': 'test-model',
        },
    ]
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
        lanes_path = write_vercel_then_opencode(port)
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            env, {'messages': [{'role': 'user', 'content': 'hi'}]}, lanes_file=lanes_path)
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
        lanes_path = write_vercel_then_opencode(port)
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            env, {'messages': [{'role': 'user', 'content': 'hi'}]}, lanes_file=lanes_path)
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
        lanes_path = write_vercel_then_opencode(port)
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            env, {'messages': [{'role': 'user', 'content': 'hi'}]}, lanes_file=lanes_path)
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
        lanes_path = write_vercel_then_opencode(port)
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            env, {'messages': [{'role': 'user', 'content': 'hi'}]}, lanes_file=lanes_path)
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

    def test_reasoning_only_answer_counts_as_answer(self):
        server, port = run_server({
            '/v1/chat/completions': (200, {
                'choices': [{'message': {
                    'content': '',
                    'reasoning': 'The diff matches the feature map.\n\nPASS\n',
                    'tool_calls': [{'type': 'function', 'function': {'name': 'bash'}}],
                }}],
            }, None),
        })
        self.addCleanup(server.shutdown)
        lanes_path = write_lanes(port, '/v1/chat/completions')
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            {'AI_GATEWAY_API_KEY': 'k'},
            {'messages': [{'role': 'user', 'content': 'hi'}]},
            lanes_file=lanes_path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('lane vercel-ai-gateway answered', result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['choices'][0]['message']['content'], '')

    def test_require_verdict_skips_agent_style_answer(self):
        server, port = run_server({
            '/agent': (200, {'choices': [{'message': {
                'content': "I'll start by inspecting the repo state and the supplied merge base.\n\n"
                           "**Shell**\n```\npwd; git status --short\n```\n",
            }}]}, None),
            '/verdict': (200, {'choices': [{'message': {
                'content': '**PASS**\n\nTests are green and the diff matches the map.',
            }}]}, None),
        })
        self.addCleanup(server.shutdown)
        lanes = [
            {
                'name': 'agent-lane',
                'key_env': 'AI_GATEWAY_API_KEY',
                'url': f'http://127.0.0.1:{port}/agent',
                'model': 'm',
            },
            {
                'name': 'verdict-lane',
                'key_env': 'OPENCODE_API_KEY',
                'url': f'http://127.0.0.1:{port}/verdict',
                'model': 'm',
            },
        ]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(lanes, fh)
            lanes_path = fh.name
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            {'AI_GATEWAY_API_KEY': 'gw-key', 'OPENCODE_API_KEY': 'oc-key'},
            {'messages': [{'role': 'user', 'content': 'hi'}]},
            lanes_file=lanes_path,
            args=('--require-verdict',),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('lane agent-lane skipped: no verdict', result.stderr)
        self.assertIn('lane agent-lane no-verdict answer tail:', result.stderr)
        self.assertIn('verdict-lane answered', result.stderr)

    def test_require_verdict_env_var(self):
        server, port = run_server({
            '/v1/chat/completions': (200, {'choices': [{'message': {'content': 'no answer here'}}]}, None),
        })
        self.addCleanup(server.shutdown)
        lanes_path = write_lanes(port, '/v1/chat/completions')
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            {'AI_GATEWAY_API_KEY': 'k', 'LADDER_CALL_REQUIRE_VERDICT': '1'},
            {'messages': [{'role': 'user', 'content': 'hi'}]},
            lanes_file=lanes_path,
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn('skipped: no verdict', result.stderr)

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
        lanes_path = write_vercel_then_opencode(port)
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            env, {'messages': [{'role': 'user', 'content': 'hi'}]}, lanes_file=lanes_path)
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

    def test_404_408_425_fall_to_second_lane(self):
        for status in (404, 408, 425):
            with self.subTest(status=status):
                server, port = run_server({
                    '/v1/chat/completions': (status, None, None),
                    '/zen/go/v1/chat/completions': (200, {
                        'choices': [{'message': {'content': 'second-lane-ok'}}],
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
                lanes_path = write_vercel_then_opencode(port)
                try:
                    result = ladder_call(
                        env, {'messages': [{'role': 'user', 'content': 'hi'}]},
                        lanes_file=lanes_path,
                    )
                finally:
                    os.unlink(lanes_path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'skipped: {status}', result.stderr)
                self.assertIn('lane opencode-go answered', result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['choices'][0]['message']['content'], 'second-lane-ok')

    def test_google_lane_answers_from_stub(self):
        server, port = run_server({
            '/v1beta/openai/chat/completions': (200, {
                'choices': [{'message': {'content': 'gemini-ok'}}],
            }, None),
        })
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{port}'
        lanes = [{
            'name': 'google',
            'key_env': 'GEMINI_API_KEY',
            'url': base + '/v1beta/openai/chat/completions',
            'model': 'gemini-2.5-flash',
        }]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(lanes, fh)
            lanes_path = fh.name
        self.addCleanup(lambda: os.unlink(lanes_path))
        result = ladder_call(
            {
                'GEMINI_API_KEY': 'g-key',
                'LADDER_CALL_LANES': lanes_path,
            },
            {'messages': [{'role': 'user', 'content': 'hi'}]},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('lane google answered', result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['choices'][0]['message']['content'], 'gemini-ok')

    def test_google_429_falls_to_cursor_then_vercel(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_bin = fake_cursor_agent(tmp, answer='from-cursor')
            server, port = run_server({
                '/v1beta/openai/chat/completions': (429, {'error': 'rate'}, None),
                '/v1/chat/completions': (200, {
                    'choices': [{'message': {'content': 'from-vercel'}}],
                }, None),
            })
            self.addCleanup(server.shutdown)
            base = f'http://127.0.0.1:{port}'
            lanes = [
                {
                    'name': 'google',
                    'key_env': 'GEMINI_API_KEY',
                    'url': base + '/v1beta/openai/chat/completions',
                    'model': 'gemini-2.5-flash',
                },
                {
                    'name': 'cursor',
                    'key_env': 'CURSOR_API_KEY',
                    'kind': 'cursor',
                    'model': 'composer-2.5',
                },
                {
                    'name': 'vercel-ai-gateway',
                    'key_env': 'AI_GATEWAY_API_KEY',
                    'url': base + '/v1/chat/completions',
                    'model': 'test-model',
                },
            ]
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
                json.dump(lanes, fh)
                lanes_path = fh.name
            self.addCleanup(lambda: os.unlink(lanes_path))
            env = {
                'GEMINI_API_KEY': 'g',
                'CURSOR_API_KEY': 'c',
                'AI_GATEWAY_API_KEY': 'v',
                'LADDER_CALL_LANES': lanes_path,
                'LADDER_CALL_CURSOR_AGENT': cursor_bin,
                'LADDER_CALL_URL_VERCEL_AI_GATEWAY': base + '/v1/chat/completions',
            }
            result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('lane google skipped: 429', result.stderr)
            self.assertIn('lane cursor answered', result.stderr)
            self.assertNotIn('lane vercel-ai-gateway answered', result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload['choices'][0]['message']['content'], 'from-cursor')

    def test_cursor_lane_failure_and_empty_skipped(self):
        for mode, reason in (('fail', 'exit 1'), ('empty', 'empty answer')):
            with self.subTest(reason=reason):
                with tempfile.TemporaryDirectory() as tmp:
                    script = fake_cursor_agent(tmp, mode=mode)
                    lanes = [{
                        'name': 'cursor',
                        'key_env': 'CURSOR_API_KEY',
                        'kind': 'cursor',
                        'model': 'composer-2.5',
                    }]
                    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
                        json.dump(lanes, fh)
                        lanes_path = fh.name
                    try:
                        result = ladder_call(
                            {
                                'CURSOR_API_KEY': 'c',
                                'LADDER_CALL_LANES': lanes_path,
                                'LADDER_CALL_CURSOR_AGENT': script,
                            },
                            {'messages': [{'role': 'user', 'content': 'hi'}]},
                        )
                        self.assertEqual(result.returncode, 3, result.stderr)
                        self.assertIn(f'lane cursor skipped: {reason}', result.stderr)
                    finally:
                        os.unlink(lanes_path)

    def test_cursor_rate_limit_text_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            rate_bin = fake_cursor_agent(tmp, mode='rate')
            lanes = [{
                'name': 'cursor',
                'key_env': 'CURSOR_API_KEY',
                'kind': 'cursor',
                'model': 'composer-2.5',
            }]
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
                json.dump(lanes, fh)
                lanes_path = fh.name
            self.addCleanup(lambda: os.unlink(lanes_path))
            result = ladder_call(
                {
                    'CURSOR_API_KEY': 'c',
                    'LADDER_CALL_LANES': lanes_path,
                    'LADDER_CALL_CURSOR_AGENT': rate_bin,
                },
                {'messages': [{'role': 'user', 'content': 'hi'}]},
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn('lane cursor skipped: rate limit', result.stderr)

    def test_google_429_cursor_fail_falls_to_vercel(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor_bin = fake_cursor_agent(tmp, mode='fail')
            server, port = run_server({
                '/v1beta/openai/chat/completions': (429, {'error': 'rate'}, None),
                '/v1/chat/completions': (200, {
                    'choices': [{'message': {'content': 'from-vercel'}}],
                }, None),
            })
            self.addCleanup(server.shutdown)
            base = f'http://127.0.0.1:{port}'
            lanes = [
                {
                    'name': 'google',
                    'key_env': 'GEMINI_API_KEY',
                    'url': base + '/v1beta/openai/chat/completions',
                    'model': 'gemini-2.5-flash',
                },
                {
                    'name': 'cursor',
                    'key_env': 'CURSOR_API_KEY',
                    'kind': 'cursor',
                    'model': 'composer-2.5',
                },
                {
                    'name': 'vercel-ai-gateway',
                    'key_env': 'AI_GATEWAY_API_KEY',
                    'url': base + '/v1/chat/completions',
                    'model': 'test-model',
                },
            ]
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
                json.dump(lanes, fh)
                lanes_path = fh.name
            self.addCleanup(lambda: os.unlink(lanes_path))
            env = {
                'GEMINI_API_KEY': 'g',
                'CURSOR_API_KEY': 'c',
                'AI_GATEWAY_API_KEY': 'v',
                'LADDER_CALL_LANES': lanes_path,
                'LADDER_CALL_CURSOR_AGENT': cursor_bin,
            }
            result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('lane google skipped: 429', result.stderr)
            self.assertIn('lane cursor skipped: exit 1', result.stderr)
            self.assertIn('lane vercel-ai-gateway answered', result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload['choices'][0]['message']['content'], 'from-vercel')

    def test_vercel_payload_pins_token_pass(self):
        StubHandler.last_bodies = []
        server, port = run_server({
            '/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'ok'}}],
            }, None),
        })
        self.addCleanup(server.shutdown)
        lanes_path = write_lanes(port, '/v1/chat/completions')
        self.addCleanup(lambda: os.unlink(lanes_path))
        env = {'AI_GATEWAY_API_KEY': 'k', 'LADDER_CALL_LANES': lanes_path}
        result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        body = StubHandler.last_bodies[-1]
        self.assertEqual(body['providerOptions'], {'gateway': {'only': ['token-pass']}})

    def test_openrouter_payload_pins_streamlake(self):
        StubHandler.last_bodies = []
        server, port = run_server({
            '/api/v1/chat/completions': (200, {
                'choices': [{'message': {'content': 'ok'}}],
            }, None),
        })
        self.addCleanup(server.shutdown)
        lanes = [{
            'name': 'openrouter',
            'key_env': 'OPENROUTER_API_KEY',
            'url': f'http://127.0.0.1:{port}/api/v1/chat/completions',
            'model': 'deepseek/deepseek-v4.1-flash-1017',
            'provider': {'only': ['StreamLake'], 'allow_fallbacks': False},
        }]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
            json.dump(lanes, fh)
            lanes_path = fh.name
        self.addCleanup(lambda: os.unlink(lanes_path))
        env = {'OPENROUTER_API_KEY': 'k', 'LADDER_CALL_LANES': lanes_path}
        result = ladder_call(env, {'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        body = StubHandler.last_bodies[-1]
        self.assertEqual(body['provider'], {'only': ['StreamLake'], 'allow_fallbacks': False})

    def test_ci_lanes_model_ids(self):
        lanes = json.loads((REPO / 'ci' / 'lanes.json').read_text(encoding='utf-8'))
        models = {lane['name']: lane['model'] for lane in lanes}
        self.assertEqual(models['vercel-ai-gateway'], 'deepseek/deepseek-v4.1-flash')
        self.assertEqual(models['openrouter'], 'deepseek/deepseek-v4.1-flash-1017')
        self.assertEqual(models['deepseek'], 'deepseek-flash')

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

    def test_action_installs_cursor_agent(self):
        action_yml = (ACTION_LADDER / 'action.yml').read_text(encoding='utf-8')
        self.assertIn('Install cursor-agent', action_yml)
        self.assertIn('cursor.com/install', action_yml)
        self.assertIn('GEMINI_API_KEY', action_yml)
        self.assertIn('CURSOR_API_KEY', action_yml)

    def test_action_wrapper_logs_on_failure(self):
        action_yml = (ACTION_LADDER / 'action.yml').read_text(encoding='utf-8')
        self.assertIn('if !', action_yml)
        self.assertIn('cat "$log" >&2', action_yml)
        self.assertIn('ladder-call-answer-', action_yml)
        self.assertIn('${RANDOM}', action_yml)
        self.assertIn('-$$-', action_yml)

    def test_action_prints_lane_skips_when_ladder_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')}
            env['RUNNER_TEMP'] = tmp
            env['GITHUB_OUTPUT'] = os.path.join(tmp, 'output')
            request = Path(tmp) / 'request.json'
            request.write_text('{"messages": []}', encoding='utf-8')
            env['REQUEST_FILE'] = str(request)
            script = Path(tmp) / 'step.sh'
            script.write_text(action_run_script(), encoding='utf-8')
            first = subprocess.run(['bash', str(script)], capture_output=True, text=True, env=env, check=False)
            second = subprocess.run(['bash', str(script)], capture_output=True, text=True, env=env, check=False)
            self.assertEqual(first.returncode, 1)
            self.assertIn('skipped: no key', first.stderr)
            self.assertIn('no lane answered', first.stderr)
            self.assertEqual(second.returncode, 1)
            answers = sorted(Path(tmp).glob('ladder-call-answer-*.json'))
            logs = sorted(Path(tmp).glob('ladder-call-*.log'))
            self.assertEqual(len(answers), 2)
            self.assertEqual(len(logs), 2)
            self.assertNotEqual(answers[0].name, answers[1].name)

    def test_flags_real_multiprompt_verifier_with_file_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / '.github' / 'scripts' / 'pstack-verifier' / 'verifier.cjs'
            dest.parent.mkdir(parents=True)
            dest.write_bytes((FIXTURE_DIR / 'multiprompt-android-verifier.cjs').read_bytes())
            result = run_lint(root)
            self.assertEqual(result.returncode, 1)
            self.assertRegex(result.stdout, r'verifier\.cjs:10: direct model call \(api\.deepseek\.com\)')
            self.assertRegex(result.stdout, r'verifier\.cjs:365: direct model call \(direct model key \(env\.DEEPSEEK_API_KEY\)\)')
            self.assertRegex(result.stdout, r'verifier\.cjs:388: direct model call \(model fallback\)')
            self.assertIn('@main', result.stdout)

    def test_flags_os_environ_and_getenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'tools' / 'keys.py'
            script.parent.mkdir(parents=True)
            script.write_text(
                "import os\n"
                "a = os.environ['OPENROUTER_API_KEY']\n"
                "b = os.getenv('ANTHROPIC_API_KEY')\n"
                "c = os.environ.get('OPENAI_API_KEY')\n",
                encoding='utf-8',
            )
            result = run_lint(root)
            self.assertEqual(result.returncode, 1)
            self.assertRegex(result.stdout, r'keys\.py:2: direct model call \(direct model key \(os\.environ\[')
            self.assertRegex(result.stdout, r'keys\.py:3: direct model call \(direct model key \(os\.getenv\(')
            self.assertRegex(result.stdout, r'keys\.py:4: direct model call \(direct model key \(os\.environ\.get\(')

    def test_flags_credit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'tools' / 'call.js'
            script.parent.mkdir(parents=True)
            script.write_text(
                "const key = process.env.DEEPSEEK_API_KEY;\n"
                "if (answer.includes('out of credit')) retry();\n",
                encoding='utf-8',
            )
            result = run_lint(root)
            self.assertEqual(result.returncode, 1)
            self.assertRegex(result.stdout, r'call\.js:2: direct model call \(model fallback\)')

    def test_ladder_call_action_passes_lint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / '.github' / 'actions' / 'ladder-call' / 'action.yml'
            dest.parent.mkdir(parents=True)
            dest.write_bytes((ACTION_LADDER / 'action.yml').read_bytes())
            result = run_lint(root)
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_flags_process_env_model_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'tools' / 'call.js'
            script.parent.mkdir(parents=True)
            script.write_text(
                "const key = process.env.DEEPSEEK_API_KEY;\n"
                "fetch('https://example.com');\n",
                encoding='utf-8',
            )
            result = subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn('direct model key', result.stdout)
            self.assertIn('@main', result.stdout)

    def test_flags_model_fallback_with_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / '.github' / 'scripts' / 'verifier.cjs'
            script.parent.mkdir(parents=True)
            script.write_text(
                "const ENDPOINT = 'https://api.deepseek.com/chat/completions';\n"
                "async function callDeepSeek({ key, fetchImpl = fetch }) {\n"
                "  const response = await fetchImpl(ENDPOINT, { headers: { authorization: key } });\n"
                "  if (response.status === 429 || response.status >= 500) {\n"
                "    throw new Error('retry');\n"
                "  }\n"
                "}\n"
                "function requireKey(env) { return env.DEEPSEEK_API_KEY.trim(); }\n",
                encoding='utf-8',
            )
            result = subprocess.run([str(LINT), str(root)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn('model fallback', result.stdout)

    def test_ladder_actions_workflow_asserts_outputs(self):
        workflow = (REPO / '.github' / 'workflows' / 'ladder-actions.yml').read_text(encoding='utf-8')
        self.assertIn('id: ladder', workflow)
        self.assertIn('steps.ladder.outputs.lane', workflow)
        self.assertIn('steps.ladder.outputs.answer-file', workflow)
        self.assertIn('opencode-go', workflow)
        self.assertIn('action-ok', workflow)

    def test_ladder_actions_workflow_runs_two_calls_in_one_job(self):
        workflow = (REPO / '.github' / 'workflows' / 'ladder-actions.yml').read_text(encoding='utf-8')
        self.assertIn('id: ladder2', workflow)
        self.assertIn('!= "${{ steps.ladder2.outputs.answer-file }}"', workflow)


if __name__ == '__main__':
    unittest.main()
