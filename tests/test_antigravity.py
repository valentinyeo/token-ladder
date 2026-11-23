import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from _load import REPO, load_bin, tl

pp = load_bin("plan-pick")

NOW = tl.iso_to_ts("2026-11-22T12:00:00Z")
AFTER_CURSOR = tl.iso_to_ts("2026-11-25T00:00:00Z")
NEON = "gemini-4-neon-high"
EXHAUSTED = ("E1122 09:05:00.123456      47 quota_manager.go:120] doRefreshQuota: refresh failed: "
             "rpc error: code = ResourceExhausted desc = quota exceeded for gemini-4-neon-high")


def google(used=None, limit=None, state="unknown"):
    return {"state": state, "used_pct": None, "pace_ratio": None, "used": used, "limit": limit,
            "unit": "local history entries today (UTC)", "chat_only": False, "shared_with_chats": True}


def lanes(now=AFTER_CURSOR):
    return pp.lanes_from(pp.load_config(), now)


def lane_in_order(name):
    names = [lane["lane"] for lane in lanes()]
    return names.index(name)


class LaneOrderTests(unittest.TestCase):
    def test_antigravity_sits_after_the_prepaid_lane_and_before_vercel(self):
        self.assertEqual(lane_in_order("antigravity"), lane_in_order("opencode") + 1)
        self.assertEqual(lane_in_order("antigravity"), lane_in_order("vercel") - 1)

    def test_the_lane_is_picked_once_opencode_is_out(self):
        names = [lane["lane"] for lane in lanes()]
        got = pp.pick(lanes(), {lane["lane"]: {"state": "ON", "pace_ratio": 0.2} for lane in lanes()},
                      {"opencode": NOW + 600}, NOW)
        self.assertEqual(got["plan"], "antigravity")
        self.assertTrue(got["command"].endswith("</dev/null"))
        self.assertEqual(names[:2], ["opencode", "antigravity"])


class CursorFirstTests(unittest.TestCase):
    def order(self, now):
        return [lane["lane"] for lane in pp.lanes_from(pp.load_config(), now)]

    def test_cursor_is_first_the_day_before_its_plan_ends(self):
        self.assertEqual(self.order(tl.iso_to_ts("2026-11-24T12:00:00Z")),
                         ["cursor", "opencode", "antigravity", "vercel", "commandcode", "openrouter", "codex"])

    def test_cursor_is_last_the_day_after_its_plan_ends(self):
        self.assertEqual(self.order(AFTER_CURSOR),
                         ["opencode", "antigravity", "vercel", "commandcode", "openrouter", "codex", "cursor"])

    def test_the_end_date_comes_from_the_cursor_lane_config(self):
        cursor = next(lane for lane in self.order_lanes(AFTER_CURSOR) if lane["lane"] == "cursor")
        self.assertEqual(cursor["first_until"], "2026-11-24T23:59:00Z")
        self.assertEqual(pp.lane_first_until(cursor), tl.iso_to_ts("2026-11-24T23:59:00Z"))

    def test_the_pick_follows_the_same_clock(self):
        self.assertEqual(self.pick_at(tl.iso_to_ts("2026-11-24T12:00:00Z")), "cursor")
        self.assertEqual(self.pick_at(AFTER_CURSOR), "opencode")

    def order_lanes(self, now):
        return pp.lanes_from(pp.load_config(), now)

    def pick_at(self, now):
        lanes = self.order_lanes(now)
        pools = {lane.get("pool") or lane["lane"]: {"state": "ON", "pace_ratio": 0.2} for lane in lanes}
        return pp.pick(lanes, pools, {}, now)["plan"]


