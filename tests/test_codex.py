import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from _load import tl
NOW = 1_800_000_000
LIMITS = {"limitId": "codex", "planType": "pro", "credits": {"balance": "10"},
          "primary": {"usedPercent": 31, "resetsAt": NOW + 3600, "windowDurationMins": 300},
          "secondary": {"usedPercent": 25, "resetsAt": NOW + 86400, "windowDurationMins": 10080}}


class CodexParserTests(unittest.TestCase):
    def test_single_bucket(self):
        rl = tl.parse_codex_response({"id": 2, "result": {"rateLimits": LIMITS}})
        self.assertEqual(rl["primary"]["used_percent"], 31)
        self.assertEqual(rl["secondary"]["window_minutes"], 10080)
        self.assertEqual(rl["plan_type"], "pro")
        self.assertEqual(rl["credits"]["balance"], "10")

    def test_codex_bucket_wins_over_other_pools_and_legacy(self):
        other = dict(LIMITS, limitId="codex_other")
        rl = tl.parse_codex_response({"result": {"rateLimits": other,
            "rateLimitsByLimitId": {"codex_other": other, "codex": LIMITS}}})
        self.assertEqual(rl["primary"]["used_percent"], 31)

    def test_week_only_primary(self):
        limits = dict(LIMITS, primary=LIMITS["secondary"], secondary=None)
        rl = tl.parse_codex_response({"result": {"rateLimits": limits}})
        with patch.object(tl, "read_codex_logs", return_value=None), patch.object(tl, "probe_codex", return_value=(NOW, rl)):
            result = tl.read_codex(NOW)
        self.assertEqual(result["week"]["used"], 25)
        self.assertIsNone(result["five_hour"]["used"])

    def test_empty_map_uses_single_bucket(self):
        self.assertIn("primary", tl.parse_codex_response({"result": {"rateLimitsByLimitId": {}, "rateLimits": LIMITS}}))

    def test_invalid_replies(self):
        for response in (None, {}, {"error": {"message": "SECRET"}}, {"result": {}}, {"result": [1]},
                         {"result": {"rateLimitsByLimitId": [1]}},
                         {"result": {"rateLimits": dict(LIMITS, limitId="other")}}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                tl.parse_codex_response(response)

    def test_invalid_windows(self):
        for key, value in (("usedPercent", None), ("usedPercent", True), ("usedPercent", float("nan")),
                           ("resetsAt", "tomorrow"), ("windowDurationMins", 0)):
            limits = copy.deepcopy(LIMITS)
            limits["primary"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                tl.parse_codex_response({"result": {"rateLimits": limits}})


class CodexSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rl = tl.parse_codex_response({"result": {"rateLimits": LIMITS}})

    def test_fresh_log_skips_probe(self):
        with patch.object(tl, "read_codex_logs", return_value=(NOW - 599, self.rl)), patch.object(tl, "probe_codex") as probe:
            result = tl.read_codex(NOW)
        probe.assert_not_called()
        self.assertEqual(result["sampled_at"], NOW - 599)
        self.assertEqual(result["five_hour"]["used"], 31)

    def test_stale_log_probes_at_ten_minutes(self):
        old = copy.deepcopy(self.rl)
        old["secondary"]["used_percent"] = 20
        with patch.object(tl, "read_codex_logs", return_value=(NOW - 600, old)), patch.object(tl, "probe_codex", return_value=(NOW + 1, self.rl)) as probe:
            result = tl.read_codex(NOW)
        probe.assert_called_once_with()
        self.assertEqual(result["sampled_at"], NOW + 1)
        self.assertEqual(result["week"]["used"], 25)
        self.assertIsNone(result["error"])

    def test_newer_log_wins_over_older_probe(self):
        with patch.object(tl, "read_codex_logs", return_value=(NOW - 600, self.rl)), patch.object(tl, "probe_codex", return_value=(NOW - 601, {})):
            self.assertEqual(tl.read_codex(NOW)["sampled_at"], NOW - 600)

    def test_no_logs_probes(self):
        with patch.object(tl, "read_codex_logs", return_value=None), patch.object(tl, "probe_codex", return_value=(NOW, self.rl)):
            self.assertEqual(tl.read_codex(NOW)["sampled_at"], NOW)

    def test_failure_preserves_old_timestamp_and_hides_error_details(self):
        for error in (TimeoutError("SECRET"), FileNotFoundError("SECRET"), ValueError("SECRET")):
            with self.subTest(error=type(error)), patch.object(tl, "read_codex_logs", return_value=(NOW - 18000, self.rl)), patch.object(tl, "probe_codex", side_effect=error):
                result = tl.read_codex(NOW)
            self.assertEqual(result["sampled_at"], NOW - 18000)
            self.assertEqual(result["week"]["used"], 25)
            self.assertIn("stale log sample", result["error"])
            self.assertNotIn("SECRET", result["error"])

    def test_both_sources_fail(self):
        with patch.object(tl, "read_codex_logs", return_value=None), patch.object(tl, "probe_codex", side_effect=TimeoutError):
            result = tl.read_codex(NOW)
        self.assertIsNone(result["sampled_at"])
        self.assertIsNone(result["week"]["used"])
        self.assertIn("no log sample", result["error"])

    def test_log_event_time_wins_over_file_mtime(self):
        with tempfile.TemporaryDirectory() as home:
            directory = os.path.join(home, ".codex/sessions/2026/10/12")
            os.makedirs(directory)
            for name, ts, mtime in (("new", NOW - 10, NOW - 100), ("old", NOW - 100, NOW)):
                path = os.path.join(directory, name + ".jsonl")
                with open(path, "w") as f:
                    json.dump({"timestamp": tl.datetime.fromtimestamp(ts, tl.timezone.utc).isoformat(), "payload": {"rate_limits": self.rl}}, f)
                    f.write("\n")
                os.utime(path, (mtime, mtime))
            with patch.object(tl, "HOME", home):
                self.assertEqual(tl.read_codex_logs()[0], NOW - 10)

    def test_log_without_timestamp_is_not_mistaken_for_fresh_sample(self):
        with tempfile.TemporaryDirectory() as home:
            directory = os.path.join(home, ".codex/sessions/2026/10/12")
            os.makedirs(directory)
            with open(os.path.join(directory, "sample.jsonl"), "w") as f:
                f.write(json.dumps({"rate_limits": self.rl}) + "\n")
            with patch.object(tl, "HOME", home):
                self.assertIsNone(tl.read_codex_logs())


class CodexProtocolTests(unittest.TestCase):
    def run_server(self, script, timeout=2):
        real_popen = subprocess.Popen
        processes = []
        def launch(argv, **kwargs):
            self.assertEqual(argv, ["codex", "app-server", "--listen", "stdio://"])
            proc = real_popen([sys.executable, "-u", "-c", script], **kwargs)
            processes.append(proc)
            return proc
        try:
            with patch.object(tl.subprocess, "Popen", side_effect=launch):
                return tl.probe_codex(timeout=timeout)
        finally:
            self.assertTrue(processes)
            self.assertIsNotNone(processes[0].poll())

    def test_only_initialize_and_read_requests(self):
        script = '''import json,sys
message = json.loads(sys.stdin.readline())
assert message['method'] == 'initialize' and message['id'] == 1
print('not json', flush=True)
print(json.dumps({'method': 'notification'}), flush=True)
print(json.dumps({'id': 1, 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
message = json.loads(sys.stdin.readline())
assert message == {'method': 'account/rateLimits/read', 'id': 2}
print(json.dumps({'id': 2, 'result': {'rateLimits': LIMITS}}), flush=True)
sys.stdin.read()
'''.replace("LIMITS", repr(LIMITS))
        ts, rl = self.run_server(script)
        self.assertLess(abs(time.time() - ts), 1)
        self.assertEqual(rl["secondary"]["used_percent"], 25)

    def test_timeout_kills_and_reaps_process(self):
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.run_server("import sys; sys.stdin.read()", timeout=0.1)
        self.assertLess(time.monotonic() - start, 1)

    def test_eof_is_failure(self):
        with self.assertRaises(ValueError):
            self.run_server("pass")

    def test_initialize_error_is_failure(self):
        with self.assertRaisesRegex(ValueError, "initialize failed"):
            self.run_server("import sys; sys.stdin.readline(); print('{\"id\":1,\"error\":{\"message\":\"SECRET\"}}',flush=True); sys.stdin.read()")


if __name__ == "__main__":
    unittest.main()
