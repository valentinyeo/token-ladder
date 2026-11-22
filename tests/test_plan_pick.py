import io
import json
import os
import subprocess
import sys
import tempfile
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

    def test_only_paced_lanes_left_picks_least_ahead(self):
        limits = {lane["lane"]: T + 60 for lane in LANES if lane["lane"] not in ("codex", "cursor")}
        got = pick({"codex": pool("ON", 2.0), "grok": pool("ON", 1.5)}, limits)
        self.assertEqual(got["plan"], "cursor")
        self.assertIn("least ahead is cursor (pace 1.50x is above 1.1x)", got["reason"])

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

    def test_no_lane_left_logs_a_null_lane(self):
        limits = {lane["lane"]: T + 60 for lane in LANES}
        Path(self.limits).write_text(json.dumps(limits))
        self.assertEqual(self.go({}), 4)
        self.assertEqual([e["lane"] for e in self.logged()], [None])

    def test_no_lane_left_says_when_the_next_lane_frees(self):
        limits = {lane["lane"]: T + 60 * (i + 1) for i, lane in enumerate(LANES)}
        Path(self.limits).write_text(json.dumps(limits))
        self.assertEqual(self.go({}), 4)
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
        self.assertEqual(pp.read_limits(self.limits), {"opencode": T + 3600})
        self.assertTrue(any("opencode hit a rate or usage limit" in line for line in self.said), self.said)

    def test_empty_credit_is_skipped_longer(self):
        Path(self.limits).write_text(json.dumps({"opencode": T + 3600, "openrouter": T + 3600}))
        self.go({"run vercel": (1, '402: {"error":{"message":"Insufficient Balance"}}'), "run deepseek": (0, "ok")})
        self.assertEqual(pp.read_limits(self.limits)["vercel"], T + 12 * 3600)

    def test_clean_exit_with_no_output_is_done_not_retried(self):
        self.assertEqual(self.go({"run opencode": (0, "  \n")}), 0)
        self.assertEqual(self.ran, ["run opencode"])
        self.assertEqual(pp.read_limits(self.limits), {})

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
        self.assertEqual(pp.read_limits(self.limits), {})
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
        self.assertEqual(self.go(outcomes), 4)
        self.assertEqual(len(self.ran), len(LANES))
        self.assertIn("no lane left, nothing ran", self.said[-1])

    def test_hours_come_from_config(self):
        self.go({"run opencode": (1, "429"), "run vercel": (0, "ok")}, cfg={"limited_hours": 3})
        self.assertEqual(pp.read_limits(self.limits)["opencode"], T + 3 * 3600)

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


class LimitTests(unittest.TestCase):
    def test_mark_limited_writes_and_drops_expired(self):
        path = str(Path(tempfile.mkdtemp()) / "sub" / "limits.json")
        Path(path).parent.mkdir()
        Path(path).write_text(json.dumps({"old": T - 10, "codex": T + 99}))
        until = pp.mark_limited("vercel", 2, T, path)
        self.assertEqual(until, T + 7200)
        self.assertEqual(pp.read_limits(path), {"codex": T + 99, "vercel": T + 7200})

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


REAL_AT = 1795174461.7206776
REAL_USD = 20.57

def month_full_windows():
    return {"5-hour": 50.0, "weekly": 50.0, "monthly": 100.0}


REAL_HISTORY = [{"at": REAL_AT, "windows": month_full_windows(),
                 "credits_usd": REAL_USD, "today_usd": 2.36, "source": "credits"},
                {"at": REAL_AT + 1800, "windows": month_full_windows(),
                 "credits_usd": None, "today_usd": None, "source": "api"},
                {"at": REAL_AT + 3600, "windows": month_full_windows(),
                 "credits_usd": None, "today_usd": None, "source": "api"}]


def api_at(at):
    return {"at": at, "windows": month_full_windows(),
            "credits_usd": None, "today_usd": None, "source": "api"}


