import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from _load import ISOLATED_LOCAL_CONFIG, REPO, load_bin, tl

pp = load_bin("plan-pick")


def pool(state="ON", ratio=0.5):
    return {"state": state, "used_pct": 40.0, "pace_ratio": ratio}


LANES = [{"lane": "opencode", "pool": "opencode", "command": "run opencode"},
         {"lane": "vercel", "pool": None, "command": "run vercel"},
         {"lane": "openrouter", "pool": None, "command": "run openrouter"},
         {"lane": "deepseek", "pool": None, "command": "run deepseek"},
         {"lane": "codex", "pool": "codex", "command": "run codex"},
         {"lane": "cursor", "pool": "grok", "command": "run cursor"}]
T = 1_800_000_000


def pick(pools, limits=None, **kw):
    return pp.pick(LANES, pools, limits or {}, T, **kw)


def burn_until(delta_s=3600):
    return datetime.fromtimestamp(T + delta_s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def pick_with_burn(pools, burn_cfg, limits=None, **kw):
    cfg = {"burn": burn_cfg}
    burn = pp.parse_burn(cfg, LANES, T)
    lanes = pp.lanes_with_burn(LANES, burn)
    return pp.pick(lanes, pools, limits if limits is not None else {}, T, burn=burn, **kw)


class LaneOkTests(unittest.TestCase):
    def check_lane(self, spec, pools=None, limits=None, cloud=False, cfg=None):
        cfg = pp.load_config() if cfg is None else cfg
        out = io.StringIO()
        with mock.patch.object(pp, "load_config", return_value=cfg), \
                mock.patch.object(pp, "read_pools", return_value=pools or {}), \
                mock.patch.object(pp, "with_opencode", side_effect=lambda pools, cfg: pools), \
                mock.patch.object(pp, "with_credits", side_effect=lambda pools, lanes, cfg: pools), \
                mock.patch.object(pp, "with_antigravity_reserve", side_effect=lambda pools, cfg: pools), \
                mock.patch.object(pp, "read_limits", return_value=limits or {}), \
                mock.patch.object(pp.time, "time", return_value=T), redirect_stdout(out):
            code = pp.main(["--lane-ok", spec] + (["--cloud"] if cloud else []))
        return code, out.getvalue().strip()

    def test_skipped_cursor_refuses_cloud_launch_with_one_reason_line(self):
        code, reason = self.check_lane("cursor", limits={"cursor": T + 86400}, cloud=True)
        self.assertEqual(code, 3)
        self.assertIn("cursor", reason)
        self.assertIn("limited until", reason)
        self.assertEqual(len(reason.splitlines()), 1)

    def test_structured_skips_refuse_until_expired(self):
        for until in (T + 3600, burn_until()):
            with self.subTest(until=until):
                code, reason = self.check_lane("cursor", {"grok": pool()},
                                               {"cursor": {"until": until, "reason": "manual skip"}})
                self.assertEqual(code, 3)
                self.assertIn("limited until", reason)
        self.assertEqual(self.check_lane("cursor", {"grok": pool()},
                                         {"cursor": {"until": T, "reason": "expired"}})[0], 0)

    def test_lane_and_vendor_names_use_their_own_pool_not_the_first_usable_lane(self):
        for name, pool_name in (("cursor", "grok"), ("codex", "codex"), ("openai", "codex"),
                                ("claude", "claude"), ("anthropic", "claude"),
                                ("antigravity", "google"), ("google", "google"),
                                ("opencode", "opencode"), ("vercel", "credit:vercel"),
                                ("commandcode", "credit:commandcode"), ("openrouter", "credit:openrouter")):
            with self.subTest(name=name):
                self.assertEqual(self.check_lane(name, {pool_name: pool()})[0], 0)
                code, reason = self.check_lane(name, {pool_name: pool("OFF")})
                self.assertEqual(code, 3)
                self.assertIn("used up", reason)

    def test_ahead_of_pace_never_uses_the_picker_fallback_or_burn_override(self):
        cfg = dict(pp.load_config(), burn={"lane": "cursor", "until": burn_until()})
        for cloud in (False, True):
            code, reason = self.check_lane("cursor", {"grok": pool(ratio=1.5)}, cloud=cloud, cfg=cfg)
            self.assertEqual(code, 3)
            self.assertIn("pace 1.50x", reason)

    def test_expired_skip_and_pace_boundary_allow_the_lane(self):
        self.assertEqual(self.check_lane("cursor", {"grok": pool(ratio=1.1)}, {"cursor": T})[0], 0)

    def test_exhausted_percent_and_credit_unknown_refuse(self):
        for state in (dict(pool(), used_pct=100), dict(pool(), credit_unknown=True)):
            self.assertEqual(self.check_lane("codex", {"codex": state})[0], 3)

    def test_credit_missing_or_at_floor_refuses_but_above_floor_allows(self):
        for name in ("vercel", "openrouter"):
            self.assertEqual(self.check_lane(name)[0], 3)
            for balance, code in ((1.99, 3), (2, 3), (2.01, 0)):
                state = pp.credit_pool({"at": T, "usd": balance}, T, {})
                self.assertEqual(self.check_lane(name, {f"credit:{name}": state})[0], code)

    def test_unknown_lane_refuses_with_one_line(self):
        code, reason = self.check_lane("not-a-plan")
        self.assertEqual(code, 3)
        self.assertIn("unknown lane", reason)
        self.assertEqual(len(reason.splitlines()), 1)

    def test_unreadable_pool_state_refuses_without_leaking_the_exception(self):
        out = io.StringIO()
        with mock.patch.object(pp, "read_pools", side_effect=OSError("private detail")), redirect_stdout(out):
            code = pp.main(["--lane-ok", "cursor", "--cloud"])
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "cursor: cannot read pool state\n")

    def test_reason_stays_on_one_line(self):
        code, reason = self.check_lane("cursor", {"grok": dict(pool("OFF"), note="used\nup")})
        self.assertEqual(code, 3)
        self.assertEqual(reason, "cursor: used up: used up")

    def test_opencode_prepaid_can_run_after_included_allowance_is_exhausted(self):
        state = dict(pool(), used_pct=100, funding="prepaid", credits_usd=2.01, credit_floor_usd=2)
        self.assertEqual(self.check_lane("opencode", {"opencode": state})[0], 0)
        state["credits_usd"] = 2
        self.assertEqual(self.check_lane("opencode", {"opencode": state})[0], 3)

    def test_chat_only_pool_without_opt_in_refuses(self):
        cfg = {"lanes": [{"lane": "custom", "pool": "custom", "command": "unused"}]}
        code, reason = self.check_lane("custom", {"custom": dict(pool(), chat_only=True)}, cfg=cfg)
        self.assertEqual(code, 3)
        self.assertIn("chat-only", reason)


class PrepaidCreditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(pp, 'CREDITS', str(Path(tmp.name, 'lane-credits.json')))
        patch.start()
        self.addCleanup(patch.stop)

    def test_live_prepaid_credit_overrides_stale_estimate_not_monthly_usage(self):
        cfg = pp.load_config()
        live = {"at": NOW, "windows": month_full_windows(), "credits_usd": 24.64,
                "credits_at": NOW, "resets_at": {"monthly": NOW + 8 * 86400}}
        with mock.patch.object(pp, "read_opencode", return_value=live):
            got = pp.opencode_pool(NOW, cfg, history([
                {"at": NOW - 8 * 3600, "windows": month_full_windows(), "credits_usd": 5.87}]))
        self.assertEqual(got["state"], "ON")
        self.assertEqual(got["used_pct"], 100)
        self.assertEqual(got["credits_usd"], 24.64)
        self.assertEqual(pick({"opencode": got})["plan"], "opencode")
        self.assertIn("included allowance used up", got["note"])
        self.assertIn("monthly 100%, weekly 0%, 5-hour 0%", got["note"])

    def live_state(self, credit=24.64, **changes):
        live = {"at": NOW, "windows": month_full_windows(), "credits_usd": credit,
                "credits_at": NOW, "resets_at": {"monthly": NOW + 8 * 86400}}
        live.update(changes)
        with mock.patch.object(pp, "read_opencode", return_value=live):
            return pp.opencode_pool(NOW, pp.load_config(), tempfile.mkdtemp())

    def test_prepaid_credit_floor_is_inclusive_and_configurable(self):
        for credit, state in ((0, "OFF"), (1.99, "OFF"), (2, "OFF"), (2.01, "ON")):
            with self.subTest(credit=credit):
                got = self.live_state(credit)
                self.assertEqual(got["state"], state)
                self.assertIn("never top up", got["note"])
        cfg = dict(pp.load_config(), opencode_credit_floor_usd=25)
        with mock.patch.object(pp, "read_opencode", return_value={
                "at": NOW, "windows": month_full_windows(), "credits_usd": 24.64, "credits_at": NOW}):
            self.assertEqual(pp.opencode_pool(NOW, cfg)["state"], "OFF")

    def test_missing_stale_future_or_invalid_credit_never_runs_prepaid(self):
        for changes in ({"credits_usd": None}, {"credits_at": None}, {"credits_at": NOW - 8 * 3600},
                        {"credits_at": NOW + 60}, {"credits_usd": float("nan")},
                        {"credits_usd": float("inf")}):
            with self.subTest(changes=changes):
                self.assertEqual(self.live_state(**changes)["state"], "OFF")

    def test_prepaid_requires_extra_usage_and_auto_topup_off(self):
        for overrides in ({"opencode_extra_usage": False}, {"opencode_auto_topup": True},
                          {"opencode_auto_topup": None}):
            with mock.patch.object(pp, "load_config", return_value=dict(pp.load_config(), **overrides)):
                self.assertEqual(self.live_state()["state"], "OFF")

    def test_floor_lock_clears_on_a_live_read_above_the_floor_and_names_the_date(self):
        renewal = NOW + 8 * 86400
        stamp = datetime.fromtimestamp(renewal, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cfg = pp.load_config()
        live = {"at": NOW, "windows": month_full_windows(), "credits_usd": 2,
                "credits_at": NOW, "resets_at": {"monthly": renewal}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pp, "read_opencode", return_value=live):
            path = str(Path(tmp, "limits.json"))
            got = pp.with_opencode({}, cfg, NOW, limits_path=path)
            self.assertEqual(pp.read_limits(path)["opencode"]["until"], renewal)
            self.assertIn(f"skip until monthly renewal {stamp}", got["opencode"]["note"])
            result = pp.pick(LANES, got, pp.read_limits(path), NOW)
            self.assertEqual(result["plan"], "vercel")
            self.assertIn(f"opencode (limited until {stamp[:10]} ", result["reason"])
            live.update(at=NOW + 60, credits_at=NOW + 60, credits_usd=24.64)
            got = pp.with_opencode({}, cfg, NOW + 60, limits_path=path)
            self.assertNotIn("opencode", pp.read_limits(path))
            self.assertEqual(pp.pick(LANES, got, pp.read_limits(path), NOW + 60)["plan"], "opencode")
            live.update(at=renewal, windows={"monthly": 0, "weekly": 0, "5-hour": 0})
            got = pp.with_opencode({}, cfg, renewal, limits_path=path)
            self.assertEqual(pp.pick(LANES, got, pp.read_limits(path), renewal)["plan"], "opencode")

    def test_live_credit_above_the_floor_leaves_a_rate_limit_lock_alone(self):
        cfg = pp.load_config()
        live = {"at": NOW, "windows": month_full_windows(), "credits_usd": 24.64,
                "credits_at": NOW, "resets_at": {"monthly": NOW + 8 * 86400}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pp, "read_opencode", return_value=live):
            path = str(Path(tmp, "limits.json"))
            pp.mark_limited("opencode", 1, NOW, path)
            got = pp.with_opencode({}, cfg, NOW, limits_path=path)
            self.assertEqual(pp.read_limits(path)["opencode"]["until"], NOW + 3600)
            self.assertEqual(pp.pick(LANES, got, pp.read_limits(path), NOW)["plan"], "vercel")

    def test_missing_reader_reports_credit_unknown_not_used_up(self):
        with mock.patch.object(pp, "read_opencode", return_value=None):
            got = pp.opencode_pool(NOW, pp.load_config(), tempfile.mkdtemp())
        self.assertEqual(got["state"], "OFF")
        self.assertTrue(got["credit_unknown"])
        result = pick({"opencode": got})
        self.assertEqual(result["plan"], "vercel")
        self.assertIn("opencode (credit unknown: OpenCode live usage unavailable; credit unknown", result["reason"])
        self.assertNotIn("opencode (used up", result["reason"])

    def test_missing_credit_reader_on_exhausted_month_reports_credit_unknown(self):
        live = {"at": NOW, "windows": month_full_windows(), "credits_usd": None,
                "credits_at": None, "resets_at": {"monthly": NOW + 8 * 86400}}
        with mock.patch.object(pp, "read_opencode", return_value=live):
            got = pp.opencode_pool(NOW, pp.load_config(), history([live]))
        self.assertTrue(got["credit_unknown"])
        self.assertIn("credit unknown", got["note"])
        result = pick({"opencode": got})
        self.assertEqual(result["plan"], "vercel")
        self.assertIn("opencode (credit unknown:", result["reason"])

    def test_live_reader_executes_read_only_commands_and_keeps_zero_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "reader.py")
            calls = Path(tmp, "calls.jsonl")
            script.write_text("import json, sys\n"
                              f"with open({str(calls)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                              "if '--credits' in sys.argv:\n"
                              f" print(json.dumps({{'at': {NOW}, 'credits_usd': 0}}))\n"
                              "else:\n"
                              " print(json.dumps({'measured_at': '2027-01-15T08:00:00Z', 'windows': ["
                              "{'name': 'monthly', 'used': 100, 'resets_at': 1801000000}, "
                              "{'name': 'weekly', 'used': 0}, {'name': '5-hour', 'used': 0}]}))\n")
            reading = pp.read_opencode({"opencode_reader_command": [sys.executable, str(script)]})
            self.assertEqual(reading["credits_usd"], 0)
            self.assertEqual(reading["windows"], month_full_windows())
            self.assertEqual([json.loads(line) for line in calls.read_text().splitlines()],
                             [["--print"], ["--credits", "--print"]])

    def test_deepseek_direct_is_retired_from_workers_and_ci(self):
        cfg = pp.load_config()
        self.assertNotIn("deepseek", [lane["lane"] for lane in pp.lanes_from(cfg)])
        ci_lanes = json.loads(Path(REPO, "ci/lanes.json").read_text())
        self.assertNotIn("deepseek", [lane["name"] for lane in ci_lanes])

    def test_burn_includes_separate_measured_30_day_direct_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = pp.burn_report(pp.load_config(), NOW, tmp, str(Path(tmp, "missing")))
        text = "\n".join(pp.burn_lines(report))
        self.assertIn("$59.93", text)
        self.assertIn("8.11B tokens", text)
        self.assertIn("67,963 requests", text)
        self.assertIn("30 days", text)
        self.assertIn("$7.40 per 1B", text)


