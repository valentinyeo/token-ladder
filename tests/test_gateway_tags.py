import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('tagged_ladder_call', str(REPO / 'ci' / 'ladder-call'))
ladder = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(ladder)


class GatewayTagsTests(unittest.TestCase):
    def test_metadata_fixtures(self):
        fixtures = json.loads((REPO / 'tests/fixtures/gateway-tags.json').read_text())
        for fixture in fixtures:
            with self.subTest(env=fixture['env']):
                self.assertEqual(ladder.gateway_tags(fixture['env']), fixture['tags'])

    def test_long_metadata_is_bounded_and_keeps_distinct_callers(self):
        env = {'GITHUB_REPOSITORY': 'owner/' + 'r' * 100, 'GITHUB_JOB': 'j' * 100}
        first = ladder.gateway_tags(env)[0]
        second = ladder.gateway_tags(dict(env, GITHUB_JOB='k' * 100))[0]
        self.assertLessEqual(len(first), 64)
        self.assertNotEqual(first, second)

    def test_gateway_headers_on_every_attempt_without_prompt_or_key_data(self):
        lane = {'name': 'vercel-ai-gateway', 'model': 'stub', 'url': 'https://example.invalid'}
        env = {'GITHUB_REPOSITORY': 'owner/multiprompt', 'GITHUB_JOB': 'verifier',
               'GITHUB_EVENT_NAME': 'pull_request', 'LADDER_CALL_PR': '42'}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                ladder.urllib.request, 'urlopen', return_value=response) as send:
            ladder.call_lane(lane, 'stub-secret', {'messages': [{'content': 'private prompt'}]})
            ladder.call_lane(lane, 'stub-secret', ladder.decide_request('private review'))
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            headers = dict((k.lower(), v) for k, v in call.args[0].header_items())
            self.assertEqual(headers['ai-reporting-tags'], 'caller=multiprompt-verifier,pr=42')

    def test_other_providers_get_no_reporting_header(self):
        lane = {'name': 'deepseek', 'model': 'stub', 'url': 'https://example.invalid'}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with mock.patch.object(ladder.urllib.request, 'urlopen', return_value=response) as send:
            ladder.call_lane(lane, 'stub-secret', {'messages': []})
        self.assertNotIn('ai-reporting-tags', dict((k.lower(), v) for k, v in send.call_args.args[0].header_items()))

    def test_composite_wires_trusted_metadata_as_environment(self):
        action = (REPO / '.github/actions/ladder-call/action.yml').read_text()
        self.assertIn('LADDER_CALL_CALLER: ${{ inputs.caller }}', action)
        self.assertIn('LADDER_CALL_PR: ${{ github.event.pull_request.number }}', action)