class OpencodePoolTests(unittest.TestCase):
    def state(self, rows, cfg=None, used=None, now=NOW):
        return pp.opencode_pool(now, cfg or {}, history(rows), sessions(used or []))

    def test_missing_file_is_unknown_and_still_usable(self):
        got = pp.opencode_pool(NOW, {}, tempfile.mkdtemp())
        self.assertEqual(got["state"], "unknown")

    def test_full_month_runs_on_fresh_credit_above_the_floor(self):
        rows = [{"at": NOW - 600, "windows": month_full_windows()}, {"at": NOW - 300, "windows": {}, "credits_usd": 23.12}]
        got = self.state(rows)
        self.assertEqual(got["state"], "ON")
        self.assertIn("$23.12", got["note"])

    def test_full_month_with_credit_under_the_floor_is_off(self):
        rows = [{"at": NOW - 600, "windows": month_full_windows()}, {"at": NOW - 300, "windows": {}, "credits_usd": 5.9}]
        got = self.state(rows)
        self.assertEqual(got["state"], "OFF")
        lanes = {"vercel": T + 60, "deepseek": T + 60, "openrouter": T + 60}
        self.assertEqual(pick({"opencode": got, "codex": pool()}, lanes)["plan"], "codex")

    def test_full_month_with_no_credit_reading_at_all_is_off(self):
        self.assertEqual(self.state([{"at": NOW - 600, "windows": month_full_windows()}])["state"], "OFF")

    def test_pc_off_estimates_from_the_api_history_and_the_lane_spend(self):
        now = REAL_AT + 3 * 3600
        got = self.state(REAL_HISTORY + [api_at(now - 60)], used=[lane_use(REAL_AT + 600, tokens=1_000_000)], now=now)
        self.assertEqual(got["state"], "ON")
        self.assertIn("$18.57 estimated credit", got["note"])
        self.assertIn("$20.57 read 3.0h ago less $2.00 spend seen since", got["note"])
        lanes = {"vercel": T + 60, "deepseek": T + 60}
        self.assertEqual(pick({"opencode": got, "codex": pool()}, lanes)["plan"], "opencode")

    def test_estimate_under_the_floor_is_off(self):
        now = REAL_AT + 4 * 3600
        got = self.state(REAL_HISTORY + [api_at(now - 60)], used=[lane_use(REAL_AT + 6600, tokens=7_500_000)], now=now)
        self.assertEqual(got["state"], "OFF")
        self.assertIn("estimated credit $5.57", got["note"])
        self.assertIn("under $6", got["note"])
        lanes = {"vercel": T + 60, "deepseek": T + 60, "openrouter": T + 60}
        self.assertEqual(pick({"opencode": got, "codex": pool()}, lanes)["plan"], "codex")

    def test_estimate_older_than_24_hours_is_refused(self):
        for hours in (23.9, 24.1):
            with self.subTest(hours=hours):
                now = REAL_AT + hours * 3600
                got = self.state(REAL_HISTORY + [api_at(now - 60)], now=now)
                self.assertEqual(got["state"], "ON" if hours < 24 else "OFF")
        now = REAL_AT + 25 * 3600
        self.assertIn("no credit reading or estimate from the last 24 hours",
                      self.state(REAL_HISTORY + [api_at(now - 60)], now=now)["note"])

    def test_recorded_cost_wins_over_the_token_rate(self):
        now = REAL_AT + 3 * 3600
        got = self.state(REAL_HISTORY + [api_at(now - 60)],
                         used=[lane_use(REAL_AT + 600, tokens=1_000_000, cost=4.0)], now=now)
        self.assertIn("$16.57 estimated credit", got["note"])
        self.assertIn("less $4.00 spend seen since", got["note"])

    def test_estimate_counts_only_the_opencode_lane(self):
        now = REAL_AT + 3 * 3600
        used = [lane_use(REAL_AT + 600, provider="vercel-ai-gateway", tokens=5_000_000),
                lane_use(REAL_AT + 700, provider="deepseek", tokens=5_000_000),
                lane_use(REAL_AT - 600, tokens=5_000_000)]
        got = self.state(REAL_HISTORY + [api_at(now - 60)], used=used, now=now)
        self.assertIn("$20.57 estimated credit", got["note"])
        self.assertIn("less $0.00 spend seen since", got["note"])

    def test_spend_since_the_reading_comes_from_the_lane_logs_only(self):
        now = REAL_AT + 3 * 3600
        used = [lane_use(REAL_AT + 60, tokens=500_000), lane_use(REAL_AT + 120, tokens=500_000)]
        self.assertAlmostEqual(pp.lane_spend(REAL_AT, sessions(used)), 2.0)

    def test_fresh_real_reading_still_wins_over_the_estimate(self):
        now = REAL_AT + 1800
        got = self.state(REAL_HISTORY + [api_at(now - 60)], used=[lane_use(REAL_AT + 600, tokens=1_000_000)], now=now)
        self.assertEqual(got["state"], "ON")
        self.assertIn("running on $20.57 credit", got["note"])

    def test_credit_only_lines_are_ignored(self):
        rows = [{"at": NOW - 600, "windows": {"monthly": 50}}, {"at": NOW - 60, "windows": {}, "credits_usd": 0.1}]
        self.assertEqual(self.state(rows)["state"], "ON")

    def test_stale_reading_is_unknown(self):
        got = self.state([{"at": NOW - 13 * 3600, "windows": month_full_windows(), "credits_usd": 0.5}])
        self.assertEqual(got["state"], "unknown")

    def test_windows_with_room_are_on(self):
        got = self.state([{"at": NOW - 600, "windows": {"rolling": 20, "weekly": 40, "monthly": 60}}])
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
        self.assertEqual(pp.opencode_pool(NOW, {}, tmp)["state"], "ON")

    def test_injected_opencode_state_is_never_overwritten(self):
        pools = {"opencode": pool("OFF")}
        self.assertEqual(pp.with_opencode(pools, {}, NOW), pools)
        self.assertIn("opencode", pp.with_opencode({"codex": pool()}, {}, NOW))

    def test_monthly_meter_full_with_short_windows_still_on(self):
        rows = [{"at": NOW - 60, "windows": {"5-hour": 0.0, "weekly": 0.0, "monthly": 100.0}}]
        got = self.state(rows)
        self.assertEqual(got["state"], "ON")
        self.assertIn("shorter windows still have room", got["note"])