class BurnTests(unittest.TestCase):
    def test_active_burn_picks_cursor_over_pace(self):
        limits = {lane["lane"]: T + 60 for lane in LANES if lane["lane"] not in ("cursor", "openrouter")}
        got = pick_with_burn({"grok": pool("ON", 3.0), "codex": pool()}, {"lane": "cursor", "until": burn_until()})
        self.assertEqual(got["plan"], "cursor")
        self.assertIn("burn: cursor first until", got["reason"])

    def test_burn_lane_by_pool_name(self):
        limits = {lane["lane"]: T + 60 for lane in LANES if lane["lane"] != "cursor"}
        got = pick_with_burn({"grok": pool("ON", 3.0)}, {"lane": "grok", "until": burn_until()})
        self.assertEqual(got["plan"], "cursor")

    def test_used_up_burn_lane_falls_through(self):
        limits = {"vercel": T + 60, "deepseek": T + 60, "opencode": T + 60}
        got = pick_with_burn({"grok": pool("OFF", 3.0), "codex": pool()}, {"lane": "cursor", "until": burn_until()},
                             limits={**limits, "openrouter": T + 60})
        self.assertEqual(got["plan"], "codex")
        self.assertIn("cursor (used up", got["reason"])

    def test_expired_burn_restores_pace_skip(self):
        limits = {lane["lane"]: T + 60 for lane in LANES
                  if lane["lane"] not in ("cursor", "codex")}
        expired = datetime.fromtimestamp(T - 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        got = pick_with_burn({"grok": pool("ON", 3.0), "codex": pool("ON", 3.0)}, {"lane": "cursor", "until": expired},
                             limits=limits)
        self.assertEqual(got["plan"], "codex")
        self.assertIn("cursor (pace 3.00x", got["reason"])

    def test_malformed_burn_is_ignored(self):
        cases = ("x", 1, [], {"lane": 1, "until": burn_until()}, {"lane": "cursor"},
                 {"lane": "cursor", "until": "not-a-time"}, {"lane": "nosuch", "until": burn_until()})
        for bad in cases:
            with self.subTest(bad=bad):
                cfg = {"burn": bad} if bad != "x" else {"burn": "x"}
                self.assertIsNone(pp.parse_burn(cfg, LANES, T))
        limits = {lane["lane"]: T + 60 for lane in LANES
                  if lane["lane"] not in ("cursor", "openrouter", "codex")}
        got = pp.pick(LANES, {"grok": pool("ON", 3.0), "codex": pool("ON", 3.0)}, limits, T)
        self.assertEqual(got["plan"], "openrouter")

    def test_burn_does_not_change_claude_helper(self):
        pools = {"claude": pool("ON", 1.5)}
        self.assertFalse(pp.claude_helper(pools)["allowed"])
        self.assertIsNotNone(pp.parse_burn({"burn": {"lane": "cursor", "until": burn_until()}}, LANES, T))
        self.assertFalse(pp.claude_helper(pools)["allowed"])


class PaceLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name, "launches.jsonl"))
        self.reset = T + 6 * 86400
        self.start = self.reset - 7 * 86400
        rows = [{"at": self.start - 3600, "lane": "codex"},
                {"at": self.start + 60, "lane": "codex"},
                {"at": self.start + 120, "lane": "codex"},
                {"at": self.start + 30, "lane": "cursor"},
                {"at": self.start + 40, "lane": "vercel"},
                {"at": self.start + 50, "lane": None}]
        Path(self.path).write_text("".join(json.dumps(row) + "\n" for row in rows))

    def pools(self, codex_ratio=1.6):
        return {"codex": {"state": "ON", "used_pct": 19.0, "resets_at": self.reset,
                           "elapsed_pct": 11.9, "window_minutes": 10080, "pace_ratio": codex_ratio},
                "grok": {"state": "WARN", "used_pct": 91.4, "resets_at": T + 5 * 86400,
                         "elapsed_pct": 29.4, "window_minutes": 10080, "pace_ratio": 3.11}}

    def test_counts_launches_since_the_plans_own_window_start(self):
        report = pp.pace_log(self.pools(), {"lanes": LANES}, T, launches_path=self.path)
        codex = report["lanes"]["codex"]
        self.assertEqual(codex["since"], self.start)
        self.assertTrue(codex["since_reset"])
        self.assertEqual(codex["launches"], 2)
        self.assertEqual(codex["used_pct"], 19.0)
        self.assertEqual(codex["pace_ratio"], 1.6)
        self.assertEqual(report["lanes"]["cursor"]["launches"], 1)
        self.assertEqual(report["lanes"]["vercel"]["launches"], 1)

    def test_text_prints_the_launches_and_the_plan_counter(self):
        report = pp.pace_log(self.pools(), {"lanes": LANES}, T, launches_path=self.path)
        lines = list(pp.pace_log_lines(report))
        codex = next(line for line in lines if line.startswith("codex:"))
        self.assertIn("2 launches since", codex)
        self.assertIn("19% used", codex)
        self.assertIn("11.9% of 168 h gone", codex)
        self.assertIn("pace 1.60x", codex)
        self.assertIn("no tokens", " ".join(lines).lower())

    def test_lanes_without_a_counter_say_so_instead_of_guessing(self):
        lanes = [dict(lane, credit="vercel") if lane["lane"] == "vercel" else lane for lane in LANES]
        report = pp.pace_log({}, {"lanes": lanes}, T, launches_path=self.path)
        lines = list(pp.pace_log_lines(report))
        self.assertIn("credit lane, no plan counter", next(line for line in lines if line.startswith("vercel:")))
        self.assertIn("counter unknown", next(line for line in lines if line.startswith("codex:")))
        self.assertFalse(report["lanes"]["codex"]["since_reset"])

    def test_main_prints_json_or_text(self):
        pools_path = self.path + ".pools.json"
        Path(pools_path).write_text(json.dumps(self.pools()))
        for flags, check in (([], "Pace log:"), (["--json"], None)):
            out = io.StringIO()
            with mock.patch.object(pp.time, "time", return_value=T), redirect_stdout(out):
                code = pp.main(["--pace-log", self.path, "--pools", pools_path] + flags)
            self.assertEqual(code, 0)
            if check:
                self.assertIn(check, out.getvalue())
                self.assertIn("pace 1.60x", out.getvalue())
            else:
                self.assertEqual(json.loads(out.getvalue())["lanes"]["codex"]["launches"], 2)


class PickTests(unittest.TestCase):
    def test_first_lane_wins_when_nothing_reads_it(self):
        got = pick({"codex": pool(), "grok": pool()})
        self.assertEqual((got["plan"], got["command"]), ("opencode", "run opencode"))
        self.assertIn("no usage reader", got["reason"])

    def test_limited_lane_is_skipped_until_its_time(self):
        got = pick({}, {"vercel": T + 600, "opencode": T + 600})
        self.assertEqual(got["plan"], "openrouter")
        self.assertIn("vercel (limited until", got["reason"])
        self.assertEqual(pick({}, {"vercel": T - 1, "opencode": T + 600})["plan"], "vercel")

    def test_every_limited_deepseek_lane_falls_to_codex(self):
        limits = {"vercel": T + 60, "deepseek": T + 60, "opencode": T + 60, "openrouter": T + 60}
        self.assertEqual(pick({"codex": pool()}, limits)["plan"], "codex")

    def test_used_up_pool_is_skipped(self):
        limits = {"vercel": T + 60, "deepseek": T + 60, "openrouter": T + 60}
        got = pick({"opencode": {"state": "OFF", "note": "credit gone"}, "codex": pool()}, limits)
        self.assertEqual(got["plan"], "codex")
        self.assertIn("opencode (used up: credit gone)", got["reason"])

    def test_live_state_codex_off_and_cursor_over_pace_reach_openrouter(self):
        limits = {"vercel": T + 60, "deepseek": T + 60, "opencode": T + 60}
        got = pick({"codex": pool("OFF", 1.9), "grok": pool("WARN", 1.27)}, limits)
        self.assertEqual(got["plan"], "openrouter")
        until = datetime.fromtimestamp(T + 60, timezone.utc).strftime("%H:%M")
        self.assertIn(f"skipped opencode (limited until {until} UTC), vercel (limited until {until} UTC)", got["reason"])

    def test_pace_exactly_on_the_line_is_allowed(self):
        limits = {"vercel": T + 60, "deepseek": T + 60, "opencode": T + 60, "openrouter": T + 60}
        self.assertEqual(pick({"codex": pool("ON", 1.1)}, limits)["plan"], "codex")

    def test_unknown_state_is_not_skipped(self):
        self.assertEqual(pick({"opencode": {"state": "unknown"}}, {"vercel": T + 60, "deepseek": T + 60})["plan"], "opencode")

    def test_only_paced_lanes_left_picks_least_ahead_when_codex_is_not_over_1_3(self):
        limits = {lane["lane"]: T + 60 for lane in LANES if lane["lane"] not in ("codex", "cursor")}
        got = pick({"codex": pool("ON", 1.3), "grok": pool("ON", 1.2)}, limits)
        self.assertEqual(got["plan"], "cursor")
        self.assertIn("least ahead is cursor (pace 1.20x is above 1.1x)", got["reason"])

    def test_nothing_left_gives_none(self):
        limits = {lane["lane"]: T + 60 for lane in LANES}
        got = pick({}, limits)
        self.assertEqual(got["plan"], "none")
        self.assertIsNone(got["command"])

    def test_empty_and_garbage_input_do_not_crash(self):
        for bad in ({}, None, [], {"codex": "x", "grok": None}):
            with self.subTest(bad=bad):
                self.assertEqual(pp.pick(LANES, bad, None, T)["plan"], "opencode")

    def test_warn_line_comes_from_argument(self):
        limits = {"vercel": T + 60, "deepseek": T + 60, "opencode": T + 60, "openrouter": T + 60}
        self.assertEqual(pick({"codex": pool("ON", 1.5)}, limits, warn=2.0)["plan"], "codex")
        self.assertEqual(pick({"codex": pool("ON", 1.5)}, limits, warn=1.0)["plan"], "cursor")


