import json, os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from _load import tl
cfg = json.load(open(os.path.join(os.path.dirname(__file__), "..", "config.json")))
NOW = tl.iso_to_ts("2026-10-10T09:30:00+00:00")
H = 3600

def bars(used, hours_left, minutes=10080):
    return tl.window(used, NOW + hours_left * H, minutes, NOW)

def cl(week, h, five=1):
    return {"week": bars(week, h), "five_hour": bars(five, 3, 300)}

def cx(week, h):
    return {"week": bars(week, h), "five_hour": tl.window(None, None, 300, NOW)}

m, why, adv = tl.decide(cl(34, 31), cx(60, 91), cfg, NOW)
assert m == "claude", (m, why)
assert tl.decide(cl(80, 31), cx(60, 91), cfg, NOW)[0] == "codex"
assert tl.decide(cl(85, 100), cx(60, 91), cfg, NOW)[0] == "codex"
assert tl.decide(cl(34, 31, five=85), cx(60, 91), cfg, NOW)[0] == "codex"
assert tl.decide(cl(70, 150), cx(100, 50), cfg, NOW)[0] == "claude"
adv = tl.decide(cl(80, 31), cx(96, 60), cfg, NOW)[2]
assert any("Use the Codex reset expiring " + cfg["codex_resets"][0]["expires"][:10] in a for a in adv), adv
adv = tl.decide(cl(80, 31), cx(96, 10), cfg, NOW)[2]
assert not any("Use the Codex reset" in a for a in adv), adv
m, why, adv = tl.decide(cl(99, 31), cx(100, 50), cfg, NOW)
assert m == "codex" and any("OpenRouter needs Valentin" in a for a in adv)
assert tl.decide(cl(34, 31), cx(60, 91), cfg, NOW, override="codex")[0] == "codex"
assert tl.window(70, NOW - H, 10080, NOW)["used"] == 0
L = tl.launches_hax
assert L("hax --provider=codex -p hi")
assert L("cd x && FOO=1 nohup hax -p hi &")
assert L("timeout 60 hax -p x")
assert L("out=$(hax -p x)")
assert not L("grep -n 'via hax (' file")
assert not L("python3 - <<'EOF'\ns='use hax --provider=codex'\nEOF")
assert not L("echo hax is great")
acc = tl.build_accounts({"extra_claude_accounts": [{"id": "claude2", "label": "Claude B", "credentials": "/nonexistent"}]},
                        cl(34, 31), cx(60, 91), NOW)
assert [a["id"] for a in acc] == ["claude", "codex", "claude2"]
assert acc[2]["label"] == "Claude B" and acc[2]["error"] and acc[2]["week"]["used"] is None
assert acc[0]["week"]["used"] == 34 and "token" not in json.dumps(acc).lower()
base = {"provider": "claude", "error": None, "host": "mirror", "week": bars(36, 33)}
others = [{"provider": "claude", "email": "a@x", "label": "Claude (private)"}]
n = tl.standalone_note(dict(base, email="a@x", expected_email="b@y"), others)
assert "same plan" in n and "not being used" in n, n
n = tl.standalone_note(dict(base, email="b@y"), others)
assert "behind pace" in n, n
st = {}
for tight_now, want in [(True, "ok"), (True, "tight"), (False, "tight"), (True, "tight"), (False, "tight"), (False, "ok")]:
    st = tl.pressure_step(st, tight_now)
    assert st["state"] == want, (tight_now, st)
R = dict(ram_free_gb=10, ram_total_gb=94, load1=3, load5=3, cores=18, claude={"count": 1, "rss_gb": 1}, node={"count": 1, "rss_gb": 1}, codex={"count": 0, "rss_gb": 0})
assert tl.raw_tight(R, cfg) and not tl.raw_tight(dict(R, ram_free_gb=44), cfg) and tl.raw_tight(dict(R, ram_free_gb=44, load5=40), cfg)
S = {"ts": NOW, "mode": "claude", "advice": [], "claude": {"week": bars(34, 31)}, "codex": {"week": bars(60, 91)}}
assert "Server tight (RAM 10 GB free, load 3.0)" in tl.mode_line(dict(S, ts=tl.time.time(), resources=dict(R, pressure="tight")))
assert "Server tight" not in tl.mode_line(dict(S, ts=tl.time.time(), resources=dict(R, pressure="ok")))
print("ALL TESTS PASSED")
st = {"accounts": [{"id": "client", "label": "Client", "email": "user@example.invalid", "week": bars(10, 80)}]}
assert "Plenty left" in tl.token_max_line(st, "client")
soon = tl.datetime.fromtimestamp(tl.time.time() + 3 * 86400, tl.timezone.utc).isoformat()
n = tl.standalone_note(dict(base, email="b@y", week=bars(97, 60), resets=[{"expires": soon}]), others)
assert "press the free reset expiring " + soon[:10] in n, n
print("TOKEN MAX OK")
assert tl.decide(cl(90, 5), cx(1, 160), cfg, NOW)[0] == "claude"
assert tl.decide(cl(90, 100), cx(1, 160), cfg, NOW)[0] == "codex"
print("FLOOR OK")
XML = ("<item><title>[Codex] Direct reset · 2026-10-07 03:35 UTC — x</title></item>"
       "<item><title>[Codex] Banked reset · 2026-10-08 03:44 UTC — y</title></item>"
       "<item><title>[Codex] Direct reset · 2026-10-02 21:18 UTC — z</title></item>"
       "<item><title>[Grok] Direct reset · 2026-10-05 10:00 UTC — g</title></item>")
