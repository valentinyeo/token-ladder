import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _load import REPO, load_bin, tl

pp = load_bin("plan-pick")
AT = 1795385400
FIXTURES = Path(REPO) / "tests" / "fixtures"


class CommandCodeTests(unittest.TestCase):
    def setUp(self):
        self.credits = json.loads((FIXTURES / "commandcode-credits.json").read_text())
        self.subscription = json.loads((FIXTURES / "commandcode-subscription.json").read_text())
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.path = str(Path(self.home.name) / "credits.json")
        self.lanes = pp.lanes_from(pp.load_config())
        self.lane = next(lane for lane in self.lanes if lane["lane"] == "commandcode")

    def get(self, url, key):
        self.assertEqual(key, "fixture-key")
        return {"https://api.commandcode.ai/alpha/billing/credits": self.credits,
                "https://api.commandcode.ai/alpha/billing/subscriptions": self.subscription}[url]

    def entry(self):
        return dict(pp.balance_commandcode(self.get, lambda provider: "fixture-key"), at=AT)

    def test_real_reader_preserves_weighted_credit_units_and_reads_key_once(self):
        key = mock.Mock(return_value="fixture-key")
        data = pp.refresh_credits(["commandcode"], AT, self.path, self.get, key)
        key.assert_called_once_with("commandcode")
        self.assertAlmostEqual(data["commandcode"]["credits"], 69.999995275)
        self.assertEqual(data["commandcode"]["windows"]["fiveHour"]["cap"], 14)
        self.assertEqual(data["commandcode"]["windows"]["weekly"]["cap"], 35)
        self.assertEqual(pp.read_credits(self.path), data)
        pool = pp.commandcode_pool(data["commandcode"], AT, {})
        self.assertEqual(pool["state"], "ON")
        self.assertEqual(pool["limit"], 70)
        self.assertIn("~$100.00 Flash usage", pool["note"])
        self.assertIn("5h 4.725e-06/14 credits", pool["note"])
        self.assertIn("week 4.725e-06/35 credits", pool["note"])
        self.assertGreater(pool["pace_ratio"], 0)

    def test_each_exhausted_window_falls_through_even_with_extra_credits(self):
        self.credits["credits"]["purchasedCredits"] = 100
        for name in ("fiveHour", "weekly"):
            entry = self.entry()
            entry["windows"] = copy.deepcopy(entry["windows"])
            entry["windows"][name]["used"] = entry["windows"][name]["cap"]
            pools = {"credit:commandcode": pp.commandcode_pool(entry, AT, {})}
            got = pp.pick([self.lane, {"lane": "next", "command": "next"}], pools, now=AT)
            self.assertEqual(got["plan"], "next")

    def test_expired_windows_do_not_keep_lane_off(self):
        entry = self.entry()
        entry["windows"]["fiveHour"].update(used=14, exceeded=True, resetAt=(AT - 1) * 1000)
        self.assertEqual(pp.commandcode_pool(entry, AT, {})["state"], "ON")

    def test_monthly_empty_stale_missing_and_corrupt_readings_are_off(self):
        good = self.entry()
        for entry in (None, {}, {"at": AT}, dict(good, credits=0), dict(good, at=AT - 7 * 3600),
                      dict(good, period_end=AT), dict(good, windows={})):
            with self.subTest(entry=entry):
                self.assertEqual(pp.commandcode_pool(entry, AT, {})["state"], "OFF")

    def test_missing_reader_disables_lane_instead_of_assuming_credit(self):
        pools = pp.with_credits({}, [self.lane], {}, AT, self.path, refresh=lambda *args: {})
        self.assertEqual(pools["credit:commandcode"]["state"], "OFF")

    def test_non_goat_subscription_and_bad_windows_are_rejected(self):
        self.subscription["data"]["planId"] = "individual-go"
        with self.assertRaises(ValueError):
            self.entry()
        self.subscription["data"]["planId"] = "individual-goat"
        for cap in (None, True, "14", 0, -1):
            self.credits["windowLimits"]["fiveHour"]["cap"] = cap
            with self.assertRaises(ValueError):
                self.entry()

    def test_failed_read_keeps_old_timestamp_and_does_not_print_key(self):
        Path(self.path).write_text(json.dumps({"commandcode": self.entry()}))
        with mock.patch("sys.stderr") as stderr:
            data = pp.refresh_credits(["commandcode"], AT + 100, self.path,
                                      mock.Mock(side_effect=OSError("fixture-key")), lambda p: "fixture-key")
        self.assertEqual(data["commandcode"]["at"], AT)
        self.assertNotIn("fixture-key", str(stderr.write.call_args_list))

    def test_monthly_pace_blocks_lane_when_another_lane_has_room(self):
        entry = self.entry()
        entry["credits"] = 35
        pool = pp.commandcode_pool(entry, AT, {})
        self.assertGreater(pool["pace_ratio"], 1.1)
        got = pp.pick([self.lane, {"lane": "next", "command": "next"}], {"credit:commandcode": pool}, now=AT)
        self.assertEqual(got["plan"], "next")

    def test_plans_displays_live_balance_and_documented_allowance(self):
        plan = next(p for p in pp.plans_from(pp.load_config()) if p["vendor"] == "commandcode")
        line = pp.plan_line(dict(plan, usage=pp.commandcode_pool(self.entry(), AT, {})))
        for text in ("$10/month", "$100 Flash usage/month", "$20/5h", "$50/week",
                     "Limit: 70", "/14 credits", "/35 credits", "Flash usage"):
            self.assertIn(text, line)
        self.assertNotIn("public repos only", line)

    def test_all_repo_visibilities_are_allowed_without_lookup_including_burn(self):
        brief = Path(self.home.name) / "brief.txt"
        for text in ("Repo owner/private-repo", "Do some work"):
            brief.write_text(text)
            run = mock.Mock(side_effect=FileNotFoundError())
            pools = pp.with_repo_visibility({"credit:commandcode": {"state": "ON"}}, [self.lane], str(brief), run)
            run.assert_not_called()
            for burn in (None, {"lane": "commandcode", "until": AT + 60}):
                with self.subTest(brief=text, burn=burn):
                    result = pp.pick([self.lane], pools, now=AT, burn=burn)
                    self.assertEqual(result["plan"], "commandcode")

    def test_public_only_lane_refuses_private_internal_and_unknown_repo_including_burn(self):
        brief = Path(self.home.name) / "brief.txt"
        brief.write_text("Repo owner/private-repo (fresh clone).\nhttps://github.com/owner/public-docs")
        for visibility in ("PRIVATE", "INTERNAL", "", "PUBLIC"):
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, visibility))
            pools = pp.with_repo_visibility({"credit:commandcode": {"state": "ON"}},
                                            [dict(self.lane, public_only=True)], str(brief), run)
            run.assert_called_once_with(["gh", "repo", "view", "owner/private-repo", "--json", "visibility", "--jq", ".visibility"],
                                        capture_output=True, text=True, timeout=20)
            result = pp.pick([self.lane], pools, now=AT, burn={"lane": "commandcode", "until": AT + 60})
            self.assertEqual(result["plan"], "commandcode" if visibility == "PUBLIC" else "none")

    def test_public_only_lane_visibility_failure_and_ambiguous_brief_fail_closed(self):
        brief = Path(self.home.name) / "brief.txt"
        for text in ("Repo https://github.com/owner/private-repo.git", "Do some work"):
            brief.write_text(text)
            for error in (FileNotFoundError(), subprocess.TimeoutExpired("gh", 20)):
                pools = pp.with_repo_visibility({}, [dict(self.lane, public_only=True)],
                                                str(brief), mock.Mock(side_effect=error))
                self.assertEqual(pools["credit:commandcode"]["state"], "OFF")

    def test_public_only_lane_requires_all_brief_repositories_to_be_public(self):
        brief = Path(self.home.name) / "brief.txt"
        brief.write_text("https://github.com/owner/public\nhttps://github.com/owner/private")
        run = mock.Mock(side_effect=[subprocess.CompletedProcess([], 0, "PUBLIC"),
                                     subprocess.CompletedProcess([], 0, "PRIVATE")])
        pools = pp.with_repo_visibility({}, [dict(self.lane, public_only=True)], str(brief), run)
        self.assertEqual(pools["credit:commandcode"]["state"], "OFF")

    def test_worker_uses_configured_provider_and_preserves_secret_override(self):
        command = self.lane["command"]
        self.assertIn("COMMANDCODE_API_KEY", command)
        self.assertIn("--provider commandcode --model deepseek/deepseek-v4.1-flash", command)
        self.assertIn("-e ~/projects/token-ladder/tools/pi-commandcode.mjs", command)
        self.assertFalse(any(lane.get("public_only") for lane in self.lanes))
        shipped = json.loads((Path(REPO) / "ci" / "lanes.json").read_text())
        self.assertNotIn("commandcode", [lane["name"] for lane in shipped])

    def test_cli_runs_commandcode_for_private_internal_unknown_and_public_repos(self):
        root = Path(self.home.name)
        (root / "brief.txt").write_text("Repo owner/target-repo\n")
        gh = root / "gh"
        gh.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$HOME/gh-args"\nprintf "%s\\n" "$TEST_VISIBILITY"\n')
        gh.chmod(0o755)
        cfg = root / "config.json"
        cfg.write_text(json.dumps({"lanes": [dict(self.lane, command="printf commandcode-worker"),
                                             {"lane": "next", "command": "printf next-worker"}],
                                   "burn": {"lane": "commandcode", "until": "2099-01-01T00:00:00Z"}}))
        pools = root / "pools.json"
        pools.write_text(json.dumps({"credit:commandcode": {"state": "ON"}}))
        for visibility in ("PRIVATE", "INTERNAL", "", "PUBLIC"):
            env = dict(os.environ, HOME=str(root), PATH=str(root) + os.pathsep + os.environ.get("PATH", ""),
                       TOKEN_LADDER_LOCAL_CONFIG=str(cfg), TEST_VISIBILITY=visibility)
            out = subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), "--run", "--pools", str(pools)],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertFalse((root / "gh-args").exists())
            self.assertIn("commandcode-worker", out.stdout)
            self.assertNotIn("next-worker", out.stdout)
            self.assertNotIn("public repos only", out.stderr)

    def test_command_guard_allows_only_flash_on_commandcode(self):
        self.assertIsNone(tl.bash_refusal(self.lane["command"], {}))
        for model in ("deepseek/deepseek-v4.1-pro", "gpt-6.1-sol", "deepseek-v4.1-flash-fast"):
            self.assertIsNotNone(tl.bash_refusal(f"pi --provider commandcode --model {model} -p hi", {}))

    def test_pi_provider_registration(self):
        extension = (Path(REPO) / "tools" / "pi-commandcode.mjs").as_uri()
        script = f'import provider from {json.dumps(extension)}; provider({{registerProvider: (name, config) => console.log(JSON.stringify({{...config, name}}))}});'
        result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
        config = json.loads(result.stdout)
        self.assertEqual(config["name"], "commandcode")
        self.assertEqual(config["baseUrl"], "https://api.commandcode.ai/provider/v1")
        self.assertEqual(config["api"], "openai-completions")
        self.assertEqual(config["apiKey"], "$COMMANDCODE_API_KEY")
        self.assertEqual(config["models"][0]["id"], "deepseek/deepseek-v4.1-flash")