class NoLaneTests(unittest.TestCase):
    def test_no_lane_names_the_next_free_lane_and_never_asks_to_pay(self):
        limits = {"vercel": T + 600, "deepseek": T + 3600, "opencode": T + 7200, "openrouter": T + 900}
        pools = {"codex": {"state": "OFF"}, "grok": {"state": "OFF"}}
        got = pp.pick(LANES, pools, limits, T)
        self.assertIsNone(got["command"])
        self.assertEqual(got["next_free"], {"lane": "vercel", "at": T + 600})
        self.assertIn("vercel frees at", got["reason"])
        self.assertIn("wait", got["reason"])
        self.assertNotIn("top", got["reason"].lower())


class RunTests(unittest.TestCase):
    def setUp(self):
        self.limits = str(Path(tempfile.mkdtemp()) / "limits.json")
        self.launches = Path(tempfile.mkdtemp()) / "launches.jsonl"
        self.ran, self.said = [], []

    def go(self, outcomes, pools=None, cfg=None):
        def runner(command):
            self.ran.append(command)
            return outcomes[command]
        return pp.run_lanes(LANES, pools or {}, cfg or {}, 1.1, runner, self.limits, T, self.said.append,
                            launches_path=str(self.launches))

    def logged(self):
        return [json.loads(line) for line in self.launches.read_text().splitlines()]

    def test_each_attempt_logs_its_lane_and_session(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-1"}):
            self.go({"run opencode": (1, "429 Too Many Requests"), "run vercel": (0, "done")})
        self.assertEqual([(e["lane"], e["session"], e["at"]) for e in self.logged()],
                         [("opencode", "sid-1", T), ("vercel", "sid-1", T)])

    def test_exhausted_percentage_is_skipped_even_if_state_says_on(self):
        self.assertEqual(self.go({"run vercel": (0, "done")},
                                 pools={"opencode": {"state": "ON", "used_pct": 100}}), 0)
        self.assertEqual(self.ran, ["run vercel"])

    def test_launch_records_multibot_caller_or_cwd(self):
        for name in ("worker chat", ""):
            with self.subTest(name=name), mock.patch.dict(os.environ, {"MULTIBOT_SESSION": name}):
                self.go({"run opencode": (0, "done")})
            self.assertEqual(self.logged()[-1]["caller"], name or os.getcwd())

    def test_no_lane_left_logs_a_null_lane(self):
        limits = {lane["lane"]: T + 60 for lane in LANES}
        Path(self.limits).write_text(json.dumps(limits))
        self.assertEqual(self.go({}), 75)
        self.assertEqual([e["lane"] for e in self.logged()], [None])

    def test_no_lane_left_says_when_the_next_lane_frees(self):
        limits = {lane["lane"]: T + 60 * (i + 1) for i, lane in enumerate(LANES)}
        Path(self.limits).write_text(json.dumps(limits))
        self.assertEqual(self.go({}), 75)
        said = " ".join(self.said)
        self.assertIn("frees at", said)
        self.assertIn("wait", said)
        self.assertNotIn("top", said.lower())

    def test_success_runs_one_lane(self):
        self.assertEqual(self.go({"run opencode": (0, "done")}), 0)
        self.assertEqual(self.ran, ["run opencode"])
        self.assertEqual(pp.read_limits(self.limits), {})

    def test_429_marks_the_lane_and_runs_the_next(self):
        code = self.go({"run opencode": (1, '429: {"error":"Too Many Requests"}'), "run vercel": (0, "done")})
        self.assertEqual(code, 0)
        self.assertEqual(self.ran, ["run opencode", "run vercel"])
        self.assertEqual(pp.read_limits(self.limits)["opencode"], {"until": T + 300,
                         "reason": "hit a rate or usage limit", "source": "429",
                         "error_line": '429: {"error":"Too Many Requests"}'})
        self.assertTrue(any("opencode hit a rate or usage limit" in line for line in self.said), self.said)

    def test_empty_credit_is_skipped_longer(self):
        Path(self.limits).write_text(json.dumps({"opencode": T + 3600, "openrouter": T + 3600}))
        self.go({"run vercel": (1, '402: {"error":{"message":"Insufficient Balance"}}'), "run deepseek": (0, "ok")})
        self.assertEqual(pp.read_limits(self.limits)["vercel"]["until"], T + 12 * 3600)

    def test_clean_exit_with_no_output_marks_limited_and_runs_next(self):
        code = self.go({"run opencode": (0, "  \n"), "run vercel": (0, "done")})
        self.assertEqual(code, 0)
        self.assertEqual(self.ran, ["run opencode", "run vercel"])
        self.assertIn("opencode", pp.read_limits(self.limits))
        self.assertTrue(any("exited with no output" in line for line in self.said), self.said)

    def test_clean_exit_that_mentions_429_is_done_not_retried(self):
        self.assertEqual(self.go({"run opencode": (0, "fixed the 429 handler")}), 0)
        self.assertEqual(self.ran, ["run opencode"])

    def test_failed_exit_with_no_output_counts_as_limited(self):
        self.go({"run opencode": (1, "  \n"), "run vercel": (0, "ok")})
        self.assertIn("opencode", pp.read_limits(self.limits))
        self.assertTrue(any("exited with no output" in line for line in self.said), self.said)

    def test_ordinary_failure_is_returned_not_retried(self):
        self.assertEqual(self.go({"run opencode": (2, "SyntaxError in your code")}), 2)
        self.assertEqual(self.ran, ["run opencode"])
        self.assertEqual(pp.read_limits(self.limits), {})

    def test_400_fast_fail_runs_the_next_lane(self):
        msg = '400 {"error":{"message":"The model request was rejected."}}'
        code = self.go({"run opencode": (1, msg), "run vercel": (0, "done")})
        self.assertEqual(code, 0)
        self.assertEqual(self.ran, ["run opencode", "run vercel"])
        self.assertEqual(pp.read_limits(self.limits)["opencode"]["source"], "fast-fail 4xx")
        self.assertEqual(pp.read_limits(self.limits)["opencode"]["until"], T + 300)
        self.assertTrue(any("skipped lane opencode: model request was rejected" in line for line in self.said), self.said)

    def test_failure_after_work_does_not_fall_through(self):
        msg = '400 {"error":{"message":"The model request was rejected."}}'
        self.assertEqual(self.go({"run opencode": (1, msg, 5.0, True)}), 1)
        self.assertEqual(self.ran, ["run opencode"])
        self.assertEqual(pp.read_limits(self.limits), {})

    def test_success_that_mentions_a_rate_limit_is_kept(self):
        self.assertEqual(self.go({"run opencode": (0, "added a rate limit, 429 on overflow")}), 0)
        self.assertEqual(pp.read_limits(self.limits), {})

    def test_every_lane_limited_stops_loudly(self):
        outcomes = {lane["command"]: (1, "429 Too Many Requests") for lane in LANES}
        self.assertEqual(self.go(outcomes), 75)
        self.assertEqual(len(self.ran), len(LANES))
        self.assertIn("no lane left, nothing ran", self.said[-1])

    def test_hours_come_from_config(self):
        self.go({"run opencode": (1, "429"), "run vercel": (0, "ok")}, cfg={"rate_limited_minutes": 180})
        self.assertEqual(pp.read_limits(self.limits)["opencode"]["until"], T + 3 * 3600)

    def test_limit_kind_reads_real_error_texts(self):
        cases = {"Error: You've hit your usage limit. Try again later": "rate",
                 "ERROR: exceeded retry limit, last status: 429 Too Many Requests": "rate",
                 '402: {"error":{"message":"Insufficient Balance","type":"unknown_error"}}': "credit",
                 "RESOURCE_EXHAUSTED": "rate",
                 '403 {"error":{"code":"challenge","message":"This request requires a challenge to be completed."}}': "blocked",
                 "": "silent",
                 "TypeError: x is undefined": None}
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(pp.limit_kind(1, text), kind)

    def test_fast_fail_reads_provider_errors(self):
        cases = {('{"statusCode":400,"message":"The model request was rejected"}', 5.0, False):
                     "model request was rejected",
                 ('HTTP 503 upstream unavailable', 2.0, False): "provider HTTP error",
                 ("unauthorized: invalid api key", 1.0, False): "provider auth error",
                 ('stopReason":"error"', 10.0, False): "agent error before work",
                 ("SyntaxError in your code", 5.0, False): None,
                 ('{"statusCode":400}', 5.0, True): None,
                 ('{"statusCode":400}', 35.0, False): None}
        for (text, elapsed, work), kind in cases.items():
            with self.subTest(text=text[:40]):
                self.assertEqual(pp.fast_fail_kind(1, text, elapsed, work), kind)


class CreditTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(tempfile.mkdtemp()) / "credits.json")

    def fake_get(self, url, key):
        self.assertEqual(key, "k")
        return {"https://api.deepseek.com/user/balance": {"balance_infos": [
                    {"currency": "USD", "total_balance": "11.30"}, {"currency": "CNY", "total_balance": "99"}]},
                "https://ai-gateway.vercel.sh/v1/credits": {"balance": "5.3", "token_pass": {"remaining": "106.2"}},
                "https://openrouter.ai/api/v1/credits": {"data": {"total_credits": 777.98, "total_usage": 771.42}},
                "https://openrouter.ai/api/v1/key": {"data": {"limit_remaining": 49.99}}}[url]

    def test_readers_parse_each_provider(self):
        data = pp.refresh_credits(["deepseek", "vercel", "openrouter"], T, self.path, self.fake_get, lambda p: "k")
        self.assertEqual({k: v["usd"] for k, v in data.items()}, {"deepseek": 11.3, "vercel": 106.2, "openrouter": 6.56})
        self.assertEqual(pp.read_credits(self.path)["vercel"]["at"], T)

    def test_failed_reader_keeps_the_old_value(self):
        Path(self.path).write_text(json.dumps({"vercel": {"usd": 50, "at": T - 99}}))
        def broken(url, key):
            raise OSError("down")
        data = pp.refresh_credits(["vercel"], T, self.path, broken, lambda p: "k")
        self.assertEqual(data["vercel"], {"usd": 50, "at": T - 99})

    def test_under_floor_is_off_and_the_next_lane_runs(self):
        Path(self.path).write_text(json.dumps({"vercel": {"usd": 1.2, "at": T - 60}, "deepseek": {"usd": 11.3, "at": T - 60}}))
        lanes = [dict(lane, credit=lane["lane"]) if lane["lane"] in ("vercel", "deepseek") else lane for lane in LANES]
        pools = pp.with_credits({"opencode": pool("OFF")}, lanes, {}, T, self.path,
                                refresh=lambda *a: self.fail("refreshed"))
        self.assertEqual(pools["credit:vercel"]["state"], "OFF")
        got = pp.pick(lanes, pools, {"opencode": T + 60, "openrouter": T + 60}, T)
        self.assertEqual(got["plan"], "deepseek")
        self.assertIn("vercel (used up: $1.20 credit left, under $2)", got["reason"])
        self.assertIn("$11.30 credit left", got["reason"])

    def test_old_reading_is_refreshed_and_stale_one_ignored(self):
        Path(self.path).write_text(json.dumps({"vercel": {"usd": 0.5, "at": T - 7 * 3600}}))
        lanes = [{"lane": "vercel", "credit": "vercel", "command": "x"}]
        asked = []
        pools = pp.with_credits({}, lanes, {}, T, self.path, refresh=lambda names, now, path: asked.append(names) or pp.read_credits(path))
        self.assertEqual(asked, [["vercel"]])
        self.assertNotIn("credit:vercel", pools)

    def test_floor_comes_from_config(self):
        entry = {"usd": 5, "at": T}
        self.assertEqual(pp.credit_pool(entry, T, {})["state"], "ON")
        self.assertEqual(pp.credit_pool(entry, T, {"credit_floor_usd": 10})["state"], "OFF")