fd = tl.parse_reset_feed(XML, NOW)
assert fd["codex"]["direct_30d"] == 2 and "claude" not in fd, fd
h, p = tl.effective_hours(bars(1, 160), {"direct_30d": 5, "pending_at": None}, NOW)
assert 60 < h < 90 and p is None, h
h, p = tl.effective_hours(bars(50, 100), {"direct_30d": 5, "pending_at": NOW + 3 * H}, NOW)
assert 2.9 < h < 3.1 and p, h
h, p = tl.effective_hours(bars(1, 167), {"direct_30d": 5, "pending_at": NOW + 2 * H}, NOW)
assert p is None
print("FEED OK")


class PoolPressureTests(unittest.TestCase):
    def test_pressure_is_independent_of_legacy_mode(self):
        for mode in ("claude", "codex"):
            line = tl.mode_line(dict(S, mode=mode, ts=tl.time.time(), resources=dict(R, pressure="tight")))
            self.assertIn("Server tight (RAM 10 GB free, load 3.0): pace local activity down.", line)
            self.assertIn("No CI, builds, tests or AI review on this VPS", line)


class ResetBurnTests(unittest.TestCase):
    def cfg(self, hours):
        c = dict(cfg)
        c["claude_resets"] = [{"expires": tl.datetime.fromtimestamp(NOW + hours * H, tl.timezone.utc).isoformat()}]
        return c

    def test_claude_reset_within_a_day_says_burn_whatever_the_percent(self):
        m, why, adv = tl.decide(cl(76, 91), cx(45, 129), self.cfg(20), NOW, override="codex")
        self.assertEqual(m, "codex")
        self.assertTrue(any(a.startswith("BURN CLAUDE") for a in adv), adv)
        self.assertTrue(tl.burn_plan(cl(76, 91), cx(45, 129), self.cfg(20), NOW)["claude"]["burn_now"])

    def test_far_claude_reset_stays_quiet(self):
        adv = tl.decide(cl(76, 91), cx(45, 129), self.cfg(60), NOW)[2]
        self.assertFalse(any("Claude reset" in a for a in adv), adv)
        self.assertFalse(tl.burn_plan(cl(76, 91), cx(45, 129), self.cfg(60), NOW)["claude"]["burn_now"])

    def test_full_claude_near_expiry_says_press_it(self):
        adv = tl.decide(cl(96, 10), cx(45, 129), self.cfg(5), NOW)[2]
        self.assertTrue(any(a.startswith("Use the Claude reset") for a in adv), adv)

    def test_burn_line_names_both_accounts_and_expiry(self):
        c = self.cfg(20)
        s = {"mode": "codex", "burn": tl.burn_plan(cl(76, 91), cx(45, 129), c, NOW)}
        line = tl.burn_line(s)
        self.assertIn("Claude BURN NOW: 24% left, reset expires", line)
        self.assertIn("Codex BURN NOW: 55% left", line)

class ForcedClaudeFloorTests(unittest.TestCase):
    def test_forced_claude_holds_above_the_floor(self):
        self.assertEqual(tl.decide(cl(60, 31), cx(40, 91), cfg, NOW, override="claude")[0], "claude")

    def test_forced_claude_ends_at_the_floor(self):
        m, why, adv = tl.decide(cl(100 - cfg["claude_floor_percent"], 31), cx(40, 91), cfg, NOW, override="claude")
        self.assertEqual(m, "codex")
        self.assertIn("forced Claude ended", why)
        self.assertIn(f"{cfg['claude_floor_percent']}% floor", why)

    def test_forced_codex_ignores_the_claude_floor(self):
        self.assertEqual(tl.decide(cl(95, 31), cx(40, 91), cfg, NOW, override="codex")[0], "codex")


