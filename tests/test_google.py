import builtins
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _load import tl


NOW = tl.iso_to_ts("2026-11-22T12:00:00Z")
FIXTURE = Path(__file__).parent / "fixtures" / "agy-history.jsonl"


class GooglePoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.history = self.home / ".gemini/antigravity-cli/history.jsonl"
        self.history.parent.mkdir(parents=True)
        self.home_patch = patch.object(tl, "HOME", str(self.home))
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_counts_only_today_through_now_and_keeps_quota_unknown(self):
        self.history.write_text(FIXTURE.read_text())
        got = tl.read_google(NOW)
        self.assertEqual(got["used"], 3)
        self.assertEqual(got["unit"], "local history entries today (UTC)")
        self.assertTrue(got["chat_only"])
        self.assertTrue(got["estimated"])
        self.assertEqual(got["state"], "unknown")
        for key in ("limit", "used_pct", "pace_ratio", "resets_at", "hours_left"):
            self.assertIsNone(got[key], key)
        self.assertNotIn("display", json.dumps(got))
        self.assertNotIn("/fixture", json.dumps(got))

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

    def test_only_opens_history_read_only_without_network_or_cli(self):
        self.history.write_text(FIXTURE.read_text())
        before = self.history.read_bytes()
        with patch("builtins.open", wraps=builtins.open) as opened, \
                patch.object(tl.subprocess, "run", side_effect=AssertionError("no CLI")), \
                patch.object(tl.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            got = tl.read_google(NOW)
        self.assertEqual(got["used"], 3)
        opened.assert_called_once_with(str(self.history))
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(list(self.history.parent.iterdir()), [self.history])

    def test_current_pools_includes_read_only_google_metadata(self):
        self.history.write_text(FIXTURE.read_text())
        got = tl.current_pools({}, NOW)["google"]
        self.assertEqual(got["used"], 3)
        self.assertTrue(got["chat_only"])
        self.assertEqual(got["state"], "unknown")
