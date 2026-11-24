import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _load import ISOLATED_LOCAL_CONFIG, REPO, load_bin, tl

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
        self.assertEqual(result, 75)
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

    def test_pi_json_429_fixture_exiting_zero_marks_opencode_limited_and_runs_next_lane(self):
        stream = (Path(REPO) / "tests" / "fixtures" / "pi-opencode-429.jsonl").read_text()
        pools = {"opencode": {"state": "ON", "used_pct": 100.0, "funding": "prepaid",
                              "included_exhausted": True, "credits_usd": 23.28, "credit_floor_usd": 2,
                              "note": "monthly 100%, weekly 0%, 5-hour 0%; included allowance used up; using held prepaid credit"}}
        ran, said = [], []
        def runner(command):
            ran.append(command)
            return (0, stream, 52.0, False) if command == "opencode" else (0, "done", 3.0, False)
        code = pp.run_lanes(LANES, pools, {}, 1.1, runner=runner, limits_path=self.path, now=NOW,
                            say=said.append, launches_path=str(Path(self.tmp.name) / "launches.jsonl"))
        self.assertEqual(code, 0)
        self.assertEqual(ran, ["opencode", "vercel"])
        entry = pp.read_limits(self.path)["opencode"]
        self.assertEqual(entry["until"], NOW + 300)
        self.assertEqual(entry["source"], "429")
        self.assertEqual(entry["reason"], "hit a rate or usage limit")
        self.assertIn("429", entry["error_line"])
        self.assertIn("rate_limit_error", entry["error_line"])
        self.assertTrue(any("opencode hit a rate or usage limit" in line for line in said), said)

    def test_pi_json_429_fixture_end_to_end_cli_falls_through_and_exits_75_when_none_left(self):
        fixture = Path(REPO) / "tests" / "fixtures" / "pi-opencode-429.jsonl"
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        pools = home / "pools.json"
        pools.write_text(json.dumps({"opencode": {"state": "ON", "used_pct": 100.0, "funding": "prepaid",
                                                  "credits_usd": 23.28, "credit_floor_usd": 2}}))
        repo = Path(self.tmp.name) / "repo"
        (repo / "bin").mkdir(parents=True)
        (repo / "bin" / "plan-pick").write_text((Path(REPO) / "bin" / "plan-pick").read_text())
        cfg = {"lanes": [{"lane": "opencode", "pool": "opencode", "command": f"cat {fixture}"},
                         {"lane": "vercel", "pool": None, "command": "echo vercel-done"}]}
        (repo / "config.json").write_text(json.dumps(cfg))
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG}
        out = subprocess.run([sys.executable, str(repo / "bin" / "plan-pick"), "--pools", str(pools), "--run"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("vercel-done", out.stdout)
        self.assertIn("lane opencode hit a rate or usage limit", out.stderr)
        limits = json.loads((home / ".local/state/token-ladder/lane-limits.json").read_text())
        self.assertEqual(limits["opencode"]["source"], "429")
        cfg_only = {"lanes": [{"lane": "opencode", "pool": "opencode", "command": f"cat {fixture}"}]}
        (repo / "config.json").write_text(json.dumps(cfg_only))
        home2 = Path(self.tmp.name) / "home2"
        home2.mkdir()
        out2 = subprocess.run([sys.executable, str(repo / "bin" / "plan-pick"), "--pools", str(pools), "--run"],
                              capture_output=True, text=True,
                              env={"HOME": str(home2), "PATH": "/usr/bin:/bin",
                                   "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG})
        self.assertEqual(out2.returncode, 75, out2.stderr)
        self.assertIn("no lane left, nothing ran", out2.stderr)

    def test_pi_json_402_and_empty_streams_fall_through_on_exit_zero(self):
        pi_402 = "\n".join([
            json.dumps({"type": "session", "version": 3}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "content": [], "stopReason": "error",
                "errorMessage": '402: {"error":{"message":"Insufficient Balance"}}'}}),
            json.dumps({"type": "agent_end", "messages": [{
                "role": "assistant", "content": [], "stopReason": "error",
                "errorMessage": '402: {"error":{"message":"Insufficient Balance"}}'}], "willRetry": False}),
            json.dumps({"type": "agent_settled", "aborted": False}),
        ])
        pi_empty = "\n".join([
            json.dumps({"type": "session", "version": 3}),
            json.dumps({"type": "agent_start"}),
            json.dumps({"type": "agent_settled", "aborted": False}),
        ])
        ran = []
        def runner(command):
            ran.append(command)
            if command == "opencode":
                return (0, pi_402, 5.0, False)
            if command == "vercel":
                return (0, pi_empty, 2.0, False)
            return (0, "  \n", 1.0, False)
        code = pp.run_lanes(LANES, {}, {}, 1.1, runner=runner, limits_path=self.path, now=NOW,
                            say=lambda text: None, launches_path=str(Path(self.tmp.name) / "launches.jsonl"))
        self.assertEqual(code, 75)
        self.assertEqual(ran, ["opencode", "vercel", "codex"])
        limits = pp.read_limits(self.path)
        self.assertEqual(limits["opencode"]["until"], NOW + 12 * 3600)
        self.assertEqual(limits["opencode"]["reason"], "is out of credit")
        self.assertEqual(limits["vercel"]["reason"], "exited with no output")
        self.assertEqual(limits["vercel"]["source"], "silent exit")
        self.assertEqual(limits["codex"]["reason"], "exited with no output")

    def test_other_lane_json_formats_detect_rate_credit_quota_and_empty(self):
        agy_multiline = json.dumps({
            "status": "ERROR",
            "error": {"short_error": "quota exceeded", "status": "RESOURCE_EXHAUSTED"},
        }, indent=2)
        agy_empty = json.dumps({"status": "OK", "response": "   "}, indent=2)
        cursor_429 = json.dumps({"type": "result", "subtype": "error", "is_error": True,
                                 "result": "429 Too Many Requests"})
        cursor_empty = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": ""})
        hax_402 = json.dumps({"kind": "error", "error": "402 Payment Required"})
        hax_empty = "\n".join([
            json.dumps({"kind": "turn_usage", "usage": {"in_tokens": 10, "cached": 0, "output": 0}}),
            json.dumps({"type": "result", "text": ""}),
        ])
        cases = [(agy_multiline, "rate"), (agy_empty, "silent"), (cursor_429, "rate"),
                 (cursor_empty, "silent"), (hax_402, "credit"), (hax_empty, "silent")]
        for text, expected in cases:
            with self.subTest(expected=expected, sample=text[:40]):
                self.assertEqual(pp.limit_kind(0, text), expected)

    def test_recovered_pi_retry_and_answers_mentioning_429_stay_successful(self):
        recovered = "\n".join([
            json.dumps({"type": "turn_end", "message": {
                "role": "assistant", "content": [], "stopReason": "error",
                "errorMessage": "429: Rate limit exceeded"}}),
            json.dumps({"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3}),
            json.dumps({"type": "message_end", "message": {
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "Fixed the 429 handler."}]}}),
            json.dumps({"type": "agent_end", "messages": [{
                "role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "Fixed the 429 handler."}]}]}),
            json.dumps({"type": "auto_retry_end", "success": True, "attempt": 1}),
            json.dumps({"type": "agent_settled", "aborted": False}),
        ])
        cursor_ok = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                "result": "Fixed 429 and quota handling"})
        self.assertIsNone(pp.limit_kind(0, recovered))
        self.assertIsNone(pp.limit_kind(0, cursor_ok))
        code = pp.run_lanes(LANES, {}, {}, 1.1, runner=mock.Mock(return_value=(0, recovered, 15.0, False)),
                            limits_path=self.path, now=NOW, say=lambda text: None,
                            launches_path=str(Path(self.tmp.name) / "launches.jsonl"))
        self.assertEqual(code, 0)
        self.assertEqual(pp.read_limits(self.path), {})

    def test_rung_light_pi_429_records_nonzero_finish_and_falls_through(self):
        cfg = pp.load_config()
        lanes = pp.lanes_from(cfg)
        brief = Path(self.tmp.name) / "brief.txt"
        brief.write_text("Rung: light\n")
        launches = str(Path(self.tmp.name) / "launches.jsonl")
        ctx = pp.rung_context(str(brief), launches)
        stream = (Path(REPO) / "tests" / "fixtures" / "pi-opencode-429.jsonl").read_text()
        pi_429_out = pp.measured_output(io.StringIO(stream), stream)
        ok_stream = json.dumps({"type": "message_end", "message": {
            "role": "assistant", "stopReason": "stop",
            "content": [{"type": "text", "text": "https://github.com/a/b/pull/42"}]}})
        ok_out = pp.measured_output(io.StringIO(ok_stream), ok_stream)
        ran = []
        def runner(command):
            ran.append(command)
            return (0, pi_429_out, 52.0, False) if len(ran) == 1 else (0, ok_out, 10.0, False)
        code = pp.run_lanes(lanes, {}, cfg, 1.1, runner=runner, limits_path=self.path, now=NOW,
                            say=lambda text: None, launches_path=launches, rung=ctx)
        self.assertEqual(code, 0)
        self.assertEqual(len(ran), 2)
        self.assertIn("opencode-go", ran[0])
        self.assertIn("vercel-ai-gateway", ran[1])
        rows = [json.loads(line) for line in Path(launches).read_text().splitlines()]
        finishes = [row for row in rows if row.get("event") == "finish"]
        self.assertEqual([row["exit_code"] for row in finishes], [1, 0])
        self.assertEqual(pp.read_limits(self.path)["opencode"]["source"], "429")


if __name__ == "__main__":
    unittest.main()