class PassCreditTests(unittest.TestCase):
    def setUp(self):
        self.path = str(Path(tempfile.mkdtemp()) / "credits.json")

    def api(self, url, key):
        self.assertEqual((url, key), ("https://ai-gateway.vercel.sh/v1/credits", "k"))
        return {"balance": "1.2850310396",
                "token_pass": {"remaining": "56.56", "resets_at": "2026-12-05T08:00:00.000Z"},
                "total_used": "632.1927432624"}

    def vercel_plan(self):
        return next(plan for plan in pp.plans_from(pp.load_config()) if plan["vendor"] == "vercel")

    def test_the_balance_is_the_pass_remaining_not_the_top_up_credits(self):
        data = pp.refresh_credits(["vercel"], T, self.path, self.api, lambda p: "k")
        self.assertEqual(data["vercel"]["usd"], 56.56)
        self.assertEqual(data["vercel"]["resets_at"], "2026-12-05T08:00:00.000Z")
        self.assertEqual(data["vercel"]["at"], T)

    def test_new_spend_is_rounded_without_pinning_the_dashboard_snapshot(self):
        def api(url, key):
            response = self.api(url, key)
            response["token_pass"]["remaining"] = "56.486768176"
            return response
        data = pp.refresh_credits(["vercel"], T, self.path, api, lambda p: "k")
        self.assertEqual(data["vercel"]["usd"], 56.49)

    def test_the_plan_states_the_110_cycle_allowance(self):
        plan = self.vercel_plan()
        self.assertEqual(plan["usd_per_cycle"], 110)
        self.assertEqual(plan["credit"], "vercel")
        entry = {"usd": 56.52, "resets_at": "2026-12-05T08:00:00.000Z", "at": T}
        line = pp.plan_line(plan, entry)
        self.assertIn("$56.52 of $110 left this cycle, resets 2026-12-05T08:00:00.000Z.", line)
        self.assertNotIn("150", line)

    def test_a_plan_without_a_reading_or_a_cycle_shows_no_balance(self):
        plan = self.vercel_plan()
        self.assertNotIn("left this cycle", pp.plan_line(plan))
        self.assertNotIn("left this cycle", pp.plan_line(dict(plan, usd_per_cycle=None), {"usd": 56.52, "at": T}))
        self.assertIn("$56.52 of $110 left this cycle.", pp.plan_line(plan, {"usd": 56.52, "at": T}))

    def test_only_a_fresh_reading_is_quoted(self):
        plan = self.vercel_plan()
        Path(self.path).write_text(json.dumps({"vercel": {"usd": 56.52, "at": T - 60}}))
        self.assertEqual(pp.plan_credits([plan], {}, T, self.path, refresh=self.no_refresh)["vercel"]["usd"], 56.52)

    def test_a_stale_reading_that_cannot_be_refreshed_is_dropped(self):
        plan = self.vercel_plan()
        Path(self.path).write_text(json.dumps({"vercel": {"usd": 56.52, "at": T - 7 * 3600}}))
        self.assertEqual(pp.plan_credits([plan], {}, T, self.path,
                                         refresh=lambda names, now, path: pp.read_credits(path)), {})

    def no_refresh(self, names, now, path):
        self.fail("refreshed a reading that was fresh: %s" % names)

    def test_a_missing_reading_is_fetched_and_shown_against_the_cycle_allowance(self):
        def refresh(names, now, path):
            return pp.refresh_credits(names, now, path, self.api, lambda p: "k")
        lines = pp.plan_lines([self.vercel_plan()], {}, T, self.path, refresh=refresh)
        self.assertIn("$56.56 of $110 left this cycle, resets 2026-12-05T08:00:00.000Z.", lines[0])

    def test_an_old_reading_is_refreshed(self):
        plan = self.vercel_plan()
        Path(self.path).write_text(json.dumps({"vercel": {"usd": 56.52, "at": T - 7 * 3600}}))
        asked = []
        got = pp.plan_credits([plan], {}, T, self.path, refresh=lambda names, now, path: asked.append(names) or
                              {"vercel": {"usd": 56.6, "at": now}})
        self.assertEqual(asked, [["vercel"]])
        self.assertEqual(got["vercel"]["usd"], 56.6)

    def test_every_plan_keeps_one_line(self):
        plans = pp.plans_from(pp.load_config())
        lines = pp.plan_lines(plans, {}, T, self.path, refresh=lambda names, now, path: {})
        self.assertEqual(len(lines), len(plans))
        for line in lines:
            self.assertNotIn("\n", line)


class LimitTests(unittest.TestCase):
    def test_mark_limited_writes_and_drops_expired(self):
        path = str(Path(tempfile.mkdtemp()) / "sub" / "limits.json")
        Path(path).parent.mkdir()
        Path(path).write_text(json.dumps({"old": T - 10, "codex": T + 99}))
        until = pp.mark_limited("vercel", 2, T, path)
        self.assertEqual(until, T + 7200)
        self.assertEqual(pp.read_limits(path), {"codex": T + 99, "vercel": {
            "until": T + 7200, "reason": "manual skip", "source": "manual", "error_line": ""}})

    def test_unreadable_limits_are_empty(self):
        self.assertEqual(pp.read_limits("/nonexistent/x.json"), {})


class ClaudeHelperTests(unittest.TestCase):
    def test_under_pace_is_allowed(self):
        got = pp.claude_helper({"claude": pool("ON", 0.84)})
        self.assertTrue(got["allowed"])
        self.assertIn("pace 0.84x, within 1.1x", got["reason"])

    def test_over_pace_is_refused(self):
        got = pp.claude_helper({"claude": pool("ON", 1.5)})
        self.assertFalse(got["allowed"])
        self.assertIn("pace 1.50x is above 1.1x", got["reason"])

    def test_exactly_on_the_line_is_allowed(self):
        self.assertTrue(pp.claude_helper({"claude": pool("ON", 1.1)})["allowed"])

    def test_off_is_refused_even_under_pace(self):
        got = pp.claude_helper({"claude": pool("OFF", 0.1)})
        self.assertFalse(got["allowed"])
        self.assertIn("used up", got["reason"])

    def test_unknown_pace_is_allowed(self):
        got = pp.claude_helper({"claude": pool("ON", None)})
        self.assertTrue(got["allowed"])
        self.assertIn("pace unknown", got["reason"])

    def test_missing_reading_is_allowed(self):
        for bad in ({}, None, [], {"claude": "x"}):
            with self.subTest(bad=bad):
                self.assertTrue(pp.claude_helper(bad)["allowed"])

    def test_answer_ignores_other_plans(self):
        pools = {"deepseek": pool("ON", 0.1), "codex": pool("ON", 0.1), "claude": pool("ON", 2.0)}
        self.assertFalse(pp.claude_helper(pools)["allowed"])
        pools["claude"] = pool("ON", 0.5)
        pools["deepseek"] = pool("ON", 3.0)
        self.assertTrue(pp.claude_helper(pools)["allowed"])

    def test_warn_line_comes_from_argument(self):
        pools = {"claude": pool("ON", 1.5)}
        self.assertFalse(pp.claude_helper(pools)["allowed"])
        self.assertTrue(pp.claude_helper(pools, warn=2.0)["allowed"])


NOW = 1_800_000_000


