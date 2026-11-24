import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ACTION = REPO / '.github/actions/ladder-agent'
LANES = ('opencode-go', 'vercel-ai-gateway', 'vercel-ai-gateway-zai', 'openrouter')
FAKE_PI = '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
agent_dir = Path(os.environ['PI_CODING_AGENT_DIR'])
settings = json.loads((agent_dir / 'settings.json').read_text())
lane = settings['defaultProvider']
with open(os.environ['STUB_CALLS'], 'a') as calls:
    print(json.dumps({'lane': lane, 'argv': sys.argv[1:], 'cwd': os.getcwd(),
                      'prompt': sys.stdin.read(), 'agent_dir': str(agent_dir),
                      'config': json.loads((agent_dir / 'models.json').read_text()),
                      'settings': settings}), file=calls)
mode = json.loads(os.environ.get('STUB_MODES', '{}')).get(lane, 'ok')
def message(text, stop='stop'):
    return {'role': 'assistant', 'content': [{'type': 'text', 'text': text}], 'stopReason': stop}
if mode == 'exit':
    sys.exit(7)
if mode == 'stderr429':
    print('HTTP 429', file=sys.stderr)
    sys.exit(1)
if mode == 'timeout':
    import time
    time.sleep(10)
    sys.exit(0)
if mode in ('402', '429', '400'):
    print(json.dumps({'type': 'message_end', 'message': message('intermediate narration')}))
    failed = message('', 'error')
    failed['errorMessage'] = 'HTTP ' + mode
    print(json.dumps({'type': 'message_end', 'message': failed}))
    print(json.dumps({'type': 'auto_retry_end', 'success': False, 'finalError': 'HTTP ' + mode}))
elif mode == 'empty':
    print(json.dumps({'type': 'message_end', 'message': message('I will run tools', 'toolUse')}))
    print(json.dumps({'type': 'agent_end', 'messages': [message('', 'stop')]}))
elif mode == 'recovered':
    print(json.dumps({'type': 'auto_retry_end', 'success': False, 'finalError': 'HTTP 429'}))
    print(json.dumps({'type': 'agent_end', 'messages': [message('recovered')]}))
else:
    print(json.dumps({'type': 'message_end', 'message': message('earlier narration')}))
    print(json.dumps({'type': 'agent_end', 'messages': [message('earlier narration'), message('final answer')]}))
