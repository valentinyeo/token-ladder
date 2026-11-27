import builtins
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from _load import tl


NOW = tl.iso_to_ts("2026-11-22T12:00:00Z")
FIXTURE = Path(__file__).parent / "fixtures" / "agy-history.jsonl"
REFRESH = "I1122 09:00:00.000000      47 quota_manager.go:45] doRefreshQuota: starting reload (force=false)"
RECOVERED = "I1122 09:20:00.000000      47 quota_manager.go:61] doRefreshQuota: refresh complete, quota available"
EXHAUSTED = ("E1122 09:05:00.123456      47 quota_manager.go:120] doRefreshQuota: refresh failed: "
             "rpc error: code = ResourceExhausted desc = quota exceeded for gemini-3.8-flash")
LENGTH_429 = ("I1125 11:38:01.554922 6876 llm_monitor.go:636] LLM Monitor sir_flash_lite: "
              "Trajectory formatted, length: 429")
USAGE_NOW = tl.iso_to_ts("2026-11-25T11:00:00Z")
USAGE = "\n".join([
    "Gemini Models\tWeekly Limit Remaining\t61%\t2026-11-30T20:03:27Z",
    "Gemini Models\tFive Hour Limit Remaining\t84%\t2026-11-25T15:01:27Z",
    "Claude and GPT models\tWeekly Limit Remaining\t100%\t2026-12-02T10:58:19Z",
    "Claude and GPT models\tFive Hour Limit Remaining\t100%\t2026-11-25T15:58:19Z",
])


class GooglePoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.history = self.home / ".gemini/antigravity-cli/history.jsonl"
        self.log = self.home / ".gemini/antigravity-cli/cli.log"
        self.history.parent.mkdir(parents=True)
        self.home_patch = patch.object(tl, "HOME", str(self.home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        keep_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self.restore_tz, keep_tz)

    def restore_tz(self, keep_tz):
        if keep_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = keep_tz
        time.tzset()

    def write_log(self, *lines):
        self.log.write_text("\n".join(lines) + "\n")

    def fake_agy(self, text="", returncode=0):
        binary = self.home / ".local/bin/agy"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        cache = self.home / ".local/state/token-ladder/agy-usage.json"
        if cache.exists():
            cache.unlink()
        done = subprocess.CompletedProcess([], returncode, stdout=text, stderr="")
        patcher = patch.object(tl.subprocess, "run", return_value=done)
        return patcher.start(), patcher

    def usage(self, text=USAGE, returncode=0, now=USAGE_NOW):
        mocked, patcher = self.fake_agy(text, returncode)
        self.addCleanup(patcher.stop)
        return tl.read_google(now), mocked

    def test_counts_only_today_through_now_and_keeps_quota_unknown(self):
        self.history.write_text(FIXTURE.read_text())
        got = tl.read_google(NOW)
        self.assertEqual(got["used"], 3)
        self.assertEqual(got["unit"], "local history entries today (UTC)")
        self.assertTrue(got["chat_only"])
        self.assertFalse(got["shared_with_chats"])
        self.assertTrue(got["estimated"])
        self.assertEqual(got["state"], "unknown")
        for key in ("limit", "used_pct", "pace_ratio", "resets_at", "hours_left"):
            self.assertIsNone(got[key], key)
        self.assertNotIn("display", json.dumps(got))
        self.assertNotIn("/fixture", json.dumps(got))
        self.assertNotIn("used_up", got)

    def test_utc_day_rolls_over_without_inventing_a_quota_reset(self):
        self.history.write_text(FIXTURE.read_text())
        got = tl.read_google(tl.iso_to_ts("2026-11-23T00:00:00Z"))
        self.assertEqual(got["used"], 0)
        self.assertIsNone(got["resets_at"])

    def test_missing_empty_or_unusable_history_is_unknown(self):
        self.assertIsNone(tl.read_google(NOW)["used"])
        for content in ("", "{broken\n[]\n", '{"timestamp":true}\n', '{"timestamp":1795348801000}\n'):
            with self.subTest(content=content):
                self.history.write_text(content)
                got = tl.read_google(NOW)
                self.assertEqual(got["state"], "unknown")
                self.assertIsNone(got["used"])

    def test_unreadable_or_invalid_encoding_is_unknown(self):
        with patch("builtins.open", side_effect=PermissionError):
            self.assertIsNone(tl.read_google(NOW)["used"])
        self.history.write_bytes(b"\xff")
        self.assertIsNone(tl.read_google(NOW)["used"])

    def test_only_opens_history_and_log_read_only_without_network_or_cli(self):
        self.history.write_text(FIXTURE.read_text())
        self.write_log(REFRESH, EXHAUSTED)
        token = self.history.parent / "antigravity-oauth-token"
        token.write_text("sentinel-token-value")
        before = self.history.read_bytes()
        with patch("builtins.open", wraps=builtins.open) as opened, \
                patch.object(tl.subprocess, "run", side_effect=AssertionError("no CLI")), \
                patch.object(tl.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            got = tl.read_google(NOW)
        self.assertEqual(got["used"], 3)
        opened_paths = [call.args[0] for call in opened.call_args_list]
        self.assertIn(str(self.history), opened_paths)
        self.assertIn(str(self.log), opened_paths)
        self.assertNotIn(str(token), opened_paths)
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(token.read_text(), "sentinel-token-value")
        self.assertNotIn("sentinel", json.dumps(got))
        self.assertEqual(sorted(path.name for path in self.history.parent.iterdir()),
                         ["antigravity-oauth-token", "cli.log", "history.jsonl"])

    def test_current_pools_includes_read_only_google_metadata(self):
        self.history.write_text(FIXTURE.read_text())
        got = tl.current_pools({}, NOW)["google"]
        self.assertEqual(got["used"], 3)
        self.assertFalse(got["shared_with_chats"])
        self.assertEqual(got["state"], "unknown")

    def test_exhausted_quota_error_marks_google_used_up_with_the_time_seen(self):
        self.history.write_text(FIXTURE.read_text())
        self.write_log(REFRESH, EXHAUSTED)
        self.assertEqual(tl.read_google(NOW), {
            "used_pct": None, "hours_left": None, "resets_at": None, "state": "OFF",
            "pace_ratio": None, "chat_only": True, "shared_with_chats": False, "used": 3, "limit": None,
            "unit": "local history entries today (UTC)", "estimated": True,
            "source": "agy local history timestamps; Antigravity quota unavailable",
            "used_up_at": "2026-11-22T09:05:00Z", "used_up": True})

    def test_recovered_after_exhaustion_is_not_used_up(self):
        self.history.write_text(FIXTURE.read_text())
        self.write_log(EXHAUSTED, RECOVERED)
        got = tl.read_google(NOW)
        self.assertEqual(got["state"], "unknown")
        self.assertNotIn("used_up", got)
        self.assertEqual(got["used"], 3)

    def test_quota_manager_recovery_without_function_name_clears_exhaustion(self):
        self.write_log(EXHAUSTED,
                       "I1122 09:20:00.000000  47 quota_manager.go:61] quota refresh complete")
        self.assertNotIn("used_up", tl.read_google(NOW))

    def test_newest_timestamp_wins_over_file_order(self):
        self.write_log(RECOVERED, EXHAUSTED)
        self.assertNotIn("used_up", tl.read_google(NOW))
        self.write_log(EXHAUSTED.replace("09:05", "10:05"), RECOVERED)
        self.assertEqual(tl.read_google(NOW)["used_up_at"], "2026-11-22T10:05:00Z")

    def test_future_or_invalid_quota_lines_do_not_clear_exhaustion(self):
        for hour in ("13", "25"):
            with self.subTest(hour=hour):
                self.write_log(EXHAUSTED, RECOVERED.replace("09:20", f"{hour}:20"))
                self.assertEqual(tl.read_google(NOW)["used_up_at"], "2026-11-22T09:05:00Z")

    def test_no_errors_keeps_todays_behaviour(self):
        self.history.write_text(FIXTURE.read_text())
        self.write_log(REFRESH, "I1122 09:30:00.000000      47 quota_manager.go:41] doRefreshQuota: skipped (throttled)")
        got = tl.read_google(NOW)
        self.assertEqual(got["state"], "unknown")
        self.assertNotIn("used_up", got)

    def test_yesterdays_exhaustion_is_ignored(self):
        self.history.write_text(FIXTURE.read_text())
        self.write_log("E1121 23:58:00.123456      47 quota_manager.go:120] doRefreshQuota: failed: RESOURCE_EXHAUSTED")
        self.assertNotIn("used_up", tl.read_google(NOW))

    def test_rate_limit_and_quota_wording_counts_as_used_up(self):
        for line in ("E1122 10:00:00.000000  47 quota_manager.go:120] doRefreshQuota: failed: 429 Too Many Requests",
                     "E1122 10:00:00.000000  47 quota_manager.go:120] doRefreshQuota: failed: rate limit exceeded",
                     "E1122 10:00:00.000000  47 quota_manager.go:120] doRefreshQuota: failed: quota exceeded for project",
                     "E1122 10:00:00.000000  47 quota_manager.go:120] doRefreshQuota: failed: RESOURCE_EXHAUSTED"):
            with self.subTest(line=line):
                self.write_log(line)
                got = tl.read_google(NOW)
                self.assertTrue(got["used_up"])
                self.assertEqual(got["used_up_at"], "2026-11-22T10:00:00Z")

    def test_log_times_are_local_and_shown_in_utc(self):
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()
        self.write_log(EXHAUSTED)
        got = tl.read_google(NOW)
        self.assertTrue(got["used_up"])
        self.assertEqual(got["used_up_at"], "2026-11-22T08:05:00Z")

    def test_missing_or_empty_log_keeps_todays_behaviour(self):
        self.write_log()
        self.assertNotIn("used_up", tl.read_google(NOW))
        self.log.unlink()
        self.assertNotIn("used_up", tl.read_google(NOW))

    def test_used_up_is_reported_even_when_history_is_missing(self):
        self.write_log(EXHAUSTED)
        got = tl.read_google(NOW)
        self.assertIsNone(got["used"])
        self.assertTrue(got["used_up"])
        self.assertEqual(got["state"], "OFF")

    def test_pool_line_names_the_used_up_time(self):
        got = {"used_pct": None, "resets_at": None, "state": "OFF",
               "used_up": True, "used_up_at": "2026-11-22T09:05:00Z"}
        self.assertEqual(tl.pool_line("google", got),
                         "Google: ? used, reset unknown, OFF (used up at 2026-11-22T09:05:00Z)")

    def test_agy_usage_reports_the_real_quota_and_both_groups(self):
        got, mocked = self.usage()
        self.assertEqual(got["used_pct"], 39)
        self.assertEqual(got["five_hour_used_pct"], 16)
        self.assertFalse(got["estimated"])
        self.assertEqual(got["source"], "agy /usage")
        self.assertEqual(got["state"], "ON")
        self.assertNotIn("used_up", got)
        self.assertEqual(got["resets_at"], tl.iso_to_ts("2026-11-30T20:03:27Z"))
        self.assertEqual(got["five_hour_resets_at"], tl.iso_to_ts("2026-11-25T15:01:27Z"))
        self.assertIsNotNone(got["pace_ratio"])
        self.assertEqual(got["gemini"], {"weekly_remaining_pct": 61,
                                         "weekly_resets_at": tl.iso_to_ts("2026-11-30T20:03:27Z"),
                                         "five_hour_remaining_pct": 84,
                                         "five_hour_resets_at": tl.iso_to_ts("2026-11-25T15:01:27Z")})
        self.assertEqual(got["claude_gpt"], {"weekly_remaining_pct": 100,
                                             "weekly_resets_at": tl.iso_to_ts("2026-12-02T10:58:19Z"),
                                             "five_hour_remaining_pct": 100,
                                             "five_hour_resets_at": tl.iso_to_ts("2026-11-25T15:58:19Z")})
        argv = mocked.call_args.args[0]
        self.assertTrue(argv[0].endswith("/.local/bin/agy"))
        self.assertEqual(argv[1:], ["-p", "/usage", "--print-timeout", "45s"])
        self.assertEqual(mocked.call_args.kwargs["timeout"], 60)
        self.assertIn("cwd", mocked.call_args.kwargs)
        json.dumps(got, allow_nan=False)

    def test_pools_json_publishes_both_groups_and_the_estimate_flag(self):
        got, _ = self.usage()
        pools = tl.current_pools({}, USAGE_NOW)
        self.assertTrue(pools["google"]["chat_only"])
        self.assertFalse(pools["google"]["shared_with_chats"])
        self.assertEqual(pools["google"]["gemini"]["weekly_remaining_pct"], 61)
        self.assertEqual(pools["google"]["claude_gpt"]["five_hour_remaining_pct"], 100)
        self.assertFalse(pools["google"]["estimated"])
        self.assertEqual(json.loads(json.dumps(pools))["google"]["used_pct"], 39)

    def test_agy_zero_weekly_remaining_marks_the_pool_used_up_with_that_reset(self):
        zero = USAGE.replace("Weekly Limit Remaining\t61%", "Weekly Limit Remaining\t0%")
        got, _ = self.usage(zero)
        self.assertTrue(got["used_up"])
        self.assertEqual(got["used_up_window"], "week")
        self.assertEqual(got["used_up_resets_at"], tl.iso_to_ts("2026-11-30T20:03:27Z"))
        self.assertEqual(got["used_up_at"], "2026-11-25T11:00:00Z")
        self.assertEqual(got["used_pct"], 100)
        self.assertEqual(got["state"], "OFF")

    def test_agy_zero_five_hour_remaining_marks_the_pool_used_up_with_its_own_reset(self):
        zero = USAGE.replace("Five Hour Limit Remaining\t84%", "Five Hour Limit Remaining\t0%")
        got, _ = self.usage(zero)
        self.assertTrue(got["used_up"])
        self.assertEqual(got["used_up_window"], "five_hour")
        self.assertEqual(got["used_up_resets_at"], tl.iso_to_ts("2026-11-25T15:01:27Z"))
        self.assertEqual(got["five_hour_used_pct"], 100)
        self.assertEqual(got["state"], "OFF")

    def test_agy_failure_keeps_the_quota_unknown_and_never_used_up(self):
        self.write_log(LENGTH_429)
        got, _ = self.usage("", returncode=1)
        self.assertEqual(got["state"], "unknown")
        self.assertNotIn("used_up", got)
        self.assertTrue(got["estimated"])
        self.assertIsNone(got["used_pct"])
        self.assertNotIn("gemini", got)

    def test_agy_garbage_output_is_unknown(self):
        got, _ = self.usage("Approaching limit...\nno quota rows here\n")
        self.assertEqual(got["state"], "unknown")
        self.assertNotIn("used_up", got)
        self.assertTrue(got["estimated"])

    def test_agy_reading_wins_over_a_quota_error_in_the_log(self):
        self.write_log("E1125 10:00:00.123456      47 quota_manager.go:120] doRefreshQuota: failed: RESOURCE_EXHAUSTED")
        got, _ = self.usage(now=tl.iso_to_ts("2026-11-25T12:00:00Z"))
        self.assertNotIn("used_up", got)
        self.assertEqual(got["used_pct"], 39)

    def test_second_read_reuses_the_cached_reading(self):
        got, mocked = self.usage()
        again = tl.read_google(USAGE_NOW + 60)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(again["used_pct"], got["used_pct"])
        self.assertFalse(again["estimated"])

    def test_a_stale_cache_is_replaced_by_a_fresh_reading(self):
        _, mocked = self.usage()
        dropped = USAGE.replace("Weekly Limit Remaining\t61%", "Weekly Limit Remaining\t41%")
        mocked.return_value = subprocess.CompletedProcess([], 0, stdout=dropped, stderr="")
        fresh = tl.read_google(USAGE_NOW + tl.AGY_TTL + 1)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(fresh["used_pct"], 59)

    def test_todays_trajectory_length_is_not_a_quota_error(self):
        self.write_log(LENGTH_429)
        got = tl.read_google(tl.iso_to_ts("2026-11-25T12:00:00Z"))
        self.assertEqual(got["state"], "unknown")
        self.assertNotIn("used_up", got)

    def test_a_bare_number_never_marks_the_pool_used_up(self):
        for line in ("I1125 10:00:00.000000      47 llm_monitor.go:636] LLM Monitor: length: 429",
                     "I1125 10:00:00.000000      47 quota_manager.go:120] doRefreshQuota: retry in 429 ms",
                     "I1125 10:00:00.000000      47 quota_manager.go:120] doRefreshQuota: model 429 unavailable"):
            with self.subTest(line=line):
                self.write_log(line)
                self.assertNotIn("used_up", tl.read_google(tl.iso_to_ts("2026-11-25T12:00:00Z")))

    def test_a_qualified_429_still_counts_as_a_quota_error(self):
        for line in ("status 429", "HTTP 429", "code: 429", "code = 429", "429 Too Many Requests"):
            with self.subTest(line=line):
                self.write_log(f"E1125 10:00:00.000000      47 quota_manager.go:120] doRefreshQuota: failed: {line}")
                got = tl.read_google(tl.iso_to_ts("2026-11-25T12:00:00Z"))
                self.assertTrue(got["used_up"])
                self.assertEqual(got["used_up_at"], "2026-11-25T10:00:00Z")

    def test_pool_line_marks_an_estimated_percentage(self):
        got = {"used_pct": 39.0, "resets_at": None, "state": "unknown", "estimated": True}
        self.assertEqual(tl.pool_line("google", got),
                         "Google: 39% (estimated) used, reset unknown, unknown (assume 70% used for planning)")

    def test_pool_line_names_both_agy_groups_with_the_source(self):
        got, _ = self.usage()
        self.assertIn("(agy /usage; Gemini 61% weekly left, 84% 5h left; "
                      "Claude and GPT 100% weekly left, 100% 5h left)", tl.pool_line("google", got))
