import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'ci'))
from model_families import author_families, filter_lanes, model_family


class ModelFamilyTests(unittest.TestCase):
    def test_model_ids_and_routing_prefixes(self):
        cases = {
            'deepseek-v4.1-flash': 'deepseek',
            'deepseek-flash': 'deepseek',
            'deepseek/deepseek-v4.1-flash-1017': 'deepseek',
            'opencode-go/deepseek-v4.1-flash': 'deepseek',
            'openrouter/deepseek/deepseek-v4.1-flash': 'deepseek',
            'openai/gpt-6.1-sol': 'openai',
            'gpt-6.1-sol': 'openai',
            'codex': 'openai',
            'o3-mini': 'openai',
            'moonshotai/kimi-k2.5': 'moonshotai',
            'kimi-k2.5': 'moonshotai',
            'z-ai/glm-5': 'zai',
            'zai/glm-5': 'zai',
            'glm-5': 'zai',
            'google/gemini-2.5-flash': 'google',
            'gemini-2.5-flash': 'google',
            'anthropic/claude-sonnet': 'anthropic',
            'claude-sonnet': 'anthropic',
            'cursor/composer-2.5': 'cursor',
            'composer-2.5': 'cursor',
            'cursor/claude-sonnet': 'anthropic',
            ' OpenAI/GPT-6.1-SOL ': 'openai',
        }
        for model, family in cases.items():
            with self.subTest(model=model):
                self.assertEqual(model_family(model), family)

    def test_unknown_family_is_rejected(self):
        for model in ('', 'unknown/model', 'not-deepseek', 'gptish'):
            with self.subTest(model=model), self.assertRaises(ValueError):
                model_family(model)

    def test_all_current_lanes_have_known_families(self):
        lanes = json.loads((REPO / 'ci' / 'lanes.json').read_text())
        self.assertEqual([model_family(lane['model']) for lane in lanes],
                         ['deepseek', 'deepseek', 'deepseek', 'deepseek', 'google', 'cursor'])


class AuthorFamilyTests(unittest.TestCase):
    def test_every_author_line_is_read_and_deduplicated(self):
        body = ('## Why\r\nAvoid self-review.\r\n'
                'Author model: opencode-go/deepseek-v4.1-flash\r\n'
                'Author model: openai/gpt-6.1-sol\r\n'
                'Author model: deepseek/deepseek-flash\r\n'
                'Author model: cursor/composer-2.5\r\n')
        self.assertEqual(author_families(body), {'deepseek', 'openai', 'cursor'})

    def test_missing_author_is_empty(self):
        self.assertEqual(author_families('No author metadata.'), set())
        self.assertEqual(author_families(None), set())

    def test_unknown_or_malformed_author_fails_even_after_known_author(self):
        for author in ('unknown/model', '', 'deepseek', 'openai/', 'openai/gpt extra'):
            with self.subTest(author=author), self.assertRaises(ValueError):
                author_families('Author model: openai/gpt-6.1-sol\n'
                                f'Author model: {author}\n')


class LaneFilterTests(unittest.TestCase):
    def setUp(self):
        self.lanes = json.loads((REPO / 'ci' / 'lanes.json').read_text())

    def test_deepseek_excludes_every_provider_for_that_family(self):
        kept = filter_lanes(self.lanes, {'deepseek'})
        self.assertEqual([lane['name'] for lane in kept], ['google', 'cursor'])
        self.assertEqual(len(self.lanes), 6)

    def test_multiple_authors_exclude_multiple_families(self):
        kept = filter_lanes(self.lanes, {'deepseek', 'google'})
        self.assertEqual([lane['name'] for lane in kept], ['cursor'])

    def test_no_exclusions_preserves_order_and_unknown_models(self):
        self.lanes.append({'name': 'future', 'model': 'unknown/model'})
        self.assertEqual(filter_lanes(self.lanes, set()), self.lanes)

    def test_all_excluded_is_empty(self):
        self.assertEqual(filter_lanes(self.lanes, {'deepseek', 'google', 'cursor'}), [])

    def test_unknown_lane_fails_closed_when_excluding(self):
        with self.assertRaises(ValueError):
            filter_lanes([{'name': 'future', 'model': 'unknown/model'}], {'deepseek'})


class AuthorFamiliesCliTests(unittest.TestCase):
    def run_parser(self, body):
        with tempfile.TemporaryDirectory() as scratch:
            event = Path(scratch) / 'event.json'
            event.write_text(json.dumps({'pull_request': {'body': body}}))
            return subprocess.run([sys.executable, str(REPO / 'ci' / 'model_families.py'),
                                   str(event)], capture_output=True, text=True, check=False)

    def test_prints_comma_separated_exclusions(self):
        proc = self.run_parser('Author model: openai/gpt-6.1-sol\n'
                               'Author model: opencode-go/deepseek-v4.1-flash')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), 'deepseek,openai')

    def test_missing_author_warns_and_fails_verifier(self):
        proc = self.run_parser(None)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('::warning::', proc.stderr)
        self.assertIn('no Author model:', proc.stderr)
        self.assertIn('::error::', proc.stderr)
        self.assertEqual(proc.stdout, '')

    def test_unknown_author_fails_without_exclusions(self):
        proc = self.run_parser('Author model: unknown/model')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('unknown model family', proc.stderr)
        self.assertEqual(proc.stdout, '')


if __name__ == '__main__':
    unittest.main()