class PaceWarningTests(unittest.TestCase):
    def test_codex_over_pace_says_when_it_runs_out(self):
        w = bars(52, 150)
        msg = tl.pace_warning("Codex", w)
        self.assertTrue(msg.startswith("Codex over pace: at this rate empty in 17 h, 6 days before reset"), msg)

    def test_on_pace_is_quiet(self):
        self.assertIsNone(tl.pace_warning("Claude", bars(47, 52)))

    def test_bonus_reset_counts_as_room(self):
        self.assertIsNotNone(tl.pace_warning("Codex", bars(52, 150)))
        self.assertIsNone(tl.pace_warning("Codex", bars(52, 150), bonus_resets=9))

    def test_unknown_or_empty_is_quiet(self):
        self.assertIsNone(tl.pace_warning("Codex", tl.window(None, None, 10080, NOW)))
        self.assertIsNone(tl.pace_warning("Codex", bars(0, 100)))

    def test_burn_line_shows_the_warning(self):
        c = dict(cfg)
        c["codex_resets"] = []
        s = {"mode": "claude", "burn": tl.burn_plan(cl(47, 52), cx(52, 150), c, NOW)}
        line = tl.burn_line(s)
        self.assertIn("Codex keep: 48% left, no reset (Codex over pace: at this rate empty in 17 h", line)
        self.assertNotIn("Claude over pace", line)


class AgentHookTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.status = Path(self.home.name) / ".local/state/token-ladder/status.json"
        self.status.parent.mkdir(parents=True)
        self.status.write_text(json.dumps({"mode": "codex"}))
        self.env = dict(os.environ, HOME=self.home.name)
        self.env.pop("TOKEN_LADDER_ALLOW", None)

    def hook(self, prompt="Read the code and implement the change", subagent_type="general-purpose"):
        return subprocess.run([sys.executable, tl.__file__, "hook-agent"],
                              input=json.dumps({"tool_name": "Agent", "tool_input": {
                                  "prompt": prompt, "subagent_type": subagent_type}}),
                              env=self.env, text=True, capture_output=True, timeout=10)

    def test_allows_agent_while_claude_reset_burns(self):
        self.status.write_text(json.dumps({"mode": "codex", "burn": {"claude": {"burn_now": True}}}))
        self.assertEqual(self.hook().returncode, 0)

    def test_allows_claude_agent_regardless_of_codex_mode(self):
        result = self.hook()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_allows_zsb_prompt(self):
        result = self.hook("Use zsb to inspect the page")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_allows_other_claude_only_tools(self):
        for tool in ("browser", "screenshot", "Artifact", "SendMessage", "MCP", "mcp__custom__inspect", "ZSB"):
            with self.subTest(tool=tool):
                self.assertEqual(self.hook(f"Use {tool} for this task").returncode, 0)

    def test_does_not_match_keywords_inside_unrelated_words(self):
        self.assertEqual(self.hook("Implement artifacts_count and browserless_count").returncode, 0)

    def test_allows_claude_code_guide(self):
        self.assertEqual(self.hook(subagent_type="claude-code-guide").returncode, 0)

    def test_allows_claude_mode(self):
        self.status.write_text(json.dumps({"mode": "claude"}))
        self.assertEqual(self.hook().returncode, 0)

    def test_allows_environment_override(self):
        self.env["TOKEN_LADDER_ALLOW"] = "1"
        self.assertEqual(self.hook().returncode, 0)

    def test_other_environment_values_do_not_override(self):
        self.env["TOKEN_LADDER_ALLOW"] = "0"
        self.assertEqual(self.hook().returncode, 0)

    def test_allows_missing_status(self):
        self.status.unlink()
        self.assertEqual(self.hook().returncode, 0)

    def test_allows_unreadable_status(self):
        self.status.unlink()
        self.status.mkdir()
        self.assertEqual(self.hook().returncode, 0)

    def test_allows_invalid_status(self):
        for text in ("{broken", "null", "[]", "{}"):
            with self.subTest(text=text):
                self.status.write_text(text)
                self.assertEqual(self.hook().returncode, 0)

    def test_claude_off_blocks_agent_even_in_codex_mode(self):
        self.status.write_text(json.dumps({"mode": "codex", "claude": {
            "week": {"used": 95, "resets_at": tl.time.time() + 3600, "hours_left": 1}}}))
        result = self.hook()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
