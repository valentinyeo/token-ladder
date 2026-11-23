import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _load import REPO, load_bin, tl

pp = load_bin("plan-pick")
NOW = 1795470000
LANES = [{"lane": "opencode", "pool": "opencode", "command": "opencode"},
         {"lane": "vercel", "credit": "vercel", "command": "vercel"},
         {"lane": "codex", "pool": "codex", "command": "codex"}]


class LaneHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "limits.json")

    def test_limit_metadata_survives_and_is_visible_in_picker(self):
        pp.mark_limited("opencode", 1, NOW, self.path, reason="provider throttled", source="429",
                        error_line='429 {"message":"Rate limit exceeded"}')
        entry = pp.read_limits(self.path)["opencode"]
        self.assertEqual(entry["until"], NOW + 3600)
        self.assertEqual(entry["source"], "429")
        self.assertIn("Rate limit exceeded", entry["error_line"])
        result = pp.pick(LANES, {}, pp.read_limits(self.path), NOW)
        for text in ("provider throttled", "429", "Rate limit exceeded"):
            self.assertIn(text, result["reason"])

    def test_limit_diagnostics_redact_credentials_before_persisting(self):
        pp.mark_limited("opencode", 1, NOW, self.path, reason="manual",
                        error_line='401 Authorization: Bearer fixture-key api_key="fixture-secret"')
        raw = Path(self.path).read_text()
        self.assertNotIn("fixture-key", raw)
        self.assertNotIn("fixture-secret", raw)
        self.assertIn("[REDACTED]", raw)

    def test_agy_timeout_summary_is_not_a_provider_quota_error(self):
        error = {"short_error": "print timeout after 45m0s", "status": "DEADLINE_EXCEEDED",
                 "retryable": False, "reason": "print_timeout"}
        tail = 'AGY_ERROR: ' + json.dumps(error) + '\n' + json.dumps({
            "status": "ERROR", "response": "Fixed the Zig comptime branch quota error", "error": error})
        self.assertIsNone(pp.limit_kind(3, tail))
        self.assertIsNone(pp.limit_kind(3, tail.split('\n')[-1]))
        error["short_error"] = "print timeout while waiting on task: fix quota error"
        self.assertIsNone(pp.limit_kind(3, 'AGY_ERROR: ' + json.dumps(error)))
        self.assertEqual(pp.limit_kind(1, 'AGY_ERROR: ' + json.dumps({
            "short_error": "quota exceeded", "status": "RESOURCE_EXHAUSTED"})), "rate")

    def test_fast_fail_cannot_be_reclassified_by_incidental_quota_text(self):
        tail = 'HTTP 400 {"error":{"message":"invalid quota parameter"}}'
        pp.run_lanes(LANES, {}, {}, 1.1, runner=mock.Mock(side_effect=[(1, tail, 1, False), (0, "done")]),
                     limits_path=self.path, now=NOW, say=lambda text: None,
                     launches_path=str(Path(self.tmp.name) / "launches.jsonl"))
        entry = pp.read_limits(self.path)["opencode"]
        self.assertEqual(entry["source"], "fast-fail 4xx")
        self.assertEqual(entry["until"], NOW + 300)
        self.assertEqual(entry["error_line"], tail)

    def test_waits_for_soonest_limit_only_above_codex_threshold(self):
        limits = {"opencode": NOW + 600, "vercel": NOW + 300}
        for pace in (1.3, 1.3001, None):
            pools = {"codex": {"state": "ON", "pace_ratio": pace}}
            got = pp.pick(LANES, pools, limits, NOW)
            if pace is not None and pace > 1.3:
                self.assertEqual(got["plan"], "none")
                self.assertEqual(got["next_free"], {"lane": "vercel", "at": NOW + 300})
                self.assertIsNone(got["command"])
            else:
                self.assertEqual(got["plan"], "codex")

    def test_wait_does_not_suggest_a_private_or_empty_credit_lane(self):
        pools = {"codex": {"state": "ON", "pace_ratio": 1.6},
                 "credit:vercel": {"state": "OFF", "note": "credit gone"}}
        got = pp.pick(LANES, pools, {"vercel": NOW + 300, "opencode": NOW + 600}, NOW)
        self.assertEqual(got["next_free"], {"lane": "opencode", "at": NOW + 600})

    def test_no_eligible_expiry_keeps_least_ahead_fallback(self):
        pools = {"opencode": {"state": "OFF"}, "credit:vercel": {"state": "OFF"},
                 "codex": {"state": "ON", "pace_ratio": 1.6}}
        got = pp.pick(LANES, pools, {"vercel": NOW + 300}, NOW)
        self.assertEqual(got["plan"], "codex")
        self.assertIn("least ahead", got["reason"])

    def test_available_lane_still_runs_when_codex_is_ahead(self):
        pools = {"codex": {"state": "ON", "pace_ratio": 1.6}}
        self.assertEqual(pp.pick(LANES, pools, {"opencode": NOW + 300}, NOW)["plan"], "vercel")

    def test_waiting_run_never_launches_a_limited_lane(self):
        Path(self.path).write_text(json.dumps({"opencode": NOW + 600, "vercel": NOW + 300}))
        runner = mock.Mock(side_effect=AssertionError("no lane may launch"))
        result = pp.run_lanes(LANES, {"codex": {"state": "ON", "pace_ratio": 1.6}}, {}, 1.1,
                              runner=runner, limits_path=self.path, now=NOW, say=lambda text: None,
                              launches_path=str(Path(self.tmp.name) / "launches.jsonl"))
        self.assertEqual(result, 4)
        runner.assert_not_called()

    def test_commandcode_idle_window_is_valid(self):
        fixtures = Path(REPO) / "tests" / "fixtures"
        credits = json.loads((fixtures / "commandcode-credits.json").read_text())
        subscription = json.loads((fixtures / "commandcode-subscription.json").read_text())
        credits["windowLimits"]["fiveHour"].update(used=0, resetAt=0)
        get = lambda url, key: subscription if url.endswith("subscriptions") else credits
        entry = dict(pp.balance_commandcode(get, lambda name: "fixture-key"), at=NOW)
        pool = pp.commandcode_pool(entry, NOW, {})
        self.assertEqual(pool["state"], "ON")
        self.assertEqual(pool["limit"], 70)
        self.assertIsNotNone(pool["pace_ratio"])
        self.assertIn("used", pool["note"])
        self.assertIn("pace", pool["note"])
        pp.save_credits({"commandcode": entry}, self.path)
        refresh = mock.Mock(side_effect=AssertionError("fresh reading must not refresh"))
        fresh = pp.plan_credits([{"credit": "commandcode"}], {}, NOW, self.path, refresh)
        self.assertEqual(fresh["commandcode"], entry)

    def test_idle_window_with_usage_or_exceeded_is_invalid(self):
        fixtures = Path(REPO) / "tests" / "fixtures"
        credits = json.loads((fixtures / "commandcode-credits.json").read_text())
        subscription = json.loads((fixtures / "commandcode-subscription.json").read_text())
        get = lambda url, key: subscription if url.endswith("subscriptions") else credits
        for values in ({"used": 1, "resetAt": 0}, {"used": 0, "resetAt": 0, "exceeded": True}):
            credits["windowLimits"]["fiveHour"].update(values)
            with self.assertRaises(ValueError):
                pp.balance_commandcode(get, lambda name: "fixture-key")

    def test_visibility_uses_cwd_remote_and_caches_per_repo_for_a_day(self):
        lane = {"lane": "commandcode", "credit": "commandcode", "public_only": True}
        brief = Path(self.tmp.name) / "brief.txt"
        brief.write_text("Fix the current checkout")
        run = mock.Mock(side_effect=[subprocess.CompletedProcess([], 0, "git@github.com:owner/project.git\n"),
                                     subprocess.CompletedProcess([], 0, '{"visibility":"PUBLIC"}')])
        cache = str(Path(self.tmp.name) / "visibility.json")
        got = pp.with_repo_visibility({}, [lane], str(brief), run, cache_path=cache, now=NOW)
        self.assertEqual(got, {})
        self.assertEqual(run.call_args_list[1].args[0],
                         ["gh", "repo", "view", "owner/project", "--json", "visibility"])
        brief.write_text("Repo owner/project\nhttps://github.com/owner/docs")
        cached = mock.Mock(side_effect=AssertionError("must use cached visibility"))
        self.assertEqual(pp.with_repo_visibility({}, [lane], str(brief), cached, cache_path=cache, now=NOW + 86399), {})
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"visibility":"PRIVATE"}'))
        got = pp.with_repo_visibility({}, [lane], str(brief), run, cache_path=cache, now=NOW + 86400)
        self.assertEqual(got["credit:commandcode"]["state"], "OFF")
        run.assert_called_once()

    def test_repository_detection_and_refusal_fixtures(self):
        fixtures = json.loads((Path(REPO) / "tests/fixtures/repo-visibility.json").read_text())
        lane = {"lane": "commandcode", "credit": "commandcode", "public_only": True}
        brief = Path(self.tmp.name) / "brief.txt"
        for i, fixture in enumerate(fixtures):
            with self.subTest(fixture=fixture):
                brief.write_text(fixture["brief"])
                answers = []
                if "remote" in fixture:
                    answers.append(subprocess.CompletedProcess([], 0, fixture["remote"]))
                if "repo" in fixture:
                    answers.append(subprocess.CompletedProcess([], 0, json.dumps({"visibility": fixture["visibility"]})))
                run = mock.Mock(side_effect=answers)
                got = pp.with_repo_visibility({}, [lane], str(brief), run,
                                              cache_path=str(brief) + str(i), now=NOW)
                self.assertEqual("credit:commandcode" not in got, fixture["allowed"])
                if "repo" in fixture:
                    self.assertEqual(run.call_args.args[0],
                                     ["gh", "repo", "view", fixture["repo"], "--json", "visibility"])

    def test_legacy_limits_still_parse_and_metadata_is_html_escaped(self):
        from test_dashboard import base_status
        raw = {"old": NOW + 10, "legacy": NOW + 20, "legacy:reason": "retired",
               "opencode": {"until": NOW + 30, "reason": "throttled", "source": "429",
                            "error_line": '<script>alert("unsafe")</script>'}}
        self.assertIsNotNone(pp.parse_limit(raw, "old", NOW))
        self.assertEqual(pp.parse_limit(raw, "legacy", NOW)["reason"], "retired")
        status = base_status(now=NOW, pools={}, limits=raw, credits={})
        with mock.patch.object(tl.time, "time", return_value=NOW):
            page = tl.render_dashboard(status)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('<script>alert("unsafe")</script>', page)
        self.assertIn("429", page)


if __name__ == "__main__":
    unittest.main()