print(json.dumps({'type': 'agent_settled', 'aborted': False}))
'''


class LadderAgentTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.cwd = self.root / 'work dir'
        self.cwd.mkdir()
        pi = self.root / 'pi'
        pi.write_text(FAKE_PI)
        pi.chmod(0o755)
        self.calls = self.root / 'calls.jsonl'
        self.output = self.root / 'output'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.endswith('_API_KEY') and not key.startswith('LADDER_AGENT_')}
        self.env.update(PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        RUNNER_TEMP=str(self.root), GITHUB_OUTPUT=str(self.output),
                        STUB_CALLS=str(self.calls), EXCLUDE_FAMILIES='',
                        OPENCODE_API_KEY='stub', AI_GATEWAY_API_KEY='stub', OPENROUTER_API_KEY='stub')

    def run_agent(self, modes=None, args=(), prompt='Use tools.\n$(touch should-not-exist)'):
        self.env['STUB_MODES'] = json.dumps(modes or {})
        result = subprocess.run([str(ACTION / 'run'), '--working-directory', str(self.cwd), *args],
                                input=prompt, text=True, capture_output=True, env=self.env, timeout=20)
        metadata = json.loads(result.stdout) if result.stdout.strip() else {}
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return result, metadata, calls

    def test_final_answer_cwd_prompt_and_outputs(self):
        result, meta, calls = self.run_agent()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(meta['lane'], 'opencode-go')
        self.assertEqual(meta['model'], 'deepseek-v4.1-flash')
        self.assertEqual(meta['exit-code'], 0)
        self.assertEqual(Path(meta['answer-file']).read_text(), 'final answer\n')
        self.assertEqual(dict(line.split('=', 1) for line in self.output.read_text().splitlines()),
                         {key: str(value) for key, value in meta.items()})
        self.assertEqual(calls[0]['cwd'], str(self.cwd))
        self.assertEqual(calls[0]['prompt'], 'Use tools.\n$(touch should-not-exist)')
        self.assertFalse((self.cwd / 'should-not-exist').exists())
        self.assertIn('-p', calls[0]['argv'])
        self.assertNotIn('--no-tools', calls[0]['argv'])
        self.assertFalse(Path(calls[0]['agent_dir']).exists())

    def test_402_429_and_empty_fall_through_even_with_exit_zero(self):
        result, meta, calls = self.run_agent(dict(zip(LANES, ('402', '429', 'empty', 'ok'))))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['lane'] for call in calls], list(LANES))
        self.assertEqual(meta['lane'], 'openrouter')
        self.assertEqual(Path(meta['answer-file']).read_text(), 'final answer\n')
        self.assertIn('empty answer', result.stderr)

    def test_exclusion_env_skips_deepseek_before_launch(self):
        self.env['EXCLUDE_FAMILIES'] = 'deepseek,openai'
        result, meta, calls = self.run_agent()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call['lane'] for call in calls], ['vercel-ai-gateway-zai'])
        self.assertEqual(meta['model'], 'zai/glm-5.3-flash')

    def test_cli_exclusions_and_unknown_family_fail_closed(self):
        result, meta, calls = self.run_agent(args=('--exclude-families', 'deepseek,zai'))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(meta['exit-code'], 3)
        result, _, calls = self.run_agent(args=('--exclude-families', 'typo'))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])

    def test_no_keys_no_launch_no_plan_login(self):
        for key in ('OPENCODE_API_KEY', 'AI_GATEWAY_API_KEY', 'OPENROUTER_API_KEY'):
            self.env.pop(key)
        self.env['ANTHROPIC_API_KEY'] = 'not-a-ladder-key'
        result, meta, calls = self.run_agent()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(calls, [])
        self.assertEqual(meta['lane'], '')
        self.assertEqual(Path(meta['answer-file']).read_text(), '')

    def test_all_exhausted_fails_with_empty_answer(self):
        result, meta, calls = self.run_agent({name: '429' for name in LANES})
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(len(calls), 4)
        self.assertEqual(meta['exit-code'], 3)
        self.assertEqual(Path(meta['answer-file']).read_text(), '')
        self.assertIn('no lane answered', result.stderr)

    def test_work_error_and_bad_request_stop_without_replaying(self):
        for mode, code in (('exit', 7), ('400', 1)):
            with self.subTest(mode=mode):
                self.calls.unlink(missing_ok=True)
                result, meta, calls = self.run_agent({'opencode-go': mode})
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(meta['exit-code'], code)
                self.assertEqual(len(calls), 1)
                self.assertEqual(Path(meta['answer-file']).read_text(), '')

    def test_stderr_rate_limit_and_timeout_fall_through(self):
        result, meta, calls = self.run_agent({'opencode-go': 'stderr429', 'vercel-ai-gateway': 'timeout'},
                                             args=('--timeout-seconds', '1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(meta['lane'], 'vercel-ai-gateway-zai')
        self.assertEqual(len(calls), 3)

    def test_recovered_provider_error_uses_final_answer(self):
        result, meta, calls = self.run_agent({'opencode-go': 'recovered'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(meta['answer-file']).read_text(), 'recovered\n')
        self.assertEqual(len(calls), 1)

    def test_endpoint_override_and_inherited_child_defaults(self):
        self.env['LADDER_AGENT_URL_OPENCODE_GO'] = 'http://127.0.0.1:1234/test/chat/completions'
        result, _, calls = self.run_agent()
        self.assertEqual(result.returncode, 0, result.stderr)
        config = calls[0]['config']['providers']['opencode-go']
        self.assertEqual(config['baseUrl'], 'http://127.0.0.1:1234/test')
        self.assertEqual(config['apiKey'], 'OPENCODE_API_KEY')
        self.assertEqual(calls[0]['settings']['defaultProvider'], 'opencode-go')
        self.assertEqual(calls[0]['settings']['defaultModel'], 'deepseek-v4.1-flash')
        self.assertFalse(calls[0]['settings']['retry']['enabled'])

    def test_answer_files_do_not_collide(self):
        first, first_meta, _ = self.run_agent()
        second, second_meta, _ = self.run_agent()
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertNotEqual(first_meta['answer-file'], second_meta['answer-file'])
        self.assertEqual(Path(first_meta['answer-file']).read_text(), 'final answer\n')

    def test_invalid_inputs_do_not_launch(self):
        for args, prompt in (((), '  '), (('--timeout-seconds', '0'), 'task'),
                             (('--working-directory', str(self.root / 'missing')), 'task')):
            with self.subTest(args=args):
                result, _, calls = self.run_agent(args=args, prompt=prompt)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(calls, [])

    def test_lint_accepts_agent_route_and_still_rejects_direct_calls(self):
        workflow = self.root / '.github/workflows/agent.yml'
        workflow.parent.mkdir(parents=True)
        workflow.write_text('steps:\n  - uses: valentinyeo/token-ladder/.github/actions/ladder-agent@main\n'
                            '    env:\n      OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}\n')
        command = [str(REPO / 'ci/lint-direct-model-calls'), str(self.root)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout)
        workflow.write_text(workflow.read_text() + '    run: curl https://api.deepseek.com/chat/completions\n')
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('ladder-agent@main', result.stdout)


if __name__ == '__main__':
    unittest.main()
