import contextlib
import io
import json
import os
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from _load import tl


TABLE = """TYPE       ACTION        LIMIT    USED    REMAINING    RESET
 token      write         100      0       100          N/A
 token      read          1000     6       994          51 minutes from now
 account    read_write    1000     13      987          23 hours from now
"""
BUDGET = {"used": 13, "limit": 1000, "reset": "23 hours from now"}
BACK_OFF = " Back off: no new key lookups unless the job needs one; read each key once per job."
FROZEN = " Frozen: key lookups only for live production work; tell Valentin."


class OnePasswordTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.home_patch = patch.object(tl, "HOME", self.home.name)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.token_file = os.path.join(tl.HOME, ".config/yeo-ux-agents/op-service-account.env")
        os.makedirs(os.path.dirname(self.token_file))
        with open(self.token_file, "w") as f:
            f.write("OP_SERVICE_ACCOUNT_TOKEN=test-service-account\n")

    def test_parse_normal_account_not_token_row(self):
        self.assertEqual(tl.parse_onepassword(TABLE), BUDGET)
        self.assertEqual(tl.parse_onepassword(TABLE.replace("23 hours from now", "N/A"))["reset"], "N/A")

    def test_parse_missing_account_row(self):
        with self.assertRaises(ValueError):
            tl.parse_onepassword("\n".join(TABLE.splitlines()[:-1]))

    def test_parse_garbage_and_invalid_counts(self):
        for text in ("", "not a table", "account read_write 1000 broken 987 soon",
                     "account read_write 0 13 0 soon", "account read_write 1000 -1 1001 soon",
                     "account read_write 1000 13 -1 soon", "account read_write 1000 13 987"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                tl.parse_onepassword(text)

    def test_parse_command_uses_file_token_only_in_child_env(self):
        with patch.dict(os.environ, OP_SERVICE_ACCOUNT_TOKEN="unrelated-env-token"), \
                patch.object(tl.subprocess, "run", return_value=types.SimpleNamespace(stdout=TABLE)) as run:
            self.assertEqual(tl.read_onepassword(), BUDGET)
            args, kwargs = run.call_args
            self.assertEqual(args[0], ["op", "service-account", "ratelimit"])
            self.assertEqual(kwargs["env"]["OP_SERVICE_ACCOUNT_TOKEN"], "test-service-account")
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 20)
            self.assertEqual(os.environ["OP_SERVICE_ACCOUNT_TOKEN"], "unrelated-env-token")

    def test_parse_failures_never_expose_cli_output_or_token(self):
        errors = (FileNotFoundError("test-service-account"),
                  subprocess.TimeoutExpired("op", 20, output="test-service-account"),
                  subprocess.CalledProcessError(1, "op", stderr="test-service-account"))
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(tl.subprocess, "run", side_effect=error):
                self.assertEqual(tl.read_onepassword(),
                                 {"error": f"ratelimit unavailable ({type(error).__name__})"})
        with patch.object(tl.subprocess, "run", return_value=types.SimpleNamespace(stdout="test-service-account")):
            self.assertEqual(tl.read_onepassword(), {"error": "ratelimit unavailable (ValueError)"})
        for content in ("test-service-account", "WRONG=test-service-account", "OP_SERVICE_ACCOUNT_TOKEN="):
            with open(self.token_file, "w") as f:
                f.write(content)
            with patch.object(tl.subprocess, "run") as run:
                self.assertEqual(tl.read_onepassword(), {"error": "ratelimit unavailable (ValueError)"})
                run.assert_not_called()
        os.remove(self.token_file)
        self.assertEqual(tl.read_onepassword(), {"error": "ratelimit unavailable (FileNotFoundError)"})

    def test_parse_collect_stores_and_mirrors_budget_each_run_even_after_failure(self):
        state = os.path.join(tl.HOME, "state")
        status = os.path.join(state, "status.json")
        cfg = {"publish": False, "mirrors": [{"ssh": "mirror", "users": ["client"]}]}
        bar = {"used": 10, "hours_left": 80, "time_gone": 50}
        plan = {"week": bar, "five_hour": bar}
        calls, mirrored = [], []
        op_calls = 0

        def run(cmd, **kwargs):
            nonlocal op_calls
            calls.append(cmd)
            if cmd[0] == "op":
                op_calls += 1
                if op_calls == 2:
                    raise subprocess.CalledProcessError(1, cmd, stderr="test-service-account")
                return types.SimpleNamespace(stdout=TABLE)
            self.assertNotIn("env", kwargs)
            if cmd[:3] == ["rsync", "-a", status]:
                with open(status) as f:
                    mirrored.append(json.load(f))
            return types.SimpleNamespace(returncode=0)

        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in (("STATE", state), ("STATUS", status)):
                stack.enter_context(patch.object(tl, name, value))
            for name, value in (("load_config", cfg), ("read_claude", plan.copy()),
                                ("read_codex", plan.copy()), ("read_openrouter", {}),
                                ("claude_email", None), ("codex_email", None), ("read_override", None),
                                ("read_reset_feed", {}), ("decide", ("codex", "test", [])),
                                ("build_accounts", []), ("dashboard_url", "test-dashboard"),
                                ("record_resources", {}), ("render_dashboard", "dashboard")):
                stack.enter_context(patch.object(tl, name, return_value=value))
            stack.enter_context(patch.object(tl.subprocess, "run", side_effect=run))
            stack.enter_context(contextlib.redirect_stdout(output))
            for expected in (BUDGET, {"error": "ratelimit unavailable (CalledProcessError)"}):
                tl.collect()
                with open(status) as f:
                    saved = json.load(f)
                    self.assertEqual(saved["onepassword"], expected)
                    self.assertEqual(set(saved["pools"]), {"claude", "codex", "grok", "google"})
                    self.assertEqual(saved["pools"]["claude"]["state"], "ON")
                self.assertEqual(mirrored[-1]["onepassword"], expected)
            self.assertEqual(op_calls, 2)
            self.assertEqual(len(mirrored), 2)
            self.assertIn(["rsync", "-a", status, "mirror:.local/state/token-ladder/status.json"], calls)
            installs = [cmd[-1] for cmd in calls if cmd[0] == "ssh" and "sudo" in cmd[-1]]
            self.assertEqual(len(installs), 2)
            self.assertIn(".local/state/token-ladder/status.json /home/client/.local/state/token-ladder/status.json",
                          installs[0])
            self.assertIn("1Password: 13/1000 reads today.", output.getvalue())
            self.assertIn("1Password: count unknown.", output.getvalue())
            self.assertNotIn("test-service-account", output.getvalue())
            for name in os.listdir(state):
                with open(os.path.join(state, name)) as f:
                    self.assertNotIn("test-service-account", f.read())

    def line_status(self, budget):
        bar = {"used": 10, "hours_left": 80, "time_gone": 50}
        s = {"ts": tl.time.time(), "mode": "codex", "advice": [],
             "claude": {"week": bar}, "codex": {"week": bar},
             "accounts": [{"id": "client", "label": "Client", "email": "test@example.invalid", "week": bar}]}
        if budget is not None:
            s["onepassword"] = budget
        return s

    def test_line_note_in_both_lines_at_10_50_80_percent(self):
        for limit in (1000, 2000):
            for percent, warnings in ((10, ""), (49, ""), (50, BACK_OFF), (79, BACK_OFF),
                                      (80, BACK_OFF + FROZEN), (100, BACK_OFF + FROZEN)):
                used = limit * percent // 100
                expected = f"1Password: {used}/{limit} reads today." + warnings
                s = self.line_status({"used": used, "limit": limit, "reset": "23 hours from now"})
                with self.subTest(percent=percent, limit=limit):
                    self.assertEqual(tl.onepassword_note(s), expected)
                    for mode in ("claude", "codex"):
                        s["mode"] = mode
                        for line in (tl.mode_line(s), tl.token_max_line(s, "client")):
                            self.assertTrue(line.endswith(expected), line)
                            self.assertEqual(line.count("1Password:"), 1)
                            self.assertNotIn("\n", line)

    def test_line_unknown_or_error_in_both_lines(self):
        for budget in (None, {}, {"error": "failed"}, {"used": None, "limit": 1000},
                       {"used": 10, "limit": 0}, {"used": "10", "limit": 1000},
                       dict(BUDGET, error="failed")):
            with self.subTest(budget=budget):
                s = self.line_status(budget)
                expected = "1Password: count unknown."
                self.assertEqual(tl.onepassword_note(s), expected)
                self.assertTrue(tl.mode_line(s).endswith(expected))
                self.assertTrue(tl.token_max_line(s, "client").endswith(expected))

    def test_line_inne_without_subscription_numbers_still_shows_budget(self):
        s = self.line_status(BUDGET)
        s["accounts"] = []
        self.assertEqual(tl.token_max_line(s, "client"),
                         "Token max: no numbers for client yet. 1Password: 13/1000 reads today.")
        s["accounts"] = [{"id": "client", "week": {"used": None}}]
        self.assertTrue(tl.token_max_line(s, "client").endswith("1Password: 13/1000 reads today."))

    def test_line_prompt_hook_for_ladder_and_inne(self):
        for argv, expected in ((["token-ladder", "hook-prompt"], ""),
                               (["token-ladder", "hook-prompt", "--account", "client"],
                                "1Password: 13/1000 reads today.\n")):
            with self.subTest(argv=argv), patch.object(tl, "load_status", return_value=self.line_status(BUDGET)), \
                    patch("sys.argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                tl.hook_prompt()
                self.assertTrue(out.getvalue().endswith(expected))


if __name__ == "__main__":
    unittest.main()
