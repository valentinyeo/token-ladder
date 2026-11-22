import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERDICT = REPO / "ci" / "verdict.py"
READER = REPO / "ci" / "read-verifier-answer.py"
BUILDER = REPO / "ci" / "build-verifier-request.py"

sys.path.insert(0, str(REPO / "ci"))
from verdict import answer_text, find_verdict

REAL_ANSWERS = (
    ("**PASS**\n\nTests are green and the diff matches the feature map.", "PASS"),
    ("## FAIL\n\nThe verifier cached a lane list and never refreshed it.", "FAIL"),
    ("Verdict: PASS+NOTES", "PASS+NOTES"),
    ("### Verdict: FAIL\n\nJob 44795563030 never reached a verdict line.", "FAIL"),
    ("  PASS  ", "PASS"),
    ("**Verdict:** FAIL", "FAIL"),
    ("PASS\n\nNotes: the ladder skip reason now names the missing verdict.", "PASS"),
    ("```\nPASS\n```", None),
    ("I'll start by inspecting the repo state and the supplied merge base.\n\n"
     "**Shell**\n```\npwd; git status --short; git log --oneline -3\n```", None),
    ("**Shell**\n```\npython3 -m unittest discover -s tests -p 'test_*.py' -q 2>&1 | tail -5\n```", None),
    ("", None),
)


class AnswerTextTests(unittest.TestCase):
    def test_content_wins_over_reasoning(self):
        payload = {"choices": [{"message": {"content": "PASS", "reasoning": "thinking"}}]}
        self.assertEqual(answer_text(payload), "PASS")

    def test_reasoning_answers_when_content_empty(self):
        payload = {"choices": [{"message": {
            "content": "",
            "reasoning": "The diff is small.\n\nPASS\n",
            "tool_calls": [{"type": "function", "function": {"name": "bash"}}],
        }}]}
        self.assertEqual(answer_text(payload), "The diff is small.\n\nPASS")

    def test_content_parts_array(self):
        payload = {"choices": [{"message": {"content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]}}]}
        self.assertEqual(answer_text(payload), "hello world")

    def test_no_choices(self):
        self.assertEqual(answer_text({}), "")
        self.assertEqual(answer_text({"choices": []}), "")


class VerdictParserTests(unittest.TestCase):
    def test_real_answers(self):
        for text, want in REAL_ANSWERS:
            with self.subTest(text=text[:40]):
                got, why = find_verdict(text)
                self.assertEqual(got, want)
                if want is None:
                    self.assertIn("no verdict found", why)

    def test_last_verdict_wins(self):
        self.assertEqual(find_verdict("FAIL\n\nThen rerun gave:\n\n**PASS**\n")[0], "PASS")

    def test_json_verdict_still_reads(self):
        self.assertEqual(find_verdict('{"verdict": "PASS+NOTES"}')[0], "PASS+NOTES")

    def test_prefixed_verdict_inside_sentence_is_not_an_answer(self):
        text = "The note said Verdict: FAIL inside a quoted review.\n\nPASS\n"
        self.assertEqual(find_verdict(text)[0], "PASS")


class ReadVerifierAnswerTests(unittest.TestCase):
    def run_reader(self, payload):
        with tempfile.TemporaryDirectory() as scratch:
            answer = Path(scratch) / "answer.json"
            out = Path(scratch) / "answer.md"
            answer.write_text(json.dumps(payload), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(READER), str(answer), str(out)],
                capture_output=True,
                text=True,
                check=False,
            )
            return proc, out.read_text(encoding="utf-8") if out.exists() else ""

    def test_reasoning_only_pass_is_read(self):
        proc, text = self.run_reader({"choices": [{"message": {
            "content": "",
            "reasoning": "All checks passed.\n\nPASS\n",
        }}]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(text.startswith("All checks passed."))

    def test_agent_style_answer_fails_clearly(self):
        proc, text = self.run_reader({"choices": [{"message": {
            "content": "I'll start by inspecting the repo state.\n\n**Shell**\n```\npwd\n```",
        }}]})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no verdict found", proc.stderr)
        self.assertEqual(text, "")


class VerifierRequestTests(unittest.TestCase):
    def test_request_demands_verdict_first_line(self):
        with tempfile.TemporaryDirectory() as scratch:
            prompt = Path(scratch) / "prompt.md"
            request = Path(scratch) / "request.json"
            prompt.write_text("Check the diff.\n", encoding="utf-8")
            subprocess.run(
                [sys.executable, str(BUILDER), str(prompt), str(request)],
                check=True,
            )
            payload = json.loads(request.read_text(encoding="utf-8"))
        system = payload["messages"][0]["content"]
        self.assertIn("first line must be exactly PASS, PASS+NOTES, or FAIL", system)
        self.assertIn("no markdown", system)
        self.assertGreaterEqual(payload["max_tokens"], 8192)


if __name__ == "__main__":
    unittest.main()