class ConfigTests(unittest.TestCase):
    def test_shipped_lanes_follow_valentins_order(self):
        cfg = pp.load_config()
        lanes = pp.lanes_from(cfg)
        self.assertEqual([lane["lane"] for lane in lanes],
                         ["opencode", "vercel", "openrouter", "deepseek", "codex", "cursor"])
        self.assertEqual(cfg["pace_warn_ratio"], pp.DEFAULT_PACE_WARN)

    def test_shipped_commands_obey_the_model_rules(self):
        for lane in pp.lanes_from(pp.load_config()):
            with self.subTest(lane=lane["lane"]):
                command = lane["command"].lower()
                self.assertNotIn("fast", command)
                self.assertNotIn("baseten", command)
                self.assertNotRegex(command, r"deepseek\S*pro")
                self.assertNotIn("claude", command)
                self.assertIn(lane.get("vendor"), ("deepseek", "openai", "cursor"))
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
        vendors = {plan["vendor"] for plan in self.plans}
        for lane in pp.lanes_from(pp.load_config()):
            with self.subTest(lane=lane["lane"]):
                self.assertIn(lane["vendor"], vendors)

    def test_missing_or_garbage_plans_give_an_empty_list(self):
        for cfg in ({}, {"plans": None}, {"plans": [{"vendor": "cursor"}]}, {"plans": ["Cursor"]}):
            with self.subTest(cfg=cfg):
                self.assertEqual(pp.plans_from(cfg), [])


