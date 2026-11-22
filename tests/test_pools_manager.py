import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _load import REPO, tl

NOW = tl.iso_to_ts("2026-10-25T16:03:00+02:00")
CFG = {"pool_warn_pct": 80, "pool_off_pct": 95}


def bar(used, hours=100, minutes=10080):
    return tl.window(used, NOW + hours * 3600, minutes, NOW)



class PoolTests(unittest.TestCase):
    def test_boundaries(self):
        for used, expected in ((0, "ON"), (79.9, "ON"), (80, "WARN"), (94.9, "WARN"), (95, "OFF"), (100, "OFF")):
            with self.subTest(used=used):
                self.assertEqual(tl.pool_state(bar(used), None, CFG, NOW)["state"], expected)

    def test_five_hour_limit_and_config(self):
        state = tl.pool_state(bar(10), bar(95, 2, 300), CFG, NOW)
        self.assertEqual(state["state"], "OFF")
        self.assertEqual(state["used_pct"], 95)
        self.assertEqual(state["hours_left"], 2)
        self.assertEqual(tl.pool_state(bar(70), None, {"pool_warn_pct": 70}, NOW)["state"], "WARN")
        self.assertEqual(tl.pool_state(bar(90), None, {"pool_off_pct": 90}, NOW)["state"], "OFF")
        self.assertEqual(tl.pool_state(bar(10), bar(95, 2, 300), {"pool_off_pct": 99},
                                       NOW, five_hour_off_pct=95)["state"], "OFF")

    def test_late_period_with_headroom_stays_on(self):
        self.assertEqual(tl.pool_state(bar(40, 20), None, CFG, NOW)["state"], "ON")
        self.assertEqual(tl.pool_state(bar(90, 20), None, CFG, NOW)["state"], "WARN")

    def test_expired_windows_reset_even_after_multiple_periods(self):
        expired = {"used": 99, "resets_at": NOW - 400 * 3600, "hours_left": -400}
        state = tl.pool_state(expired, None, CFG, NOW)
        self.assertEqual(state["state"], "ON")
        self.assertGreater(state["resets_at"], NOW)

    def test_independent_pools_and_external_grok(self):
        with tempfile.TemporaryDirectory() as state, patch.object(tl, "STATE", state):
            s = {"claude": {"week": bar(96)}, "codex": {"week": bar(10)}}
            pools = tl.current_pools(s, NOW)
            self.assertEqual([p["state"] for p in pools.values()], ["OFF", "ON", "unknown", "unknown"])
            self.assertIn("assume 70% used for planning", tl.pool_line("grok", pools["grok"]))
            path = Path(state, "grok.json")
            path.write_text(json.dumps({"used_pct": 81, "resets_at": "2026-10-26T16:03:00+02:00", "updated_at": "2026-10-25T16:03:00+02:00"}))
            self.assertEqual(tl.current_pools(s, NOW)["grok"]["state"], "WARN")
            path.write_text(json.dumps({"used_pct": 99, "resets_at": "2026-10-24T16:03:00+02:00"}))
            self.assertEqual(tl.current_pools(s, NOW)["grok"]["state"], "unknown")
            path.write_text("{broken")
            self.assertEqual(tl.current_pools(s, NOW)["grok"]["state"], "unknown")

    def test_approved_credits_keep_codex_on_until_floor(self):
        rule = {**CFG, "codex_credits": {"approved": "https://app.hypertask.ai/detail/project-4060/911", "floor": 10000}}
        with tempfile.TemporaryDirectory() as state, patch.object(tl, "STATE", state):
            for cfg, credits, expected in ((rule, "57625.3", "ON"), (rule, "10000", "OFF"), (rule, None, "OFF"),
                                           (CFG, "57625.3", "OFF")):
                with self.subTest(credits=credits, approved=cfg is rule), patch.object(tl, "load_config", return_value=cfg):
                    s = {"codex": {"week": bar(99), "credits": credits}}
                    self.assertEqual(tl.current_pools(s, NOW)["codex"]["state"], expected)

    def test_pools_json_is_read_only_and_offline(self):
        with tempfile.TemporaryDirectory() as state, patch.object(tl, "STATE", state), \
                patch.object(tl, "STATUS", os.path.join(state, "status.json")), \
                patch("sys.argv", ["token-ladder", "pools", "--json"]), \
                patch("sys.stdout", new_callable=io.StringIO) as out, \
                patch.object(tl.subprocess, "run", side_effect=AssertionError("no subprocess")), \
                patch.object(tl.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            tl.pools_command()
            self.assertEqual(set(json.loads(out.getvalue())), {"claude", "codex", "grok", "google"})
            self.assertEqual(os.listdir(state), [])


class BashRulesTests(unittest.TestCase):
    def test_fast_model_ids_refused_without_launching_models(self):
        for exe in ("hax", "codex", "cursor", "cursor-agent", "grok"):
            for option in ("--model=example-fast", '--model "example-FAST"', "-m example-fast"):
                with self.subTest(exe=exe, option=option):
                    self.assertIn("fast mode is off", tl.bash_refusal(f"env X=1 timeout 60 {exe} {option}", {}))
        self.assertIn("fast mode is off", tl.bash_refusal('codex -c \'model="example-fast"\'', {}))
        self.assertIsNone(tl.bash_refusal('hax --model=gpt-6.1-sol -p "fast search"', {}))

    def test_deepseek_block_and_explicit_override(self):
        reason = "no DeepSeek fallback (Valentin 2026-10-25)"
        for exe in ("cc-deepseek-apikey", "ccr"):
            self.assertEqual(tl.bash_refusal(f"/usr/local/bin/{exe} start", {}), reason)
            self.assertIsNone(tl.bash_refusal(f"TOKEN_LADDER_DEEPSEEK_OK=1 {exe} start", {}))
            self.assertIsNone(tl.bash_refusal(f"{exe} start", {"TOKEN_LADDER_DEEPSEEK_OK": "1"}))
            self.assertEqual(tl.bash_refusal(f"{exe} start TOKEN_LADDER_DEEPSEEK_OK=1", {}), reason)
        self.assertIsNone(tl.bash_refusal('echo "ccr start"', {}))
        self.assertIsNone(tl.bash_refusal("python3 - <<'EOF'\ns='ccr start'\nEOF", {}))

    def test_pi_lanes_allowed_for_deepseek_flash_only(self):
        for ok in ("pi -p --provider vercel-ai-gateway --model deepseek/deepseek-v4.1-flash --thinking medium",
                   "pi --provider=vercel-ai-gateway --model=deepseek/deepseek-v4.1-flash",
                   "pi -p --provider deepseek --model deepseek-flash",
                   "pi -p --provider opencode-go --model deepseek-v4.1-flash",
                   "pi -p --provider openrouter-credit --model deepseek/deepseek-v4.1-flash-1017"):
            self.assertIsNone(tl.bash_refusal(ok, {}), ok)
        for command in ("pi --provider vercel-ai-gateway --model moonshotai/kimi-k3",
                        "pi --provider vercel-ai-gateway",
                        "pi --provider vercel-ai-gateway --model deepseek/deepseek-v4.1-flash --api-key placeholder",
                        "hax --provider vercel-ai-gateway --model deepseek/deepseek-v4.1-flash",
                        "pi --provider vercel-ai-gateway --provider openai --model deepseek/deepseek-v4.1-flash",
                        "pi --provider opencode-go --model deepseek-v4-pro",
                        "pi --provider deepseek --model deepseek-v4-pro"):
            self.assertIsNotNone(tl.bash_refusal(command, {}), command)

    def test_baseten_always_refused(self):
        for command in ("pi --provider baseten --model deepseek-ai/DeepSeek-V4.1-Flash-1017",
                        "TOKEN_LADDER_OPENROUTER_OK=1 pi --provider=baseten --model deepseek-flash"):
            self.assertIn("never Baseten", tl.bash_refusal(command, {}))

    def test_paid_fallbacks_blocked_but_codex_allowed(self):
        self.assertIsNone(tl.bash_refusal("hax --provider=codex --model=gpt-6.1-sol", {}))
        for command in ("hax --provider=openrouter", "hax --provider openai", "hax --api-key placeholder"):
            self.assertIsNotNone(tl.bash_refusal(command, {}))
        self.assertIsNone(tl.bash_refusal("TOKEN_LADDER_OPENROUTER_OK=1 hax --provider=openrouter", {}))
        self.assertIsNotNone(tl.bash_refusal("hax --provider=openrouter -p TOKEN_LADDER_OPENROUTER_OK=1", {}))


class RemovedCommandTests(unittest.TestCase):
    def test_pick_manager_and_targets_are_unknown_commands(self):
        for argv in (("pick", "--role", "runner", "--harness", "hax"), ("targets",), ("manager", "status")):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as home:
                result = subprocess.run([sys.executable, os.path.join(REPO, "bin/token-ladder"), *argv],
                                        capture_output=True, text=True, timeout=20,
                                        env=dict(os.environ, HOME=home))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unknown command: " + argv[0], result.stderr)


if __name__ == "__main__":
    unittest.main()
