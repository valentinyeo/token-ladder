import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _load import REPO, load_bin

pp = load_bin("plan-pick")


class RungTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.brief = self.root / "brief.txt"
        self.log = str(self.root / "launches.jsonl")
        self.cfg = pp.load_config()
        self.lanes = pp.lanes_from(self.cfg)
        self.limits = str(self.root / "limits.json")

    def context(self, text="Rung: light\n", lease=None):
        self.brief.write_text(text)
        return pp.rung_context(str(self.brief), self.log, lease)

    def test_brief_and_default_and_invalid_rungs(self):
        self.assertEqual(self.context()["rung_source"], "brief line")
        ctx = self.context("Do something\n")
        self.assertEqual((ctx["rung"], ctx["rung_source"]), ("medium", "default"))
        for text in ("Rung: turbo\n", "Rung: light\nRung: hard\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.context(text)

    def test_light_hard_and_medium_model_effort_and_order(self):
        light = pp.rung_lanes(self.lanes, self.cfg, "light")
        hard = pp.rung_lanes(self.lanes, self.cfg, "hard")
        medium = pp.rung_lanes(self.lanes, self.cfg, "medium")
        self.assertEqual(light[0]["lane"], "opencode")
        self.assertTrue(all(l["vendor"] in ("deepseek", "zai") and l["effort"] == "low" for l in light))
        self.assertEqual([l["lane"] for l in hard], ["codex", "antigravity"])
        self.assertEqual(hard[0]["model"], "gpt-6.1-sol")
        self.assertIn("--effort high", hard[0]["command"])
        self.assertEqual(hard[1]["model"], "gemini-4-neon-high")
        self.assertEqual([l["lane"] for l in medium], [l["lane"] for l in self.lanes if l["lane"] != "vercel-zai"])
        for lane in light + medium + hard:
            self.assertIn(lane["model"], self.cfg["list_prices"])

    def test_limits_and_pace_climb_but_hard_waits(self):
        ctx = self.context()
        limits = {l["lane"]: 200 for l in pp.rung_lanes(self.lanes, self.cfg, "light")}
        result, climbed, _ = pp.pick_rung(self.lanes, {}, self.cfg, ctx, limits, 1.1, now=100)
        self.assertEqual(climbed["rung"], "medium")
        self.assertNotIn(result["plan"], limits)
        pools = {"codex": {"pace_ratio": 1.2}, "google": {"state": "OFF"}}
        result, _, _ = pp.pick_rung(self.lanes, pools, self.cfg, self.context("Rung: hard\n"), {}, 1.1, now=100)
        self.assertEqual(result["plan"], "none")
        code = pp.run_lanes(self.lanes, pools, self.cfg, 1.1, rung=self.context("Rung: hard\n"),
                            runner=mock.Mock(side_effect=AssertionError("must wait")),
                            limits_path=self.limits, launches_path=self.log, now=100, say=lambda _: None)
        self.assertEqual(code, 75)

    def test_failed_run_climbs_by_hash_and_ticket_survives_edited_brief(self):
        lease = self.root / "VSTA-166.json"
        lease.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
        for code, pr in ((1, "https://github.com/a/b/pull/31"), (0, None)):
            ctx = self.context(lease=str(lease))
            lane = pp.rung_lanes(self.lanes, self.cfg, ctx["rung"])[0]
            output = pp.RunOutput("failed")
            output.pr_url = pr
            output.tokens = None
            pp.launch_finish(self.log, str(code), ctx, lane, code, output, self.cfg)
            next_ctx = self.context("Rung: light\nEdited brief\n", str(lease))
            self.assertEqual(next_ctx["ticket"], "VSTA-166")
            self.assertEqual(next_ctx["rung"], "medium" if code == 1 else "hard")
        self.assertIn("prior run failed", next_ctx["rung_reason"])
        self.assertNotEqual(self.context("one")["job"], self.context("two")["job"])

    def test_detects_ticket_from_own_ancestor_lease_not_some_other_job(self):
        mine = self.root / "VSTA-166.json"
        mine.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname()}))
        other = self.root / "VSTA-99.json"
        other.write_text(json.dumps({"pid": 99999999, "host": socket.gethostname()}))
        with mock.patch.object(pp.glob, "glob", return_value=[str(other), str(mine)]):
            self.assertEqual(pp.lease_ticket(), "VSTA-166")

    def test_first_fail_climbs_even_after_later_pass_and_is_cached(self):
        ctx = self.context()
        pp.update_state(ctx["state_path"], ctx["job"], {"rung": "light", "exit_code": 0,
                        "pr_url": "https://github.com/a/b/pull/31"})
        comments = [[{"id": 3, "created_at": "2026-01-03", "body": "PASS\nVerifier model: strong"},
                     {"id": 1, "created_at": "2026-01-01", "body": "PASS\nOrdinary author comment"}],
                    [{"id": 2, "created_at": "2026-01-02", "body": "FAIL\nVerifier model: cheap"}]]
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(comments), ""))
        original = pp.first_verdict
        with mock.patch.object(pp, "first_verdict", side_effect=lambda pr, cache: original(pr, cache, run)):
            climbed = self.context()
            self.context()
        self.assertEqual(climbed["rung"], "medium")
        self.assertIn("rung light -> medium (prior FAIL on PR 31)", climbed["rung_reason"])
        self.assertEqual(run.call_count, 1)

    def test_finish_records_usage_and_counts_once_in_existing_pace_log(self):
        ctx = self.context()
        output = self.output([{"type": "message_end", "message": {"role": "assistant",
                              "usage": {"input": 1000000, "cacheRead": 1000000, "output": 1000000},
                              "content": [{"type": "text", "text": "https://github.com/a/b/pull/31"}]}}])
        runner = mock.Mock(return_value=(0, output))
        self.assertEqual(pp.run_lanes(self.lanes, {}, self.cfg, 1.1, rung=ctx, runner=runner,
                         limits_path=self.limits, launches_path=self.log, now=100, say=lambda _: None), 0)
        rows = [json.loads(line) for line in Path(self.log).read_text().splitlines()]
        start, finish = rows
        for key in ("rung", "rung_source", "lane", "model", "effort", "ticket"):
            self.assertIn(key, start)
        self.assertEqual(start["launch_id"], finish["launch_id"])
        self.assertEqual(finish["exit_code"], 0)
        self.assertEqual(finish["pr_url"], "https://github.com/a/b/pull/31")
        self.assertAlmostEqual(finish["cost_usd"], .753)
        self.assertEqual(finish["cost_basis"], "list-equivalent")
        self.assertEqual(pp.today_launches(self.log, "opencode", 100), 1)

    def output(self, records):
        return pp.measured_output(io.StringIO("\n".join(json.dumps(r) for r in records)), "tail")

    def test_stream_usage_sums_all_turns_not_just_tail_and_ignores_tools(self):
        output = self.output([{"kind": "turn_usage", "usage": {"input": 100, "in_tokens": 20, "cached": 80, "output": 9}},
                              {"kind": "tool_result", "output": "https://github.com/a/b/pull/99"},
                              {"kind": "turn_usage", "usage": {"input": 100, "in_tokens": 10, "cached": 90, "output": 5}},
                              {"type": "result", "text": "https://github.com/a/b/pull/31"}])
        self.assertEqual(output.tokens, {"in": 30, "cached": 170, "out": 14})
        self.assertEqual(output.pr_url, "https://github.com/a/b/pull/31")
        self.assertIsNone(self.output([{"kind": "tool_result", "output": "PR 99"}]).pr_url)

    def test_antigravity_multiline_json_and_missing_usage(self):
        record = {"response": "https://github.com/a/b/pull/31", "stats": {"models": {
            "gemini": {"tokens": {"input": 120, "cached": 100, "candidates": 5, "thoughts": 10}}}}}
        output = pp.measured_output(io.StringIO(json.dumps(record, indent=2)), "tail")
        self.assertEqual(output.tokens, {"in": 20, "cached": 100, "out": 15})
        self.assertIsNotNone(output.pr_url)
        self.assertIsNone(self.output([{"type": "result", "text": "done"}]).tokens)


class RungReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name, "launches.jsonl"))

    def report(self, rows, verdicts=None, last=50):
        Path(self.path).write_text("".join(json.dumps(r) + "\n" for r in rows))
        return pp.rung_report(self.path, last, lambda pr: (verdicts or {}).get(pr))

    def test_sensitivity_failure_costs_are_not_discarded(self):
        fixture = json.loads(Path(REPO, "tests/fixtures/rung-report.json").read_text())
        outputs = []
        for _ in range(5):
            for case in ("cheap_fails_twice", "both_pass"):
                report = self.report(fixture["launches"], fixture[case])
                outputs.append(report["cost_per_first_pass"])
                self.assertEqual(report["jobs"], 3)
                self.assertEqual(report["errors"], 0)
                self.assertEqual(report["unknowns"], 0)
        self.assertEqual(outputs[::2], [1.2] * 5)
        for value in outputs[1::2]:
            self.assertAlmostEqual(value, .4)

    def test_unknown_cost_is_not_free_and_missing_verdicts_are_unknown(self):
        fixture = json.loads(Path(REPO, "tests/fixtures/rung-report.json").read_text())
        rows = fixture["launches"]
        rows[-1]["cost_usd"] = None
        report = self.report(rows, fixture["both_pass"])
        self.assertIsNone(report["cost_per_first_pass"])
        self.assertEqual(report["unknowns"], 1)
        self.assertEqual(report["missing_cost"], 1)
        report = self.report(rows)
        self.assertEqual(report["unknowns"], 3)

    def test_last_finished_legacy_errors_and_hard_fail(self):
        fixture = json.loads(Path(REPO, "tests/fixtures/rung-report.json").read_text())
        rows = [{"lane": "old", "at": 1}, *fixture["launches"],
                {"event": "start", "launch_id": "incomplete", "at": 8},
                {"event": "finish", "launch_id": "orphan", "finished_at": 9}]
        rows[-3]["exit_code"] = 1
        report = self.report(rows, fixture["both_pass"], last=1)
        self.assertEqual(report["jobs"], 1)
        self.assertEqual(report["passes"], 0)
        self.assertEqual(report["hard_failed"], 1)
        self.assertEqual(report["errors"], 2)
        self.assertEqual(report["legacy_missing_finish"], 1)
        self.assertEqual(report["unfinished"], 1)
        text = "\n".join(pp.rung_report_lines(report))
        self.assertIn("jobs=1", text)
        self.assertIn("errors=2", text)
        self.assertIn("hard_failed=1", text)

    def test_gh_failure_is_not_a_pass_or_free_job(self):
        fixture = json.loads(Path(REPO, "tests/fixtures/rung-report.json").read_text())
        Path(self.path).write_text("".join(json.dumps(r) + "\n" for r in fixture["launches"]))
        with mock.patch.object(pp, "first_verdict", side_effect=ValueError("gh failed")):
            report = pp.rung_report(self.path)
        self.assertEqual((report["jobs"], report["passes"], report["unknowns"], report["errors"]), (3, 0, 3, 3))

    def test_cli_report_does_not_read_pools_or_credentials(self):
        env = {"HOME": self.tmp.name, "PATH": "/usr/bin:/bin", "PLAN_PICK_LAUNCHES": self.path}
        Path(self.path).write_text('{"at":1,"lane":"codex"}\n')
        run = subprocess.run([sys.executable, str(Path(REPO, "bin/plan-pick")), "--rung-report"],
                             env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("jobs=0 passes=0 unknowns=0 errors=0", run.stdout)
        self.assertIn("legacy_missing_finish=1", run.stdout)
