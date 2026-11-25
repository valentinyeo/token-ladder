import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from _load import tl

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
TS = 1795608980.0
EXPECTED = ("Plan usage (token-ladder, 5 min old): Claude 74% wk · Codex 41% wk · "
            "Vercel Token Pass $28 of $150 · OpenCode credit $5.42 · "
            "Antigravity Gemini 43% wk, 5h 31% · CommandCode 9% mo. Source: plan-pick --plans.")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class HookPlanUsageLineTests(unittest.TestCase):
    def test_line_from_fixture_status(self):
        line = tl.plan_usage_line(fixture("hook-status.json"), fixture("hook-lane-credits.json"), TS + 300)
        self.assertEqual(line, EXPECTED)

    def test_retired_lanes_stay_out_of_the_line(self):
        line = tl.plan_usage_line(fixture("hook-status.json"), fixture("hook-lane-credits.json"), TS + 300)
        for retired in ("Cursor", "DeepSeek", "OpenRouter", "Grok"):
            self.assertNotIn(retired, line)

    def test_estimated_numbers_are_marked(self):
        status = fixture("hook-status.json")
        status["pools"]["google"]["estimated"] = True
        line = tl.plan_usage_line(status, fixture("hook-lane-credits.json"), TS + 300)
        self.assertIn("Antigravity Gemini 43% wk, 5h 31% est", line)

    def test_no_numbers_gives_no_line(self):
        self.assertIsNone(tl.plan_usage_line({}, {}, TS))


class HookPromptTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.home_patch = patch.object(tl, "HOME", self.home.name)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.status = os.path.join(self.home.name, "status.json")
        self.status_patch = patch.object(tl, "STATUS", self.status)
        self.status_patch.start()
        self.addCleanup(self.status_patch.stop)

    def write_status(self):
        with open(self.status, "w") as f:
            json.dump(fixture("hook-status.json"), f)
        state = os.path.join(self.home.name, ".local/state/token-ladder")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "lane-credits.json"), "w") as f:
            json.dump(fixture("hook-lane-credits.json"), f)

    def run_hook(self, session_id, argv=("hook-prompt",)):
        stdin = io.StringIO(json.dumps({"session_id": session_id}) if session_id else "{}")
        out = io.StringIO()
        with patch.object(sys, "argv", ["token-ladder", *argv]), patch.object(sys, "stdin", stdin), \
                patch.object(sys, "stdout", out):
            tl.hook_prompt()
        return out.getvalue().splitlines()

    def test_first_prompt_prints_one_line(self):
        self.write_status()
        lines = self.run_hook("session-a")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Plan usage (token-ladder, "))
        self.assertTrue(lines[0].endswith("Source: plan-pick --plans."))

    def test_same_session_is_suppressed_for_an_hour(self):
        self.write_status()
        self.assertEqual(len(self.run_hook("session-a")), 1)
        self.assertEqual(self.run_hook("session-a"), [])
        self.assertEqual(len(self.run_hook("session-b")), 1)

    def test_no_session_id_prints_every_time(self):
        self.write_status()
        self.assertEqual(len(self.run_hook(None)), 1)
        self.assertEqual(len(self.run_hook(None)), 1)

    def test_missing_status_is_silent(self):
        self.assertEqual(self.run_hook("session-a"), [])

    def test_broken_status_is_silent(self):
        with open(self.status, "w") as f:
            f.write("{not json")
        self.assertEqual(self.run_hook("session-a"), [])

    def test_account_flag_keeps_its_own_line(self):
        with open(self.status, "w") as f:
            json.dump({"accounts": [{"id": "max", "label": "Max", "email": "a@b", "week": {
                "used": 10.0, "time_gone": 40.0, "hours_left": 100.0}}], "onepassword": {}}, f)
        lines = self.run_hook("session-a", ("hook-prompt", "--account", "max"))
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Token max (Max, a@b): 10% used"))


if __name__ == "__main__":
    unittest.main()
