import builtins
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _load import REPO, load_bin, tl

pp = load_bin("plan-pick")

NOW = tl.iso_to_ts("2026-10-24T09:00:00+00:00")
DASH_NOW = tl.iso_to_ts("2026-11-23T19:38:00+00:00")
FIXTURES = Path(REPO) / "tests" / "fixtures"
EXHAUSTED_LOG = ("E1123 09:05:00.123456      47 quota_manager.go:120] doRefreshQuota: refresh failed: "
                 "rpc error: code = ResourceExhausted desc = quota exceeded for gemini-4-neon-high")


def win(used, time_gone, minutes=10080, now=NOW):
    hours_left = minutes * (1 - time_gone / 100) / 60
    return tl.window(used, now + hours_left * 3600, minutes, now)


def base_status(cfg=None, now=DASH_NOW, pools=None, limits=None, credits=None):
    cfg = copy.deepcopy(cfg if cfg is not None else tl.load_config())
    s = {
        "ts": now,
        "mode": "codex",
        "why": "forced by Valentin (codex)",
        "override": "codex",
        "advice": [],
        "claude": {"week": win(15, 16, 10080, now), "five_hour": tl.window(None, None, 300, now),
                   "email": "test@example.invalid", "error": None},
        "codex": {"week": win(20, 13, 10080, now), "five_hour": tl.window(None, None, 300, now),
                  "email": "test@example.invalid", "error": None, "credits": 0},
        "openrouter": {"usage": None, "limit": None, "error": "OPENROUTER_API_KEY not set"},
        "accounts": [],
        "config": cfg,
        "global_resets": {"codex": {"direct_30d": 2}, "claude": {"direct_30d": 0}},
        "resources": {"ram_free_gb": 64.0, "ram_total_gb": 94.0, "load1": 2.0, "load5": 2.5, "cores": 18,
                      "pressure": "ok", "claude": {"count": 2, "rss_gb": 1.2},
                      "node": {"count": 1, "rss_gb": 0.4}, "codex": {"count": 1, "rss_gb": 0.1}},
    }
    if pools is not None:
        s["pools"] = pools
    if limits is not None:
        s["limits"] = limits
    if credits is not None:
        s["credits"] = credits
    return s


class UsageRowTests(unittest.TestCase):
    def test_row_shows_pace_and_reset_clock(self):
        line = tl.usage_line(win(50, 50))
        self.assertIn("50% used", line)
        self.assertIn("ON PACE", line)
        self.assertRegex(line, r"resets [A-Z][a-z]{2} \d{2}:\d{2}, in \d+ h")

    def test_row_says_behind_when_the_bar_is_left_of_the_tick(self):
        self.assertIn("BEHIND", tl.usage_line(win(20, 50)))

    def test_row_says_ahead_when_the_bar_is_right_of_the_tick(self):
        self.assertIn("AHEAD", tl.usage_line(win(80, 20)))

    def test_ten_points_off_pace_still_reads_on_pace(self):
        self.assertIn("ON PACE", tl.usage_line(win(40, 50)))
        self.assertIn("ON PACE", tl.usage_line(win(60, 50)))

    def test_row_without_numbers_has_no_pace_word(self):
        line = tl.usage_line(tl.window(None, None, 10080, NOW))
        self.assertIn("unknown", line)
        self.assertNotIn("PACE", line)


class DashboardRendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / ".local/state/token-ladder"
        self.pacing = self.home / ".local/state/pacing"
        self.state.mkdir(parents=True)
        self.pacing.mkdir(parents=True)
        self.patches = [
            mock.patch.object(tl, "HOME", str(self.home)),
            mock.patch.object(tl, "STATE", str(self.state)),
            mock.patch.object(tl, "STATUS", str(self.state / "status.json")),
            mock.patch.object(tl, "RESOURCES", str(self.state / "resources.jsonl")),
            mock.patch.dict(os.environ, {"PLAN_PICK_PACING_DIR": str(self.pacing),
                                         "PLAN_PICK_LAUNCHES": str(self.state / "launches.jsonl")}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
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

    def write_state(self, name, data):
        (self.state / name).write_text(json.dumps(data))

    def commandcode_credit(self, credits=69.5, five_used=1.0, week_used=2.0, at=DASH_NOW - 600):
        return {
            "credits": credits,
            "windows": {
                "fiveHour": {"used": five_used, "cap": 14, "exceeded": False, "resetAt": int((DASH_NOW + 7200) * 1000)},
                "weekly": {"used": week_used, "cap": 35, "exceeded": False, "resetAt": int((DASH_NOW + 86400 * 3) * 1000)},
            },
            "period_start": DASH_NOW - 86400 * 2,
            "period_end": DASH_NOW + 86400 * 28,
            "at": at,
        }

    def test_renders_all_plans_lanes_and_removes_stale_cursor_placeholder(self):
        self.write_state("grok.json", {
            "pct": 91.6,
            "resets_at": "2026-11-28T16:10:23+00:00",
            "updated_at": "2026-11-23T18:35:00+00:00",
        })
        self.write_state("lane-limits.json", {
            "cursor": tl.iso_to_ts("2026-12-23T19:35:30+00:00"),
            "deepseek": tl.iso_to_ts("2027-11-22T21:43:39+00:00"),
        })
        self.write_state("lane-credits.json", {
            "vercel": {"usd": 51.02, "resets_at": "2026-12-05T08:00:00Z", "at": DASH_NOW - 300},
            "commandcode": self.commandcode_credit(),
            "openrouter": {"usd": -0.2, "at": DASH_NOW - 300},
            "opencode": {"usd": 6.12, "at": DASH_NOW - 600, "source": "OpenCode live console"},
        })
        (self.pacing / "opencode-history.jsonl").write_text(json.dumps({
            "at": DASH_NOW - 120,
            "windows": {"5-hour": 0.0, "weekly": 0.0, "monthly": 100.0},
        }) + "\n")
        agy_dir = self.home / ".gemini/antigravity-cli"
        agy_dir.mkdir(parents=True)
        (agy_dir / "history.jsonl").write_text(json.dumps({"timestamp": int((DASH_NOW - 60) * 1000)}) + "\n")

        page = tl.render_dashboard(base_status())
        self.assertNotIn("not tracked yet", page)
        for text in (
            "Claude",
            "ChatGPT / Codex",
            "Cursor",
            "Grok 4.7",
            "pool: grok",
            "91.6% / 100%",
            "2026-12-23 19:35 UTC",
            "manual skip (lane-limits)",
            "USED UP · SKIPPED",
            "Vercel AI Gateway Token Pass",
            "$58.98 / $110 ($51.02 left)",
            "2026-12-05 08:00 UTC",
            "OpenCode Go Plus",
            "EXTRA USAGE ON",
            "$6.12 prepaid left",
            "Google Antigravity plan",
            "Gemini 4 Neon",
            "shared with chats",
            "1 / unknown local history entries today (UTC)",
            "antigravity",
            "CommandCode GOAT",
            "credit:commandcode",
            "0.5 / 70 plan credits",
            "OpenRouter",
            "DeepSeek direct",
            "RETIRED",
            "Next worker lane:",
        ):
            self.assertIn(text, page)
        self.assertNotIn("public repos only", page)

    def test_cloud_agents_badges_follow_plan_fixtures(self):
        cfg = json.loads((FIXTURES / "dashboard-cloud-plans.json").read_text())
        page = tl.render_dashboard(base_status(cfg=cfg, pools={}))
        cards = page.split('<div class="card">')[1:]
        for plan in cfg["plans"]:
            with self.subTest(plan=plan["name"]):
                card = next(card for card in cards if f'<b>{plan["name"]}</b>' in card)
                self.assertEqual('<span class="badge info">Cloud agents</span>' in card,
                                 plan["name"] in {"Cloud workshop", "Hybrid studio"})

    def test_cloud_agents_badges_on_supported_plans(self):
        cfg = tl.load_config()
        page = tl.render_dashboard(base_status(cfg=cfg, pools={}))
        cards = page.split('<div class="card">')[1:]
        for plan in cfg["plans"]:
            with self.subTest(plan=plan["name"]):
                card = next(card for card in cards if f'<b>{plan["name"]}</b>' in card)
                self.assertEqual('<span class="badge info">Cloud agents</span>' in card,
                                 plan["name"] in {"Claude", "ChatGPT / Codex", "Cursor"})

    def test_antigravity_cloud_agents_badge_requires_config_opt_in(self):
        cfg = tl.load_config()
        plan = next(plan for plan in cfg["plans"] if plan.get("pool") == "google")
        plan["runs_on"] = ["local", "cloud"]
        page = tl.render_dashboard(base_status(cfg=cfg, pools={}))
        card = next(card for card in page.split('<div class="card">')[1:]
                    if '<b>Google Antigravity plan</b>' in card)
        self.assertIn('<span class="badge info">Cloud agents</span>', card)

    def test_grok_used_up_comes_from_grok_json_or_limits_never_hardcoded(self):
        self.write_state("grok.json", {
            "pct": 100,
            "resets_at": "2026-11-28T16:10:00+00:00",
            "updated_at": "2026-11-23T19:00:00+00:00",
        })
        exhausted = tl.render_dashboard(base_status(limits={}))
        self.assertIn("100% / 100%", exhausted)
        self.assertIn("USED UP", exhausted)
        self.assertIn("2026-11-28 16:10 UTC", exhausted)
        self.assertNotIn("Manual skips", exhausted)

        self.write_state("grok.json", {
            "pct": 20,
            "resets_at": "2026-11-28T16:10:00+00:00",
            "updated_at": "2026-11-23T19:00:00+00:00",
        })
        healthy = tl.render_dashboard(base_status(limits={}))
        snap = tl.dashboard_data(base_status(limits={}))
        cursor_plan = next(p for p in snap["plans"] if p["pool_key"] == "grok")
        self.assertEqual(cursor_plan["status"], "ON")
        self.assertFalse(cursor_plan["used_up"])
        self.assertIn("20% / 100%", healthy)

        limited = tl.render_dashboard(base_status(limits={
            "grok": {"until": "2026-12-23T19:35:00Z", "reason": "grok quota used up"},
        }))
        self.assertIn("USED UP · SKIPPED", limited)
        self.assertIn("2026-12-23 19:35 UTC", limited)
        self.assertIn("grok quota used up", limited)

    def test_manual_skips_support_reasons_floor_locks_expiry_and_escaping(self):
        limits = {
            "cursor": tl.iso_to_ts("2026-12-23T19:35:00+00:00"),
            "cursor:reason": "used up <billing> cycle",
            "opencode": tl.iso_to_ts("2026-11-30T23:00:00+00:00"),
            "opencode:floor": tl.iso_to_ts("2026-11-30T23:00:00+00:00"),
            "codex": {"until": DASH_NOW + 3600, "reason": "temporary 429 cooldown"},
            "vercel": DASH_NOW - 60,
        }
        page = tl.render_dashboard(base_status(limits=limits))
        self.assertIn("used up &lt;billing&gt; cycle", page)
        self.assertNotIn("<billing>", page)
        self.assertIn("credit floor reached", page)
        self.assertIn("temporary 429 cooldown", page)
        snap = tl.dashboard_data(base_status(limits=limits))
        skip_lanes = [sk["lane"] for sk in snap["skips"]]
        self.assertEqual(skip_lanes, ["codex", "opencode", "cursor"])
        codex_lane = next(l for l in snap["lanes"] if l["lane"] == "codex")
        self.assertEqual(codex_lane["status"], "SKIPPED")
        self.assertFalse(codex_lane["used_up"])

    def test_used_up_antigravity_and_commandcode_states(self):
        agy_dir = self.home / ".gemini/antigravity-cli"
        agy_dir.mkdir(parents=True)
        (agy_dir / "cli.log").write_text(EXHAUSTED_LOG + "\n")
        (agy_dir / "history.jsonl").write_text(json.dumps({"timestamp": int((DASH_NOW - 60) * 1000)}) + "\n")

        self.write_state("lane-credits.json", {
            "commandcode": self.commandcode_credit(credits=0),
        })
        page = tl.render_dashboard(base_status(limits={}))
        snap = tl.dashboard_data(base_status(limits={}))
        google_plan = next(p for p in snap["plans"] if p["pool_key"] == "google")
        cc_plan = next(p for p in snap["plans"] if p["pool_key"] == "credit:commandcode")
        self.assertEqual(google_plan["status"], "USED UP")
        self.assertEqual(cc_plan["status"], "USED UP")
        self.assertIn("Used up at 2026-11-23T09:05:00Z", page)
        self.assertIn("70 / 70 plan credits", page)

        self.write_state("lane-credits.json", {
            "commandcode": self.commandcode_credit(at=DASH_NOW - 8 * 3600),
        })
        stale_snap = tl.dashboard_data(base_status(limits={}))
        stale_cc = next(p for p in stale_snap["plans"] if p["pool_key"] == "credit:commandcode")
        self.assertEqual(stale_cc["status"], "OFF")
        self.assertFalse(stale_cc["used_up"])
        self.assertIn("CommandCode credit reading missing or stale", tl.render_dashboard(base_status(limits={})))

    def test_chat_only_and_shared_pools_and_new_lanes_render_dynamically(self):
        cfg = {
            "ladder_name": "Dynamic Test",
            "pace_warn_ratio": 1.1,
            "claude_floor_percent": 15,
            "plans": [
                {
                    "name": "Chat Vault",
                    "vendor": "vault",
                    "pool": "vault",
                    "included_models": ["Vault 1"],
                    "runs_on": ["local"],
                    "extra_cost": "none",
                },
                {
                    "name": "Shared Studio",
                    "vendor": "studio",
                    "pool": "studio",
                    "shared_with_chats": True,
                    "included_models": ["Studio 2"],
                    "runs_on": ["local"],
                    "extra_cost": "none",
                },
                {
                    "name": "Acme Coder",
                    "vendor": "acme",
                    "pool": "acme",
                    "included_models": ["Acme Pro"],
                    "runs_on": ["local", "cloud"],
                    "extra_cost": "$20/month",
                },
            ],
            "lanes": [
                {"lane": "vault-worker", "vendor": "vault", "pool": "vault", "command": "run vault"},
                {"lane": "studio-worker", "vendor": "studio", "pool": "studio",
                 "shared_with_chats": True, "command": "run studio"},
                {"lane": "acme-worker", "vendor": "acme", "pool": "acme", "command": "run acme"},
                {"lane": "zephyr", "vendor": "zephyr", "pool": "zephyr", "command": "run zephyr"},
            ],
        }
        pools = {
            "vault": {"state": "ON", "chat_only": True, "used": 4, "limit": 20, "unit": "chats", "pace_ratio": 0.4,
                      "resets_at": DASH_NOW + 3600 * 12},
            "studio": {"state": "ON", "shared_with_chats": True, "used": 2, "limit": 10, "unit": "runs",
                       "pace_ratio": 0.5, "resets_at": DASH_NOW + 3600 * 6},
            "acme": {"state": "ON", "used_pct": 42, "elapsed_pct": 50, "pace_ratio": 0.84,
                     "resets_at": DASH_NOW + 3600 * 48},
            "zephyr": {"state": "ON", "used_pct": 10, "elapsed_pct": 25, "pace_ratio": 0.4,
                       "resets_at": DASH_NOW + 3600 * 72},
        }
        s = base_status(cfg=cfg, pools=pools, limits={"acme-worker": {"until": DASH_NOW + 7200, "reason": "maintenance"}})
        page = tl.render_dashboard(s)
        snap = tl.dashboard_data(s)
        self.assertEqual(snap["picked"]["plan"], "studio-worker")
        vault_lane = next(l for l in snap["lanes"] if l["lane"] == "vault-worker")
        self.assertEqual(vault_lane["status"], "CHAT ONLY")
        self.assertTrue(vault_lane["chat_only"])
        for text in (
            "Chat Vault",
            "CHAT ONLY",
            "chat-only pool, never workers",
            "4 / 20 chats",
            "Shared Studio",
            "shared with chats",
            "studio-worker",
            "2 / 10 runs",
            "Acme Coder",
            "acme-worker",
            "42% / 100%",
            "0.84x",
            "maintenance",
            "zephyr",
            "10% / 100%",
            "Next worker lane: <b>studio-worker</b>",
        ):
            self.assertIn(text, page)

    def test_render_dashboard_is_passive_and_offline_without_reading_secrets(self):
        token_file = self.home / ".gemini/antigravity-cli/antigravity-oauth-token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("sentinel-secret-value")
        with mock.patch("builtins.open", wraps=builtins.open) as opened, \
                mock.patch.object(tl.subprocess, "run", side_effect=AssertionError("no subprocess")), \
                mock.patch.object(tl.urllib.request, "urlopen", side_effect=AssertionError("no network")):
            page = tl.render_dashboard(base_status())
        self.assertNotIn("sentinel-secret-value", page)
        for call in opened.call_args_list:
            self.assertNotIn("oauth", str(call.args[0]).lower())
            self.assertNotIn("credentials", str(call.args[0]).lower())
        self.assertEqual(os.listdir(self.state), [])

    def test_mark_limited_with_reason_and_dict_limits_work_in_plan_pick(self):
        path = str(self.state / "lane-limits.json")
        until = pp.mark_limited("cursor", 48, DASH_NOW, path, reason="quota exhausted")
        raw = pp.read_limits(path)
        self.assertEqual(raw["cursor"], until)
        self.assertEqual(raw["cursor:reason"], "quota exhausted")
        parsed = pp.parse_limit(raw, "cursor", DASH_NOW)
        self.assertEqual(parsed["reason"], "quota exhausted")
        lanes = [{"lane": "cursor", "pool": "grok", "command": "run cursor"},
                 {"lane": "codex", "pool": "codex", "command": "run codex"}]
        got = pp.pick(lanes, {"grok": {"state": "ON"}, "codex": {"state": "ON"}},
                      {"cursor": {"until": DASH_NOW + 3600, "reason": "paused"}}, DASH_NOW)
        self.assertEqual(got["plan"], "codex")
        self.assertIn("cursor (limited until", got["reason"])
        pp.mark_limited("codex", 1, until + 10, path)
        self.assertNotIn("cursor:reason", pp.read_limits(path))


if __name__ == "__main__":
    unittest.main()