def history(rows):
    tmp = tempfile.mkdtemp()
    Path(tmp, "opencode-history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp


def sessions(rows):
    tmp = Path(tempfile.mkdtemp(), "--tmp-work--")
    tmp.mkdir()
    tmp.joinpath("run.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(tmp.parent)


def lane_use(at, provider="opencode-go", tokens=0, cost=None):
    usage = {"totalTokens": tokens}
    if cost is not None:
        usage["cost"] = {"total": cost}
    return {"type": "message", "message": {"role": "assistant", "provider": provider, "model": "deepseek-v4.1-flash",
                                             "timestamp": int(at * 1000), "usage": usage}}


def month_full_windows():
    return {"5-hour": 0.0, "weekly": 0.0, "monthly": 100.0}


class AnchoredCreditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.credits = self.root / 'lane-credits.json'
        self.launches = self.root / 'launches.jsonl'
        self.store = self.root / 'sessions'
        self.store.mkdir()
        patch = mock.patch.object(pp, 'CREDITS', str(self.credits))
        patch.start()
        self.addCleanup(patch.stop)
        self.anchor()

    def anchor(self, usd=24.64, at=NOW - 3600):
        self.credits.write_text(json.dumps({'opencode': {'usd': usd, 'at': at,
                                                        'source': 'Valentin dashboard screenshot'}}))

    def run_log(self, name, rows, cwd='/tmp/brief'):
        path = self.store / name / 'run.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{'type': 'session', 'id': name, 'cwd': cwd}] + rows
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def state(self, live=None, now=NOW, unavailable=False):
        reading = {'at': now, 'windows': month_full_windows(),
                   'resets_at': {'monthly': NOW + 8 * 86400}}
        reading.update(live or {})
        with mock.patch.object(pp, 'read_opencode', return_value=None if unavailable else reading):
            return pp.opencode_pool(now, pp.load_config(), history([reading]),
                                    sessions=str(self.store), launches_path=str(self.launches))

    def test_anchor_without_runs_reopens_prepaid_lane(self):
        reading = {'at': NOW, 'windows': month_full_windows(),
                   'resets_at': {'monthly': NOW + 8 * 86400}}
        with mock.patch.object(pp, 'read_opencode', return_value=reading):
            got = pp.opencode_pool(NOW, pp.load_config(), history([reading]))
        self.assertEqual(pick({'opencode': got})['plan'], 'opencode')

    def test_anchor_minus_all_recorded_run_costs_selects_opencode(self):
        self.run_log('one', [lane_use(NOW - 3700, cost=90), lane_use(NOW - 600, cost=1.25),
                             lane_use(NOW - 500, cost=0.75),
                             lane_use(NOW - 400, provider='vercel-ai-gateway', cost=90)])
        self.run_log('two', [lane_use(NOW - 300, cost=2), lane_use(NOW + 1, cost=90)])
        got = self.state()
        self.assertAlmostEqual(got['credits_usd'], 20.64)
        self.assertEqual(pick({'opencode': got})['plan'], 'opencode')
        self.assertIn('anchored', got['note'])
        self.assertIn('Valentin dashboard screenshot', got['note'])
        self.assertAlmostEqual(self.state()['credits_usd'], 20.64)
        self.assertEqual(json.loads(self.credits.read_text())['opencode']['usd'], 24.64)

    def test_unknown_cost_uses_default_once_per_run_and_reports_it(self):
        self.run_log('one', [lane_use(NOW - 600), lane_use(NOW - 500), lane_use(NOW - 400, cost=1)])
        self.run_log('two', [lane_use(NOW - 300, cost=0)])
        got = self.state()
        self.assertAlmostEqual(got['credits_usd'], 23.14)
        self.assertIn('$0.50', got['note'])
        self.assertIn('1 run', got['note'])

    def test_launch_without_usage_is_charged_but_matched_launch_is_not_doubled(self):
        rows = [{'at': NOW - 700, 'lane': 'opencode', 'cwd': '/tmp/brief'},
                {'at': NOW - 200, 'lane': 'opencode', 'cwd': '/tmp/missing'},
                {'at': NOW - 100, 'lane': 'vercel', 'cwd': '/tmp/other'},
                {'at': NOW + 1, 'lane': 'opencode', 'cwd': '/tmp/future'}]
        self.launches.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.run_log('one', [lane_use(NOW - 600, cost=1.25)])
        got = self.state()
        self.assertAlmostEqual(got['credits_usd'], 22.89)
        self.assertIn('1 run', got['note'])

    def test_copied_messages_do_not_double_charge(self):
        row = dict(lane_use(NOW - 600, cost=1.25), id='unique-message')
        self.run_log('one', [row])
        self.run_log('copy', [row])
        self.assertAlmostEqual(self.state()['credits_usd'], 23.39)

    def test_invalid_cost_is_unknown_not_a_credit(self):
        for cost in (-1, float('nan'), float('inf')):
            with self.subTest(cost=cost):
                self.run_log('one', [lane_use(NOW - 600, cost=cost)])
                self.assertAlmostEqual(self.state()['credits_usd'], 24.14)

    def test_anchor_below_or_at_floor_skips(self):
        for cost in (0.64, 0.65, 100):
            with self.subTest(cost=cost):
                self.anchor(usd=2.64)
                self.run_log('one', [lane_use(NOW - 600, cost=cost)])
                got = self.state()
                self.assertEqual(got['state'], 'OFF')
                self.assertEqual(pick({'opencode': got})['plan'], 'vercel')
                self.assertTrue(got['floor_reached'])

    def test_live_credit_replaces_anchor_and_resets_deductions(self):
        self.run_log('one', [lane_use(NOW - 600, cost=5)])
        got = self.state({'credits_usd': 10, 'credits_at': NOW})
        self.assertEqual(got['credits_usd'], 10)
        anchor = json.loads(self.credits.read_text())['opencode']
        self.assertEqual((anchor['usd'], anchor['at']), (10, NOW))
        self.assertIn('live', anchor['source'])
        self.assertEqual(self.state()['credits_usd'], 10)
        self.run_log('two', [lane_use(NOW + 30, cost=1.25)])
        self.assertAlmostEqual(self.state(now=NOW + 60)['credits_usd'], 8.75)

    def test_live_zero_replaces_anchor_even_on_included_allowance(self):
        got = self.state({'windows': {'monthly': 10}, 'credits_usd': 0, 'credits_at': NOW})
        self.assertEqual(got['state'], 'ON')
        self.assertEqual(json.loads(self.credits.read_text())['opencode']['usd'], 0)
        self.assertEqual(self.state()['state'], 'OFF')

    def test_missing_browser_and_usage_read_can_use_exhausted_history(self):
        got = self.state(unavailable=True)
        self.assertEqual(pick({'opencode': got})['plan'], 'opencode')
        self.assertIn('anchored', got['note'])

    def test_seven_day_anchor_expires_and_future_or_invalid_anchor_skips(self):
        for age, expected in ((7 * 86400, 'ON'), (7 * 86400 + 1, 'OFF'), (-60, 'OFF')):
            with self.subTest(age=age):
                self.anchor(at=NOW - age)
                self.assertEqual(self.state()['state'], expected)
        for usd in (-1, float('nan'), float('inf')):
            self.anchor(usd=usd)
            self.assertEqual(self.state()['state'], 'OFF')

    def test_live_balance_is_saved_when_quota_reader_fails(self):
        script = self.root / 'reader.py'
        script.write_text('import json, sys\n'
                          "if '--credits' not in sys.argv: sys.exit(1)\n"
                          f"print(json.dumps({{'at': {NOW}, 'credits_usd': 9}}))\n")
        cfg = dict(pp.load_config(), opencode_reader_command=[sys.executable, str(script)])
        got = pp.opencode_pool(NOW, cfg, history([{'at': NOW - 60, 'windows': month_full_windows(),
                                                  'resets_at': {'monthly': NOW + 86400}}]))
        self.assertEqual(got['credits_usd'], 9)
        self.assertEqual(got['state'], 'ON')
        self.assertEqual(json.loads(self.credits.read_text())['opencode']['usd'], 9)

    def test_background_refresh_preserves_anchor_written_during_request(self):
        def reader(get, key):
            self.anchor(usd=19)
            return 7
        pp.refresh_credits(['vercel'], NOW, path=str(self.credits), readers={'vercel': reader})
        data = json.loads(self.credits.read_text())
        self.assertEqual(data['opencode']['usd'], 19)
        self.assertEqual(data['vercel']['usd'], 7)

    def test_anchor_cannot_authorize_unknown_month_or_expired_renewal(self):
        for live in ({'windows': {'weekly': 0}}, {'resets_at': {'monthly': NOW - 1}}):
            with self.subTest(live=live):
                self.assertEqual(self.state(live)['state'], 'OFF')

    def test_set_credit_cli_stores_utc_source_and_preserves_other_lanes(self):
        home = self.root / 'home'
        path = home / '.local/state/token-ladder/lane-credits.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'vercel': {'usd': 5, 'at': NOW}}))
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin',
               'TOKEN_LADDER_LOCAL_CONFIG': ISOLATED_LOCAL_CONFIG}
        command = [sys.executable, str(Path(REPO, 'bin/plan-pick')), '--set-credit', 'opencode', '24.64']
        before = datetime.now(timezone.utc).timestamp()
        out = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(path.read_text())
        self.assertEqual(data['vercel'], {'usd': 5, 'at': NOW})
        self.assertGreaterEqual(data['opencode']['at'], before)
        self.assertLessEqual(data['opencode']['at'], datetime.now(timezone.utc).timestamp())
        self.assertTrue(data['opencode']['source'])
        out = subprocess.run(command + ['--credit-at', '2026-11-22T22:00Z',
                                        '--credit-source', 'Valentin dashboard screenshot'],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(path.read_text())['opencode'],
                         {'usd': 24.64, 'at': datetime(2026, 11, 22, 22, tzinfo=timezone.utc).timestamp(),
                          'source': 'Valentin dashboard screenshot'})
        for value in ('nan', 'inf', '-1', 'garbage'):
            out = subprocess.run(command[:-1] + [value], capture_output=True, text=True, env=env)
            self.assertEqual(out.returncode, 2, out.stderr)


class OpencodePoolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.object(pp, 'CREDITS', str(Path(tmp.name, 'lane-credits.json')))
        patch.start()
        self.addCleanup(patch.stop)

    def state(self, rows, cfg=None, now=NOW):
        windows = [row for row in rows if row.get("windows")]
        live = max(windows, key=lambda row: row["at"]) if windows else None
        with mock.patch.object(pp, "read_opencode", return_value=live):
            return pp.opencode_pool(now, pp.load_config() if cfg is None else cfg, history(rows))

    def test_above_go_plus_allowance_never_runs_paid_extra_usage(self):
        row = json.loads(Path(REPO, "tests/fixtures/opencode-go-plus-exhausted.json").read_text())
        got = self.state([row], cfg=pp.load_config())
        self.assertEqual(got["state"], "OFF")
        self.assertIn("$225.00 of $200.00", got["note"])
        ran = []
        with tempfile.TemporaryDirectory() as tmp:
            code = pp.run_lanes(LANES, {"opencode": got}, {}, 1.1,
                                runner=lambda command: (ran.append(command) or 0, "done"),
                                limits_path=str(Path(tmp, "limits.json")), now=NOW,
                                launches_path=str(Path(tmp, "launches.jsonl")), say=lambda text: None,
                                burn={"lane": "opencode", "until": NOW + 3600})
        self.assertEqual(code, 0)
        self.assertEqual(ran, ["run vercel"])

    def test_missing_file_refuses_paid_extra_usage(self):
        got = pp.opencode_pool(NOW, pp.load_config(), tempfile.mkdtemp())
        self.assertEqual(got["state"], "OFF")
        self.assertIn("never top up", got["note"])

    def test_full_month_never_runs_on_history_credit(self):
        for credit in (0, 5.9, 24.64, 1000, None):
            with self.subTest(credit=credit):
                got = self.state([{"at": NOW - 600, "windows": month_full_windows(), "credits_usd": credit}])
                self.assertEqual(got["state"], "OFF")
                self.assertIn("used up", got["note"])
                self.assertIn("monthly 100%, weekly 0%, 5-hour 0%", got["note"])
                lanes = {"vercel": T + 60, "deepseek": T + 60, "openrouter": T + 60}
                self.assertEqual(pick({"opencode": got, "codex": pool()}, lanes)["plan"], "codex")

    def test_missing_allowance_config_refuses_paid_extra_usage(self):
        self.assertEqual(self.state([{"at": NOW, "windows": {"monthly": 1}}], cfg={})["state"], "OFF")

    def test_missing_monthly_usage_refuses_paid_extra_usage(self):
        got = self.state([{"at": NOW - 60, "windows": {"weekly": 0, "5-hour": 0}, "credits_usd": 50}])
        self.assertEqual(got["state"], "OFF")

    def test_stale_or_future_reading_refuses_paid_extra_usage(self):
        for at in (NOW - 13 * 3600, NOW + 3600):
            with self.subTest(at=at):
                got = self.state([{"at": at, "windows": {"monthly": 20}}])
                self.assertEqual(got["state"], "OFF")
                self.assertIn("unavailable or stale", got["note"])

    def test_stale_exhaustion_stays_used_up(self):
        got = self.state([{"at": NOW - 24 * 3600, "windows": month_full_windows()}])
        self.assertEqual(got["state"], "OFF")
        self.assertIn("used up", got["note"])

    def test_reset_reading_can_reopen_lane(self):
        got = self.state([{"at": NOW - 3600, "windows": month_full_windows()},
                          {"at": NOW - 60, "windows": {"monthly": 1, "weekly": 1, "5-hour": 1}}])
        self.assertEqual(got["state"], "ON")
        self.assertIn("$2.00 of $200.00", got["note"])

    def test_credit_only_lines_are_ignored(self):
        rows = [{"at": NOW - 60, "windows": {"monthly": 50}}, {"at": NOW - 60, "windows": {}, "credits_usd": 0.1}]
        self.assertEqual(self.state(rows)["state"], "ON")

    def test_windows_with_room_are_on(self):
        got = self.state([{"at": NOW - 60, "windows": {"rolling": 20, "weekly": 40, "monthly": 60}}])
        self.assertEqual(got["state"], "ON")

    def test_full_short_window_is_off(self):
        for name in ("rolling", "5-hour", "weekly"):
            with self.subTest(name=name):
                got = self.state([{"at": NOW - 600, "windows": {name: 100, "monthly": 10}, "credits_usd": 50}])
                self.assertEqual(got["state"], "OFF")

    def test_garbage_lines_are_skipped(self):
        tmp = tempfile.mkdtemp()
        Path(tmp, "opencode-history.jsonl").write_text("not json\n[]\n" + json.dumps(
            {"at": NOW - 60, "windows": {"monthly": 5}}) + "\n")
        self.assertEqual(pp.opencode_pool(NOW, pp.load_config(), tmp)["state"], "OFF")

    def test_injected_opencode_state_is_never_overwritten(self):
        pools = {"opencode": pool("OFF")}
        self.assertEqual(pp.with_opencode(pools, {}, NOW), pools)
        self.assertIn("opencode", pp.with_opencode({"codex": pool()}, {}, NOW))

class BurnTests(unittest.TestCase):
    def report(self, rows, launches=()):
        path = Path(tempfile.mkdtemp(), "launches.jsonl")
        path.write_text("".join(json.dumps(row) + "\n" for row in launches))
        return pp.burn_report(pp.load_config(), NOW, sessions(rows), str(path))

    def test_costs_are_grouped_by_lane_and_usage_store_caller(self):
        rows = [{"type": "session", "cwd": "/tmp/worker-brief"},
                lane_use(NOW - 60, cost=1.25),
                lane_use(NOW - 50, provider="vercel-ai-gateway", cost=2.5),
                lane_use(NOW - 40, cost=0, tokens=1_000_000)]
        got = self.report(rows)
        self.assertEqual(got["month"], "2027-01")
        self.assertEqual(got["by_lane"]["opencode"]["usd"], 1.25)
        self.assertEqual(got["by_lane"]["vercel"]["usd"], 2.5)
        self.assertEqual(got["by_caller"]["/tmp/worker-brief"]["usd"], 3.75)
        self.assertEqual(got["by_caller"]["/tmp/worker-brief"]["messages"], 3)
        self.assertIsNone(got["by_lane"]["cursor"]["usd"])

    def test_only_current_month_up_to_now_is_counted(self):
        start = datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()
        got = self.report([lane_use(start - 1, cost=50), lane_use(start, cost=2),
                           lane_use(NOW, cost=3), lane_use(NOW + 1, cost=50)])
        self.assertEqual(got["by_lane"]["opencode"]["usd"], 5)

    def test_unknown_costs_are_not_estimated_or_reported_as_zero(self):
        got = self.report([lane_use(NOW - 60, tokens=1_000_000)])
        self.assertEqual(got["by_lane"]["opencode"]["unpriced_messages"], 1)
        self.assertIsNone(got["by_lane"]["opencode"]["usd"])
        self.assertIn("opencode: unknown; 1 messages; 1 unpriced", "\n".join(pp.burn_lines(got)))
        self.assertIn("unknown caller", got["by_caller"])

    def test_copied_message_ids_are_not_double_counted(self):
        row = dict(lane_use(NOW - 60, cost=2), id="message-1")
        got = self.report([row, row])
        self.assertEqual(got["by_lane"]["opencode"]["usd"], 2)
        self.assertEqual(got["by_lane"]["opencode"]["messages"], 1)

    def test_launches_use_recorded_chat_name_or_legacy_cwd(self):
        got = self.report([], [{"at": NOW - 60, "lane": "codex", "caller": "My chat", "cwd": "/tmp/brief"},
                               {"at": NOW - 50, "lane": "codex", "cwd": "/tmp/old-brief"},
                               {"at": NOW + 1, "lane": "codex", "caller": "future"},
                               {"at": NOW, "lane": None, "caller": "no worker"}])
        self.assertEqual(got["by_lane"]["codex"]["launches"], 2)
        self.assertIsNone(got["by_lane"]["codex"]["usd"])
        self.assertEqual(got["by_caller"]["My chat"]["launches"], 1)
        self.assertEqual(got["by_caller"]["/tmp/old-brief"]["launches"], 1)
        self.assertNotIn("future", got["by_caller"])
        self.assertNotIn("no worker", got["by_caller"])

    def test_missing_stores_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = pp.burn_report(pp.load_config(), NOW, tmp, str(Path(tmp, "missing.jsonl")))
        self.assertTrue(all(item["usd"] is None for item in got["by_lane"].values()))
        self.assertEqual(got["by_caller"], {})


class ConfigTests(unittest.TestCase):
    def test_shipped_lanes_follow_valentins_order_after_cursor_ends(self):
        cfg = pp.load_config()
        lanes = pp.lanes_from(cfg, tl.iso_to_ts("2026-11-25T00:00:00Z"))
        self.assertEqual([lane["lane"] for lane in lanes],
                         ["opencode", "antigravity", "vercel", "vercel-zai", "commandcode", "openrouter", "codex", "cursor"])
        self.assertEqual(cfg["pace_warn_ratio"], pp.DEFAULT_PACE_WARN)

    def test_cursor_first_until_its_plan_ends(self):
        cfg = pp.load_config()
        before = pp.lanes_from(cfg, tl.iso_to_ts("2026-11-24T12:00:00Z"))
        self.assertEqual(before[0]["lane"], "cursor")
        self.assertEqual(before[0]["first_until"], "2026-11-24T23:59:00Z")

    def test_shipped_commands_obey_the_model_rules(self):
        for lane in pp.lanes_from(pp.load_config()):
            with self.subTest(lane=lane["lane"]):
                command = lane["command"].lower()
                self.assertNotIn("fast", command)
                self.assertNotIn("baseten", command)
                self.assertNotRegex(command, r"deepseek\S*pro")
                self.assertNotIn("claude", command)
                self.assertIn(lane.get("vendor"), ("deepseek", "openai", "cursor", "google", "zai"))
                self.assertIn(lane.get("credit"), (None, *pp.BALANCES))
                self.assertTrue(command.endswith("</dev/null"), command)
                self.assertIsNone(tl.bash_refusal(lane["command"], {}), lane["command"])

    def test_pi_gateway_lanes_pin_the_provider_lanes_json_names(self):
        lanes = {lane["lane"]: lane for lane in pp.lanes_from(pp.load_config())}
        shipped = json.loads((Path(REPO) / "ci" / "lanes.json").read_text(encoding="utf-8"))
        gateway = next(lane for lane in shipped if lane["name"] == "vercel-ai-gateway")
        openrouter = next(lane for lane in shipped if lane["name"] == "openrouter")
        self.assertEqual(gateway["gateway"], {"only": ["token-pass"]})
        self.assertEqual(openrouter["provider"], {"only": ["StreamLake"], "allow_fallbacks": False})
        self.assertTrue((Path(REPO) / "tools" / "pi-lane-pins.mjs").is_file())
        for name in ("vercel", "openrouter"):
            with self.subTest(lane=name):
                self.assertIn("-e ~/projects/token-ladder/tools/pi-lane-pins.mjs", lanes[name]["command"])


class PlansTests(unittest.TestCase):
    def setUp(self):
        self.plans = pp.plans_from(pp.load_config())

    def test_every_plan_lists_models_places_and_cost(self):
        for plan in self.plans:
            with self.subTest(plan=plan["name"]):
                for key in ("included_models", "runs_on", "extra_cost", "source"):
                    self.assertTrue(plan[key], key)
                self.assertTrue(set(plan["runs_on"]) <= {"local", "cloud"}, plan["runs_on"])

    def test_cursor_is_composer_and_grok_local_and_cloud_with_no_extra_cost(self):
        cursor = next(plan for plan in self.plans if plan["vendor"] == "cursor")
        models = " ".join(cursor["included_models"]).lower()
        self.assertIn("composer", models)
        self.assertIn("grok", models)
        self.assertIn("cloud", cursor["runs_on"])
        self.assertEqual(cursor["extra_cost"], "none")
        self.assertEqual(cursor["source"], "Valentin, 2026-11-20")

    def test_every_lane_vendor_has_a_plan(self):
        vendors = {vendor for plan in self.plans for vendor in (plan["vendor"], *plan.get("model_vendors", []))}
        for lane in pp.lanes_from(pp.load_config()):
            with self.subTest(lane=lane["lane"]):
                self.assertIn(lane["vendor"], vendors)

    def test_missing_or_garbage_plans_give_an_empty_list(self):
        for cfg in ({}, {"plans": None}, {"plans": [{"vendor": "cursor"}]}, {"plans": ["Cursor"]}):
            with self.subTest(cfg=cfg):
                self.assertEqual(pp.plans_from(cfg), [])


class ChatOnlyPoolTests(unittest.TestCase):
    def test_a_chat_only_reading_is_never_picked_without_the_opt_in(self):
        lane = {"lane": "renamed", "pool": "google", "command": "echo forbidden"}
        pools = {"google": {"state": "ON", "used_pct": 40.0, "pace_ratio": 0.1, "chat_only": True}}
        got = pp.pick([lane], pools, now=T)
        self.assertEqual(got["plan"], "none")
        self.assertIsNone(got["command"])
        self.assertIn("chat-only", got["reason"])

    def test_the_opt_in_lane_shares_the_plan_and_runs(self):
        lane = {"lane": "antigravity", "pool": "google", "shared_with_chats": True, "command": "echo allowed"}
        pools = {"google": {"state": "ON", "used_pct": 40.0, "pace_ratio": None,
                            "chat_only": False, "shared_with_chats": True}}
        self.assertEqual(pp.pick([lane], pools, now=T)["plan"], "antigravity")
        runner = mock.Mock(return_value=(0, "done", 4.0, True))
        with tempfile.TemporaryDirectory() as home:
            code = pp.run_lanes([lane], pools, {}, 1.1, runner=runner,
                                limits_path=os.path.join(home, "limits.json"), now=T, say=lambda text: None,
                                launches_path=os.path.join(home, "launches.jsonl"))
        self.assertEqual(code, 0)
        runner.assert_called_once_with("echo allowed")

    def test_a_chat_only_reading_keeps_run_lanes_off_that_pool(self):
        lane = {"lane": "antigravity", "pool": "google", "command": "echo forbidden"}
        runner = mock.Mock(side_effect=AssertionError("no chat-only worker"))
        with tempfile.TemporaryDirectory() as home:
            result = pp.run_lanes([lane], {"google": {"state": "ON", "chat_only": True}}, {}, 1.1, runner=runner,
                                  limits_path=os.path.join(home, "limits.json"), now=T, say=lambda text: None,
                                  launches_path=os.path.join(home, "launches.jsonl"))
        self.assertEqual(result, 75)
        runner.assert_not_called()

    def test_the_shipped_antigravity_lane_is_the_google_plan_lane(self):
        cfg = pp.load_config()
        google = next(plan for plan in pp.plans_from(cfg) if plan["vendor"] == "google")
        self.assertEqual(google["pool"], "google")
        self.assertTrue(google["shared_with_chats"])
        self.assertNotIn("chat_only", google)
        lane = next(lane for lane in pp.lanes_from(cfg) if lane["pool"] == "google")
        self.assertEqual(lane["lane"], "antigravity")
        self.assertTrue(lane["shared_with_chats"])


class WorkerEnvTests(unittest.TestCase):
    def setUp(self):
        self.shims = str(Path(REPO).resolve() / "bin" / "worker-shims")

    def test_env_drops_the_board_login_and_keeps_the_rest(self):
        env = pp.worker_env({"HOME": "/tmp/worker-home", "PATH": "/usr/bin:/bin",
                             "HT_TOKEN": "secret", "HYPERTASK_FOO": "bar",
                             "MULTIPROMPT_TAB": "chat-1", "MULTIPROMPT_OTHER": "x"})
        self.assertNotIn("HT_TOKEN", env)
        self.assertNotIn("HYPERTASK_FOO", env)
        self.assertNotIn("MULTIPROMPT_TAB", env)
        self.assertNotIn("MULTIPROMPT_OTHER", env)
        self.assertEqual(env["HOME"], "/tmp/worker-home")
        self.assertEqual(env["PLAN_PICK_WORKER"], "1")
        self.assertEqual(env["PATH"], self.shims + os.pathsep + "/usr/bin:/bin")

    def test_shims_answer_before_the_real_board_cli(self):
        for name in ("ht", "hypertask"):
            self.assertTrue(os.access(os.path.join(self.shims, name), os.X_OK), name)

    def test_real_run_command_gets_no_board(self):
        command = "ht ticket view SOFT-2857; echo rc=$?; hypertask board 2060; echo rc2=$?"
        env = {"HT_TOKEN": "secret", "HYPERTASK_BOARD": "2060",
               "PATH": os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"])}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(out):
            code, tail = pp.run_command(command)
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn("rc=3", printed)
        self.assertIn("rc2=3", printed)
        self.assertIn("no board login", printed)
        self.assertNotIn("secret", printed)
        self.assertIn("no board login", tail)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.pacing = Path(self.home) / ".local/state/pacing"
        self.pacing.mkdir(parents=True)
        (self.pacing / "opencode-history.jsonl").write_text(
            json.dumps({"at": datetime.now(timezone.utc).timestamp(),
                        "windows": {"5-hour": 0.0, "weekly": 0.0, "monthly": 0.0}}) + "\n")
        reader = Path(self.home, "reader.py")
        reader.write_text("import json, os, sys\n"
                          "from datetime import datetime, timezone\n"
                          "from pathlib import Path\n"
                          "row = json.loads((Path(os.environ['PLAN_PICK_PACING_DIR']) / 'opencode-history.jsonl').read_text().splitlines()[-1])\n"
                          "if '--credits' in sys.argv:\n"
                          " print(json.dumps({'at': row['at'], 'credits_usd': row.get('credits_usd')}))\n"
                          "else:\n"
                          " print(json.dumps({'measured_at': datetime.fromtimestamp(row['at'], timezone.utc).isoformat(), 'windows': [{'name': k, 'used': v} for k, v in row['windows'].items()]}))\n")
        self.local_config = str(Path(self.home, "local.json"))
        Path(self.local_config).write_text(json.dumps({"opencode_reader_command": [sys.executable, str(reader)]}))
        lanes = [dict(lane) for lane in pp.load_config()["lanes"]]
        for lane in lanes:
            if lane["lane"] == "cursor":
                lane["first_until"] = "2026-01-01T00:00:00Z"
        self.ordered_config = str(Path(self.home, "ordered.json"))
        Path(self.ordered_config).write_text(json.dumps({"opencode_reader_command": [sys.executable, str(reader)],
                                                         "lanes": lanes}))

    def cli(self, *args, config=None):
        return subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), *args],
                              capture_output=True, text=True,
                              env={"HOME": self.home, "PATH": "/usr/bin:/bin",
                                   "PLAN_PICK_PACING_DIR": str(self.pacing),
                                   "TOKEN_LADDER_LOCAL_CONFIG": config or self.ordered_config})

    def run_cli(self, pools, *args, config=None):
        path = Path(self.home) / "pools.json"
        path.write_text(json.dumps(pools))
        return self.cli("--pools", str(path), *args, config=config)

    def plan_not_from(self, vendor, earlier=(), *args):
        home = tempfile.mkdtemp()
        pools = Path(home) / "pools.json"
        pools.write_text("{}")
        env = {"HOME": home, "PATH": "/usr/bin:/bin",
               "PLAN_PICK_PACING_DIR": str(self.pacing),
               "TOKEN_LADDER_LOCAL_CONFIG": self.ordered_config}
        cmd = [sys.executable, str(Path(REPO) / "bin" / "plan-pick"), "--pools", str(pools)]
        for lane in earlier:
            subprocess.run(cmd + ["--limited", lane], capture_output=True, text=True, env=env)
        return subprocess.run(cmd + ["--not-vendor", vendor, *args], capture_output=True, text=True, env=env)

    def test_json_output_has_three_fields(self):
        out = self.run_cli({"codex": pool()}, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(sorted(got), ["command", "plan", "reason", "rung", "rung_reason", "rung_source"])
        self.assertEqual(got["plan"], "opencode")

    def test_limited_after_a_429_moves_the_pick_to_the_next_lane(self):
        self.assertEqual(json.loads(self.run_cli({}, "--json").stdout)["plan"], "opencode")
        out = self.cli("--limited", "vercel")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("Lane vercel is skipped until"), out.stdout)
        self.cli("--limited", "opencode")
        got = json.loads(self.run_cli({}, "--json").stdout)
        self.assertEqual(got["plan"], "antigravity")
        self.assertIn("opencode (limited until", got["reason"])
        self.cli("--limited", "antigravity")
        self.assertEqual(json.loads(self.run_cli({}, "--json").stdout)["plan"], "openrouter")

    def test_run_skips_a_400_lane_end_to_end(self):
        cfg = {"lanes": [{"lane": "a", "pool": None, "command": "echo '400 model request was rejected'; exit 1"},
                         {"lane": "b", "pool": None, "command": "echo worker-done"}]}
        repo = Path(tempfile.mkdtemp())
        (repo / "bin").mkdir()
        (repo / "bin" / "plan-pick").write_text((Path(REPO) / "bin" / "plan-pick").read_text())
        (repo / "config.json").write_text(json.dumps(cfg))
        pools = Path(self.home) / "pools.json"
        pools.write_text("{}")
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin",
               "PLAN_PICK_PACING_DIR": str(self.pacing),
               "TOKEN_LADDER_LOCAL_CONFIG": self.local_config}
        out = subprocess.run([sys.executable, str(repo / "bin" / "plan-pick"), "--pools", str(pools), "--run"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("worker-done", out.stdout)
        self.assertIn("skipped lane a: model request was rejected", out.stderr)

    def test_run_skips_a_429_lane_end_to_end(self):
        cfg = {"lanes": [{"lane": "a", "pool": None, "command": "echo '429: Too Many Requests'; exit 1"},
                         {"lane": "b", "pool": None, "command": "echo worker-done"}]}
        repo = Path(tempfile.mkdtemp())
        (repo / "bin").mkdir()
        (repo / "bin" / "plan-pick").write_text((Path(REPO) / "bin" / "plan-pick").read_text())
        (repo / "config.json").write_text(json.dumps(cfg))
        pools = Path(self.home) / "pools.json"
        pools.write_text("{}")
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin",
               "PLAN_PICK_PACING_DIR": str(self.pacing),
               "TOKEN_LADDER_LOCAL_CONFIG": self.local_config}
        out = subprocess.run([sys.executable, str(repo / "bin" / "plan-pick"), "--pools", str(pools), "--run"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("worker-done", out.stdout)
        self.assertIn("lane a hit a rate or usage limit", out.stderr)
        again = subprocess.run([sys.executable, str(repo / "bin" / "plan-pick"), "--pools", str(pools), "--json"],
                               capture_output=True, text=True, env=env)
        got = json.loads(again.stdout)
        self.assertEqual(got["plan"], "b")
        self.assertIn("a (limited until", got["reason"])

    def test_run_with_pool_file_still_skips_exhausted_opencode_history(self):
        fixture = Path(REPO, "tests/fixtures/opencode-go-plus-exhausted.json").read_text()
        (self.pacing / "opencode-history.jsonl").write_text(fixture)
        cfg = pp.load_config()
        cfg["lanes"] = [{"lane": "opencode", "pool": "opencode", "command": "echo paid-worker"},
                        {"lane": "codex", "pool": "codex", "command": "echo included-worker"}]
        repo = Path(tempfile.mkdtemp())
        (repo / "bin").mkdir()
        (repo / "bin/plan-pick").write_text(Path(REPO, "bin/plan-pick").read_text())
        (repo / "config.json").write_text(json.dumps(cfg))
        pools = Path(self.home, "pools.json")
        pools.write_text("{}")
        out = subprocess.run([sys.executable, str(repo / "bin/plan-pick"), "--pools", str(pools), "--run"],
                             capture_output=True, text=True,
                             env={"HOME": self.home, "PATH": "/usr/bin:/bin",
                                  "PLAN_PICK_PACING_DIR": str(self.pacing),
                                  "TOKEN_LADDER_LOCAL_CONFIG": self.local_config})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "included-worker\n")
        self.assertIn("used up", out.stderr)

    def test_not_vendor_skips_the_authors_vendor(self):
        got = json.loads(self.run_cli({"codex": pool()}, "--json", "--not-vendor", "deepseek").stdout)
        self.assertEqual(got["plan"], "antigravity")
        line = self.run_cli({"codex": pool()}, "--not-vendor", "deepseek").stdout
        self.assertTrue(line.startswith("Worker plan: antigravity. Launch: ~/.local/bin/plan-pick --run --not-vendor deepseek ("), line)

    def test_not_vendor_excludes_google_for_a_google_author(self):
        self.cli("--limited", "opencode")
        got = json.loads(self.run_cli({"codex": pool()}, "--json", "--not-vendor", "Google").stdout)
        self.assertEqual(got["plan"], "vercel")

    def test_not_vendor_excludes_each_lane_by_its_own_vendor(self):
        cases = (("opencode", (), "antigravity"),
                 ("google", (), "opencode"),
                 ("vercel", ("opencode",), "antigravity"),
                 ("antigravity", ("opencode",), "vercel"),
                 ("deepseek", ("opencode", "antigravity"), "codex"),
                 ("codex", ("opencode", "antigravity", "vercel", "commandcode", "openrouter"), "cursor"))
        for vendor, earlier, expected in cases:
            with self.subTest(vendor=vendor):
                out = self.plan_not_from(vendor, earlier)
                self.assertEqual(out.returncode, 0, out.stderr)
                start = f"Worker plan: {expected}." if expected else "Worker plan: none."
                self.assertTrue(out.stdout.startswith(start), out.stdout)

    def test_not_vendor_also_applies_to_json(self):
        out = self.plan_not_from("vercel", ("opencode", "antigravity"), "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["plan"], "openrouter")

    def test_not_vendor_accumulates_repeats_and_comma_lists(self):
        cfg = {"lanes": [{"lane": name, "vendor": name, "command": f"echo {name}-ran"}
                         for name in ("alpha", "beta", "gamma")]}
        repo = Path(tempfile.mkdtemp())
        (repo / "bin").mkdir()
        (repo / "bin" / "plan-pick").write_text((Path(REPO) / "bin" / "plan-pick").read_text())
        (repo / "config.json").write_text(json.dumps(cfg))
        pools = Path(self.home) / "pools.json"
        pools.write_text("{}")
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin",
               "PLAN_PICK_PACING_DIR": str(self.pacing),
               "TOKEN_LADDER_LOCAL_CONFIG": self.local_config}
        base = [sys.executable, str(repo / "bin" / "plan-pick"), "--pools", str(pools)]
        for flags in (("--not-vendor", "alpha", "--not-vendor", "beta"),
                      ("--not-vendor", "alpha,beta"),
                      ("--not-vendor", "alpha, beta")):
            with self.subTest(flags=flags):
                out = subprocess.run(base + ["--json", *flags], capture_output=True, text=True, env=env)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(json.loads(out.stdout)["plan"], "gamma")
                text = subprocess.run(base + list(flags), capture_output=True, text=True, env=env)
                self.assertIn("Worker plan: gamma.", text.stdout)
                self.assertIn("--not-vendor alpha --not-vendor beta", text.stdout)
        ran = subprocess.run(base + ["--run", "--not-vendor", "alpha", "--not-vendor", "beta"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn("gamma-ran", ran.stdout)
        self.assertNotIn("beta-ran", ran.stdout)

    def test_run_skips_a_lane_excluded_by_its_own_name(self):
        cfg = {"lanes": [{"lane": "vercel", "credit": "vercel", "vendor": "deepseek",
                          "command": "echo vercel-ran"},
                         {"lane": "openrouter", "vendor": "deepseek", "command": "echo worker-done"}]}
        repo = Path(tempfile.mkdtemp())
        (repo / "bin").mkdir()
        (repo / "bin" / "plan-pick").write_text((Path(REPO) / "bin" / "plan-pick").read_text())
        (repo / "config.json").write_text(json.dumps(cfg))
        pools = Path(self.home) / "pools.json"
        pools.write_text("{}")
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin",
               "PLAN_PICK_PACING_DIR": str(self.pacing),
               "TOKEN_LADDER_LOCAL_CONFIG": self.local_config}
        out = subprocess.run([sys.executable, str(repo / "bin" / "plan-pick"), "--pools", str(pools),
                              "--run", "--not-vendor", "vercel"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("worker-done", out.stdout)
        self.assertNotIn("vercel-ran", out.stdout)

    def test_plans_prints_each_plan_and_the_no_extra_cost_rule(self):
        out = self.cli("--plans")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Cursor (cursor): Composer 2.5, Grok 4.7.", out.stdout)
        self.assertIn("Included means no extra cost: never quote API pricing or ask for a spend cap for these.",
                      out.stdout)

    def test_set_credit_and_usage_logs_drive_selection_without_browser_credit(self):
        now = datetime.now(timezone.utc).timestamp()
        cfg = json.loads(Path(self.ordered_config).read_text())
        cfg['opencode_monthly_renewal'] = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat()
        Path(self.ordered_config).write_text(json.dumps(cfg))
        (self.pacing / 'opencode-history.jsonl').write_text(json.dumps(
            {'at': now, 'windows': month_full_windows()}))
        stamp = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat()
        out = self.cli('--set-credit', 'opencode', '24.64', '--credit-at', stamp)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(self.run_cli({}, '--json').stdout)
        self.assertEqual(got['plan'], 'opencode')
        self.assertIn('anchored credit $24.64', got['reason'])
        path = Path(self.home, '.pi/agent/sessions/worker/run.jsonl')
        path.parent.mkdir(parents=True)
        for cost, plan, balance in ((1.5, 'opencode', '23.14'), (22.65, 'antigravity', '1.99')):
            path.write_text(json.dumps(lane_use(now - 60, cost=cost)) + '\n')
            got = json.loads(self.run_cli({}, '--json').stdout)
            self.assertEqual(got['plan'], plan)
            plans = json.loads(self.cli('--plans', '--json').stdout)
            opencode = next(item for item in plans if item['vendor'] == 'opencode')
            self.assertIn('anchored credit $' + balance, opencode['note'])

    def test_plans_json_includes_allowance_and_usage_status(self):
        out = self.cli("--plans", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        plan = next(plan for plan in json.loads(out.stdout) if plan["vendor"] == "opencode")
        self.assertEqual((plan["name"], plan["monthly_price_usd"], plan["included_allowance_usd"]),
                         ("OpenCode Go Plus", 40, 200))
        self.assertIn(plan["status"], ("ON", "OFF", "used up"))

    def test_plans_shows_go_plus_used_up_without_exposing_credit(self):
        fixture = Path(REPO, "tests/fixtures/opencode-go-plus-exhausted.json").read_text()
        (self.pacing / "opencode-history.jsonl").write_text(fixture)
        out = self.cli("--plans")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("OpenCode Go Plus", out.stdout)
        self.assertIn("$40/month; $200 included allowance", out.stdout)
        self.assertIn("Status: included allowance used up; extra usage OFF", out.stdout)
        self.assertIn("monthly 112.5%, weekly 0%, 5-hour 0%", out.stdout)
        self.assertNotIn("24.64", out.stdout)

    def test_burn_cli_json_and_text_use_usage_store_cwd(self):
        path = Path(self.home, ".pi/agent/sessions/worker/run.jsonl")
        path.parent.mkdir(parents=True)
        now = datetime.now(timezone.utc).timestamp()
        rows = [{"type": "session", "cwd": "/tmp/my-brief"}, lane_use(now, cost=1.5)]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        out = self.cli("--burn", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got["by_lane"]["opencode"]["usd"], 1.5)
        self.assertEqual(got["by_caller"]["/tmp/my-brief"]["usd"], 1.5)
        out = self.cli("--burn")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("opencode: $1.5000", out.stdout)
        self.assertIn("/tmp/my-brief: $1.5000", out.stdout)

    def test_plans_json_includes_config_and_google_usage(self):
        out = self.cli("--plans", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        plans = json.loads(out.stdout)
        google = next(plan for plan in plans if plan["vendor"] == "google")
        self.assertEqual(google["usage"]["state"], "unknown")
        self.assertTrue(google["usage"]["shared_with_chats"])
        self.assertIsNone(google["usage"]["limit"])
        for plan in plans:
            plan.pop("usage", None)
            plan.pop("status", None)
            plan.pop("note", None)
            plan.pop("windows", None)
            plan.pop("credits_usd", None)
            plan.pop("resets_at", None)
        self.assertEqual(plans, pp.plans_from(pp.load_config()))

    def test_plans_prints_google_count_without_claiming_a_known_quota(self):
        usage = {"state": "unknown", "used": 3, "limit": None, "pace_ratio": None,
                 "unit": "local history entries today (UTC)", "shared_with_chats": True}
        out = self.run_cli({"google": usage}, "--plans")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Pool: google. Shared with chats; the antigravity lane burns the full quota.", out.stdout)
        self.assertIn("Used: 3 local history entries today (UTC). Limit: unknown. Pace: unknown.", out.stdout)
        self.assertNotIn("Used up", out.stdout)
        plans = json.loads(self.run_cli({"google": usage}, "--plans", "--json").stdout)
        self.assertEqual(next(plan for plan in plans if plan.get("pool") == "google")["usage"], usage)

    def test_plans_names_google_used_up_and_the_time_seen(self):
        usage = {"state": "OFF", "used": 3, "limit": None, "pace_ratio": None,
                 "unit": "local history entries today (UTC)", "shared_with_chats": True,
                 "used_up": True, "used_up_at": "2026-11-22T09:05:00Z"}
        out = self.run_cli({"google": usage}, "--plans")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Used: 3 local history entries today (UTC). Limit: unknown. Pace: unknown. "
                      "Used up at 2026-11-22T09:05:00Z.", out.stdout)
        plans = json.loads(self.run_cli({"google": usage}, "--plans", "--json").stdout)
        self.assertTrue(next(plan for plan in plans if plan.get("pool") == "google")["usage"]["used_up"])

    def test_plans_survives_unavailable_google_reading(self):
        out = self.cli("--plans", "--pools", "/nonexistent")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Pool: google. Shared with chats; the antigravity lane burns the full quota. "
                      "Used: unknown. Limit: unknown. Pace: unknown.", out.stdout)

    def test_limited_unknown_lane_fails(self):
        out = self.cli("--limited", "baseten")
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown lane baseten", out.stderr)

    def test_text_line_names_plan_and_launch(self):
        out = self.run_cli({"codex": pool("ON", 2.3)})
        self.assertTrue(out.stdout.startswith("Worker plan: opencode. Launch: ~/.local/bin/plan-pick --run (reads brief.txt, runs ~/.npm-global/bin/pi -p"), out.stdout)

    def test_none_line_has_no_launch(self):
        for lane in ("vercel", "deepseek", "openrouter", "antigravity"):
            self.cli("--limited", lane)
        out = self.run_cli({p: pool("OFF") for p in ("opencode", "codex", "grok", "google")})
        self.assertTrue(out.stdout.startswith("Worker plan: none. Why:"), out.stdout)

    def test_claude_helper_under_pace_exits_zero(self):
        out = self.run_cli({"claude": pool("ON", 0.5)}, "--claude-helper")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("Claude helper: allowed. Why:"), out.stdout)

    def test_claude_helper_over_pace_exits_three(self):
        out = self.run_cli({"claude": pool("ON", 1.8)}, "--claude-helper")
        self.assertEqual(out.returncode, 3)
        self.assertTrue(out.stdout.startswith("Claude helper: refused. Why:"), out.stdout)

    def test_claude_helper_json(self):
        out = self.run_cli({"claude": pool("ON", 1.8)}, "--claude-helper", "--json")
        self.assertEqual(json.loads(out.stdout)["allowed"], False)
        self.assertEqual(sorted(json.loads(out.stdout)), ["allowed", "reason"])

    def emergency_until(self, hours):
        return datetime.fromtimestamp(time.time() + hours * 3600, timezone.utc).isoformat()

    def test_claude_helper_emergency_allows_above_pace(self):
        until = self.emergency_until(2)
        self.assertEqual(self.cli("--claude-emergency-until", until, "--reason", "workers blocked").returncode, 0)
        out = self.run_cli({"claude": pool("ON", 1.8)}, "--claude-helper")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, f"Claude helper: allowed. Why: emergency allowance until {until} "
                                     "(workers blocked); pace 1.80x.\n")

    def test_claude_helper_emergency_keeps_used_up_refusal(self):
        self.cli("--claude-emergency-until", self.emergency_until(2), "--reason", "x")
        out = self.run_cli({"claude": pool("OFF", 1.8)}, "--claude-helper")
        self.assertEqual(out.returncode, 3)

    def test_claude_helper_expired_emergency_refuses_and_is_removed(self):
        state = Path(self.home) / ".local" / "state" / "token-ladder" / "claude-emergency.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"until": self.emergency_until(-1), "reason": "old", "set_at": "x"}))
        out = self.run_cli({"claude": pool("ON", 1.8)}, "--claude-helper")
        self.assertEqual(out.returncode, 3)
        self.assertIn("pace 1.80x is above 1.1x", out.stdout)
        self.assertFalse(state.exists())

    def test_claude_emergency_off_removes_it(self):
        self.cli("--claude-emergency-until", self.emergency_until(2), "--reason", "x")
        self.assertEqual(self.cli("--claude-emergency-until", "off").returncode, 0)
        out = self.run_cli({"claude": pool("ON", 1.8)}, "--claude-helper")
        self.assertEqual(out.returncode, 3)

    def test_claude_emergency_needs_reason_and_zone(self):
        self.assertEqual(self.cli("--claude-emergency-until", self.emergency_until(2)).returncode, 2)
        self.assertEqual(self.cli("--claude-emergency-until", "2030-01-01T00:00:00", "--reason", "x").returncode, 2)

    def test_unreadable_pools_fail_loud(self):
        out = subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), "--pools", "/nonexistent"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("cannot read pool state", out.stderr)

    def test_live_reader_runs_without_a_pools_file(self):
        out = subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), "--json"],
                             capture_output=True, text=True,
                             env={"HOME": tempfile.mkdtemp(), "PATH": "/usr/bin:/bin",
                                  "TOKEN_LADDER_LOCAL_CONFIG": self.local_config})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(json.loads(out.stdout)["plan"],
                      ("vercel", "deepseek", "opencode", "antigravity", "codex", "cursor", "openrouter", "none"))


class LoopIntervalTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), *args],
                              capture_output=True, text=True,
                              env={"HOME": self.home, "PATH": "/usr/bin:/bin",
                                   "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG})

    def run_cli(self, pools, *args):
        path = Path(self.home) / "pools.json"
        path.write_text(json.dumps(pools))
        return self.cli("--pools", str(path), "--loop-interval", *args)

    def test_on_pace_waits_the_two_hour_base(self):
        out = self.run_cli({"claude": pool("ON", 0.6)})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "7200 claude pace 0.60x on pace\n")

    def test_ahead_of_pace_waits_three_hours(self):
        out = self.run_cli({"claude": pool("ON", 1.16)})
        self.assertEqual(out.stdout, "10800 claude pace 1.16x ahead\n")

    def test_far_ahead_of_pace_waits_four_hours(self):
        out = self.run_cli({"claude": pool("ON", 1.34)})
        self.assertEqual(out.stdout, "14400 claude pace 1.34x far ahead\n")

    def test_used_95_percent_waits_four_hours(self):
        out = self.run_cli({"claude": {"state": "WARN", "used_pct": 95.0, "pace_ratio": 0.5}})
        self.assertEqual(out.stdout, "14400 claude 95% used\n")

    def test_missing_claude_pool_uses_the_base(self):
        out = self.run_cli({"codex": pool()})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "7200 claude pool unknown\n")

    def test_unreadable_pool_file_still_exits_zero(self):
        out = self.cli("--pools", "/nonexistent", "--loop-interval")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "7200 claude pool unknown\n")

    def test_json_has_the_four_fields(self):
        got = json.loads(self.run_cli({"claude": pool("ON", 1.16)}, "--json").stdout)
        self.assertEqual(sorted(got), ["pace_ratio", "reason", "seconds", "used_pct"])
        self.assertEqual(got["seconds"], 10800)
        self.assertEqual(got["pace_ratio"], 1.16)
        self.assertEqual(got["used_pct"], 40.0)
        self.assertEqual(got["reason"], "claude pace 1.16x ahead")

    def test_json_for_an_unknown_pool_keeps_null_numbers(self):
        got = json.loads(self.cli("--pools", "/nonexistent", "--loop-interval", "--json").stdout)
        self.assertEqual(got, {"seconds": 7200, "pace_ratio": None, "used_pct": None,
                               "reason": "claude pool unknown"})

    def test_config_holds_the_thresholds(self):
        self.assertEqual(pp.loop_rule(pp.load_config()),
                         {"base_seconds": 7200, "ahead_seconds": 10800, "far_ahead_seconds": 14400,
                          "ahead_ratio": 1.0, "far_ahead_ratio": 1.2, "used_pct": 95, "min_seconds": 1800})

    def test_config_changes_are_the_only_source(self):
        cfg = {"loop_interval": {"ahead_ratio": 0.5}}
        self.assertEqual(pp.loop_interval({"claude": pool("ON", 0.6)}, cfg)["seconds"], 10800)

    def test_never_below_the_minimum(self):
        cfg = {"loop_interval": {"far_ahead_seconds": 60, "min_seconds": 1800}}
        self.assertEqual(pp.loop_interval({"claude": pool("ON", 5.0)}, cfg)["seconds"], 1800)


class LogLaunchTests(unittest.TestCase):
    def test_log_launch_prints_pacer_line_and_writes_record(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "sub", "launches.jsonl")
            buf = io.StringIO()
            with redirect_stderr(buf):
                pp.log_launch(path, "vercel", 1795127211.9, None)
            stamp = datetime.fromtimestamp(1795127211.9, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.assertEqual(buf.getvalue().strip(), f"Pacer launch: vercel {stamp}")
            with open(path) as fh:
                record = json.loads(fh.readline())
            self.assertEqual(record["lane"], "vercel")
            self.assertEqual(record["at"], 1795127211.9)

    def test_log_launch_lane_none_prints_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "launches.jsonl")
            buf = io.StringIO()
            with redirect_stderr(buf):
                pp.log_launch(path, None, 1795127211.9, None)
            self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