class CommandShapeTests(unittest.TestCase):
    def setUp(self):
        self.lane = next(lane for lane in lanes() if lane["lane"] == "antigravity")

    def test_pins_the_neon_model_and_reads_the_brief(self):
        command = self.lane["command"]
        self.assertIn("~/.local/bin/agy --print \"$(cat brief.txt)\"", command)
        self.assertIn(f"--model {NEON}", command)
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--output-format json", command)

    def test_timeout_is_a_worker_timeout_and_matches_the_lane_field(self):
        command = self.lane["command"]
        self.assertIn(f"--print-timeout {self.lane['print_timeout']}", command)
        self.assertNotIn("--print-timeout 0", command)

    def test_never_names_a_token_file(self):
        command = self.lane["command"].lower()
        for bad in ("oauth", "token", "credentials", "api-key", "apikey"):
            self.assertNotIn(bad, command)

    def test_the_google_plan_points_at_this_lane(self):
        plan = next(plan for plan in pp.plans_from(pp.load_config()) if plan["vendor"] == "google")
        self.assertEqual(plan["pool"], self.lane["pool"])
        self.assertTrue(plan["shared_with_chats"])


class ReserveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.launches = os.path.join(self.temp.name, "launches.jsonl")

    def write_launches(self, count, lane="antigravity", at=NOW - 60):
        rows = [{"at": at, "lane": lane, "session": "s", "cwd": "/w", "not_vendor": None} for _ in range(count)]
        Path(self.launches).write_text("".join(json.dumps(row) + "\n" for row in rows))

    def pick(self, pool, cfg=None):
        pools = pp.with_antigravity_reserve({"google": pool}, cfg or {}, NOW, self.launches)
        return pp.pick(lanes(), pools, {"opencode": NOW + 600}, NOW)

    def test_the_default_burns_todays_whole_known_limit(self):
        self.assertEqual(self.pick(google(used=7, limit=10))["plan"], "antigravity")
        self.assertEqual(self.pick(google(used=10, limit=10))["plan"], "antigravity")

    def test_a_lowered_share_still_keeps_room_for_chats(self):
        got = self.pick(google(used=7, limit=10), {"antigravity_reserve_share_pct": 60})
        self.assertEqual(got["plan"], "vercel")
        self.assertIn("daily limit", got["reason"])

    def test_a_share_under_the_used_count_keeps_the_lane(self):
        self.assertEqual(self.pick(google(used=7, limit=10), {"antigravity_reserve_share_pct": 80})["plan"],
                         "antigravity")

    def test_there_is_no_default_daily_run_cap(self):
        self.write_launches(50)
        self.assertEqual(self.pick(google(used=50))["plan"], "antigravity")

    def test_a_configured_daily_run_cap_still_applies(self):
        self.write_launches(2)
        self.assertEqual(self.pick(google(used=2), {"antigravity_max_daily_runs": 3})["plan"], "antigravity")
        got = self.pick(google(used=2), {"antigravity_max_daily_runs": 2})
        self.assertEqual(got["plan"], "vercel")
        self.assertIn("2 worker runs today, cap 2", got["reason"])

    def test_other_lanes_and_older_days_do_not_count(self):
        self.write_launches(20, lane="vercel")
        self.write_launches(20, at=NOW - 36 * 3600)
        self.assertEqual(self.pick(google(used=20), {"antigravity_max_daily_runs": 5})["plan"], "antigravity")

    def test_absent_or_null_config_values_keep_burning(self):
        self.write_launches(50)
        self.assertEqual(self.pick(google(used=50), {})["plan"], "antigravity")
        self.assertEqual(self.pick(google(used=50), {"antigravity_max_daily_runs": None})["plan"], "antigravity")
        self.assertEqual(self.pick(google(used=50), {"antigravity_reserve_share_pct": None})["plan"],
                         "antigravity")

    def test_the_shipped_defaults_burn_the_quota(self):
        cfg = pp.load_config()
        self.assertEqual(cfg["antigravity_reserve_share_pct"], 100)
        self.assertNotIn("antigravity_max_daily_runs", cfg)
        self.assertIsNone(pp.DEFAULT_ANTIGRAVITY_MAX_DAILY_RUNS)

    def test_a_used_up_reading_is_left_alone(self):
        pools = {"google": {"state": "OFF", "used": 3, "limit": 10, "used_up": True}}
        self.assertEqual(pp.with_antigravity_reserve(pools, {}, NOW, self.launches), pools)

    def test_no_google_reading_leaves_the_pools_untouched(self):
        self.assertEqual(pp.with_antigravity_reserve({"codex": {"state": "ON"}}, {}, NOW, self.launches),
                         {"codex": {"state": "ON"}})
        self.assertEqual(pp.with_antigravity_reserve({"google": "junk"}, {}, NOW, self.launches),
                         {"google": "junk"})


class QuotaFallthroughTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.log = self.home / ".gemini/antigravity-cli/cli.log"
        self.log.parent.mkdir(parents=True)
        self.limits = os.path.join(self.temp.name, "limits.json")
        self.launches = os.path.join(self.temp.name, "launches.jsonl")
        self.said = []

    def google_reading(self):
        with mock.patch.object(tl, "HOME", str(self.home)):
            return tl.read_google(NOW)

    def test_an_exhausted_agy_log_line_sends_the_pick_to_vercel(self):
        self.log.write_text(EXHAUSTED + "\n")
        got = pp.pick(lanes(), {"google": self.google_reading()}, {"opencode": NOW + 600}, NOW)
        self.assertEqual(got["plan"], "vercel")
        self.assertIn("antigravity (used up", got["reason"])

    def test_a_recovered_log_line_keeps_the_lane(self):
        self.log.write_text(EXHAUSTED + "\n"
                            "I1122 09:20:00.000000      47 quota_manager.go:61] doRefreshQuota: refresh complete\n")
        got = pp.pick(lanes(), {"google": self.google_reading()}, {"opencode": NOW + 600}, NOW)
        self.assertEqual(got["plan"], "antigravity")

    def test_an_early_quota_error_runs_the_next_lane_and_marks_this_one_limited(self):
        tail = '{"status":"ERROR","error":"rpc error: code = ResourceExhausted desc = quota exceeded"}'
        runner = mock.Mock(side_effect=[(1, tail, 4.0, False), (0, "worker-done", 9.0, True)])
        code = pp.run_lanes(lanes(), {"opencode": {"state": "OFF"}}, {}, 1.1, runner=runner,
                            limits_path=self.limits, now=NOW, say=self.said.append, launches_path=self.launches)
        self.assertEqual(code, 0)
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(self.said[0].startswith("lane antigravity:"), self.said[0])
        self.assertIn("lane antigravity hit a rate or usage limit", self.said[1])
        entry = json.loads(Path(self.limits).read_text())["antigravity"]
        self.assertEqual(entry["until"], NOW + 300)
        self.assertEqual(entry["source"], "quota error")
        self.assertIn("ResourceExhausted", entry["error_line"])
        self.assertEqual(json.loads(Path(self.launches).read_text().splitlines()[1])["lane"], "vercel")

    def test_a_used_up_reading_leaves_nothing_to_run(self):
        self.log.write_text(EXHAUSTED + "\n")
        gate = pp.with_antigravity_reserve({"google": self.google_reading()}, {}, NOW, self.launches)
        runner = mock.Mock(side_effect=AssertionError("nothing to run"))
        code = pp.run_lanes([lane for lane in lanes() if lane["pool"] == "google"], gate, {}, 1.1, runner=runner,
                            limits_path=self.limits, now=NOW, say=self.said.append, launches_path=self.launches)
        self.assertEqual(code, 4)
        self.assertIn("no lane left", self.said[-1])
        self.assertIn("antigravity (used up", self.said[-1])
        runner.assert_not_called()


class LaunchCountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, "launches.jsonl")

    def test_counts_only_this_lane_today(self):
        rows = [{"at": NOW - 3600, "lane": "antigravity"},
                {"at": NOW + 3600, "lane": "antigravity"},
                {"at": NOW - 3600, "lane": "vercel"},
                {"at": tl.iso_to_ts("2026-11-21T23:00:00Z"), "lane": "antigravity"},
                {"at": "soon", "lane": "antigravity"},
                {"lane": "antigravity"}]
        Path(self.path).write_text("".join(json.dumps(row) + "\n" for row in rows) + "not json\n")
        self.assertEqual(pp.today_launches(self.path, "antigravity", NOW), 1)
        self.assertEqual(pp.today_launches(os.path.join(self.temp.name, "missing.jsonl"), "antigravity", NOW), 0)

    def test_the_day_starts_at_utc_midnight(self):
        self.assertEqual(datetime.fromtimestamp(pp.midnight(NOW), timezone.utc).isoformat(),
                         "2026-11-22T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
