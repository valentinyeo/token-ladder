import builtins
import json
import os
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

    def test_counts_only_today_through_now_and_keeps_quota_unknown(self):
        self.history.write_text(FIXTURE.read_text())
        got = tl.read_google(NOW)
        self.assertEqual(got["used"], 3)
        self.assertEqual(got["unit"], "local history entries today (UTC)")
        self.assertFalse(got["chat_only"])
        self.assertTrue(got["shared_with_chats"])
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
        self.assertEqual([call.args[0] for call in opened.call_args_list],
                         [str(self.history), str(self.log)])
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(token.read_text(), "sentinel-token-value")
        self.assertNotIn("sentinel", json.dumps(got))
        self.assertEqual(sorted(path.name for path in self.history.parent.iterdir()),
                         ["antigravity-oauth-token", "cli.log", "history.jsonl"])

    def test_current_pools_includes_read_only_google_metadata(self):
        self.history.write_text(FIXTURE.read_text())
        got = tl.current_pools({}, NOW)["google"]
        self.assertEqual(got["used"], 3)
        self.assertTrue(got["shared_with_chats"])
        self.assertEqual(got["state"], "unknown")

    def test_exhausted_quota_error_marks_google_used_up_with_the_time_seen(self):
        self.history.write_text(FIXTURE.read_text())
        self.write_log(REFRESH, EXHAUSTED)
        self.assertEqual(tl.read_google(NOW), {
            "used_pct": None, "hours_left": None, "resets_at": None, "state": "OFF",
            "pace_ratio": None, "chat_only": False, "shared_with_chats": True, "used": 3, "limit": None,
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