class GoogleWorkerExclusionTests(unittest.TestCase):
    def test_google_pool_or_lane_is_never_picked_even_without_a_reading(self):
        for lane in ({"lane": "google", "pool": None, "command": "echo forbidden"},
                     {"lane": "renamed", "pool": "google", "command": "echo forbidden"}):
            for reading in (None, {}, pool("ON", 0.1), pool("OFF"), pool("ON", 2.0)):
                pools = {} if reading is None else {"google": reading}
                with self.subTest(lane=lane, reading=reading):
                    burn = {"lane": lane["lane"], "until": T + 60}
                    got = pp.pick([lane], pools, now=T, burn=burn)
                    self.assertEqual(got["plan"], "none")
                    self.assertIsNone(got["command"])
                    self.assertIn("chat-only", got["reason"])

    def test_worker_fallback_and_least_ahead_selection_exclude_google(self):
        google = {"lane": "google", "pool": "google", "command": "echo forbidden"}
        worker = {"lane": "worker", "pool": "worker", "command": "echo allowed"}
        for ratio in (0.1, 2.0):
            with self.subTest(ratio=ratio):
                got = pp.pick([google, worker], {"google": pool("ON", 0.0), "worker": pool("ON", ratio)}, now=T)
                self.assertEqual(got["plan"], "worker")

    def test_run_never_launches_google(self):
        lane = {"lane": "google", "pool": "google", "command": "echo forbidden"}
        runner = mock.Mock(side_effect=AssertionError("no Google worker"))
        with tempfile.TemporaryDirectory() as home:
            result = pp.run_lanes([lane], {}, {}, 1.1, runner=runner,
                                  limits_path=os.path.join(home, "limits.json"), now=T, say=lambda text: None,
                                  launches_path=os.path.join(home, "launches.jsonl"))
        self.assertEqual(result, 4)
        runner.assert_not_called()

    def test_google_is_a_chat_only_plan_without_a_shipped_lane(self):
        cfg = pp.load_config()
        google = next(plan for plan in pp.plans_from(cfg) if plan["vendor"] == "google")
        self.assertEqual(google["pool"], "google")
        self.assertTrue(google["chat_only"])
        self.assertFalse(any(lane.get("pool") == "google" or lane["lane"] == "google"
                             for lane in pp.lanes_from(cfg)))


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
            json.dumps({"at": T, "windows": {"5-hour": 0.0, "weekly": 0.0, "monthly": 0.0}}) + "\n")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), *args],
                              capture_output=True, text=True,
                              env={"HOME": self.home, "PATH": "/usr/bin:/bin",
                                   "PLAN_PICK_PACING_DIR": str(self.pacing),
                                   "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG})

    def run_cli(self, pools, *args):
        path = Path(self.home) / "pools.json"
        path.write_text(json.dumps(pools))
        return self.cli("--pools", str(path), *args)

    def plan_not_from(self, vendor, earlier=(), *args):
        home = tempfile.mkdtemp()
        pools = Path(home) / "pools.json"
        pools.write_text("{}")
        env = {"HOME": home, "PATH": "/usr/bin:/bin",
               "PLAN_PICK_PACING_DIR": str(self.pacing),
               "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG}
        cmd = [sys.executable, str(Path(REPO) / "bin" / "plan-pick"), "--pools", str(pools)]
        for lane in earlier:
            subprocess.run(cmd + ["--limited", lane], capture_output=True, text=True, env=env)
        return subprocess.run(cmd + ["--not-vendor", vendor, *args], capture_output=True, text=True, env=env)

    def test_json_output_has_three_fields(self):
        out = self.run_cli({"codex": pool()}, "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(sorted(got), ["command", "plan", "reason"])
        self.assertEqual(got["plan"], "opencode")

    def test_limited_after_a_429_moves_the_pick_to_the_next_lane(self):
        self.assertEqual(json.loads(self.run_cli({}, "--json").stdout)["plan"], "opencode")
        out = self.cli("--limited", "vercel")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("Lane vercel is skipped until"), out.stdout)
        self.cli("--limited", "opencode")
        got = json.loads(self.run_cli({}, "--json").stdout)
        self.assertEqual(got["plan"], "openrouter")
        self.assertIn("vercel (limited until", got["reason"])

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
               "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG}
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
               "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG}
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

    def test_not_vendor_skips_the_authors_vendor(self):
        got = json.loads(self.run_cli({"codex": pool()}, "--json", "--not-vendor", "deepseek").stdout)
        self.assertEqual(got["plan"], "codex")
        got = json.loads(self.run_cli({"codex": pool("OFF")}, "--json", "--not-vendor", "DeepSeek").stdout)
        self.assertEqual(got["plan"], "cursor")
        line = self.run_cli({"codex": pool()}, "--not-vendor", "deepseek").stdout
        self.assertTrue(line.startswith("Worker plan: codex. Launch: ~/.local/bin/plan-pick --run --not-vendor deepseek ("), line)

    def test_not_vendor_excludes_each_lane_by_its_own_vendor(self):
        cases = (("opencode", (), "vercel"),
                 ("vercel", ("opencode",), "openrouter"),
                 ("openrouter", ("opencode", "vercel"), "deepseek"),
                 ("deepseek", ("opencode", "vercel", "openrouter"), "codex"),
                 ("codex", ("opencode", "vercel", "openrouter", "deepseek"), "cursor"),
                 ("cursor", ("opencode", "vercel", "openrouter", "deepseek", "codex"), None))
        for vendor, earlier, expected in cases:
            with self.subTest(vendor=vendor):
                out = self.plan_not_from(vendor, earlier)
                self.assertEqual(out.returncode, 0, out.stderr)
                start = f"Worker plan: {expected}." if expected else "Worker plan: none."
                self.assertTrue(out.stdout.startswith(start), out.stdout)

    def test_not_vendor_also_applies_to_json(self):
        out = self.plan_not_from("vercel", ("opencode",), "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["plan"], "openrouter")

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
               "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG}
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

    def test_plans_json_includes_config_and_google_usage(self):
        out = self.cli("--plans", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        plans = json.loads(out.stdout)
        google = next(plan for plan in plans if plan["vendor"] == "google")
        self.assertEqual(google["usage"]["state"], "unknown")
        self.assertTrue(google["usage"]["chat_only"])
        self.assertIsNone(google["usage"]["limit"])
        for plan in plans:
            plan.pop("usage", None)
        self.assertEqual(plans, pp.plans_from(pp.load_config()))

    def test_plans_prints_google_count_without_claiming_a_known_quota(self):
        usage = {"state": "unknown", "used": 3, "limit": None, "pace_ratio": None,
                 "unit": "local history entries today (UTC)", "chat_only": True}
        out = self.run_cli({"google": usage}, "--plans")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Pool: google. Chat-only, never workers.", out.stdout)
        self.assertIn("Used: 3 local history entries today (UTC). Limit: unknown. Pace: unknown.", out.stdout)
        self.assertNotIn("Used up", out.stdout)
        plans = json.loads(self.run_cli({"google": usage}, "--plans", "--json").stdout)
        self.assertEqual(next(plan for plan in plans if plan.get("pool") == "google")["usage"], usage)

    def test_plans_names_google_used_up_and_the_time_seen(self):
        usage = {"state": "OFF", "used": 3, "limit": None, "pace_ratio": None,
                 "unit": "local history entries today (UTC)", "chat_only": True,
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
        self.assertIn("Pool: google. Chat-only, never workers. Used: unknown. Limit: unknown. Pace: unknown.", out.stdout)

    def test_limited_unknown_lane_fails(self):
        out = self.cli("--limited", "baseten")
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown lane baseten", out.stderr)

    def test_text_line_names_plan_and_launch(self):
        out = self.run_cli({"codex": pool("ON", 2.3)})
        self.assertTrue(out.stdout.startswith("Worker plan: opencode. Launch: ~/.local/bin/plan-pick --run (reads brief.txt, runs ~/.npm-global/bin/pi -p"), out.stdout)

    def test_none_line_has_no_launch(self):
        for lane in ("vercel", "deepseek", "openrouter"):
            self.cli("--limited", lane)
        out = self.run_cli({p: pool("OFF") for p in ("opencode", "codex", "grok")})
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

    def test_unreadable_pools_fail_loud(self):
        out = subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), "--pools", "/nonexistent"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("cannot read pool state", out.stderr)

    def test_live_reader_runs_without_a_pools_file(self):
        out = subprocess.run([sys.executable, str(Path(REPO) / "bin" / "plan-pick"), "--json"],
                             capture_output=True, text=True,
                             env={"HOME": tempfile.mkdtemp(), "PATH": "/usr/bin:/bin",
                                  "TOKEN_LADDER_LOCAL_CONFIG": ISOLATED_LOCAL_CONFIG})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(json.loads(out.stdout)["plan"], ("vercel", "deepseek", "opencode", "codex", "cursor", "openrouter", "none"))


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
