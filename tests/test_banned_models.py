import ast
import json
import re
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config.json"
LANES = REPO / "ci" / "lanes.json"
BANNED = re.compile(r"baseten|deepseek[\w./-]*-pro", re.I)
MODEL_ID = re.compile(r"(?:--model(?:-id)?|[\"']model[\"']\s*:|-m)\s*[=:]?\s*[\"']?([\w./:@-]+)", re.I)
ROUTE_PIN = re.compile(r"[\"']only[\"']\s*:\s*\[([^\]]*)\]", re.I)
SKIP = ("docs/research/",)


def tracked_files():
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True)
    for name in listed.stdout.decode("utf-8").split("\0"):
        if not name or name.startswith(SKIP):
            continue
        try:
            yield name, (REPO / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


def refusal_spans(name, text):
    if not name.endswith(".py"):
        return ()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        called = {child.func.attr if isinstance(child.func, ast.Attribute) else getattr(child.func, "id", "")
                  for child in ast.walk(node) if isinstance(child, ast.Call)}
        if "refus" in node.name or "banned" in node.name or any("refusal" in c for c in called):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans


def texts():
    got = []
    for path in (CONFIG, LANES):
        got.append(path.read_text(encoding="utf-8"))
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for lane in cfg.get("lanes") or []:
        command = lane.get("command")
        if isinstance(command, str):
            got.append(command)
    return got


LANE_ORDER = [
    "opencode-go",
    "vercel-ai-gateway",
    "openrouter",
    "google",
    "cursor",
]


class BannedModelTests(unittest.TestCase):
    def test_no_tracked_file_launches_a_banned_model_id(self):
        scanned = 0
        for name, text in tracked_files():
            scanned += 1
            spans = refusal_spans(name, text)
            for match in MODEL_ID.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                if any(start <= line <= end for start, end in spans):
                    continue
                with self.subTest(file=name, line=match.group(0)):
                    self.assertIsNone(BANNED.search(match.group(1)), f"{name}: {match.group(0)}")
        self.assertGreater(scanned, 30)

    def test_no_tracked_file_pins_a_baseten_route(self):
        for name, text in tracked_files():
            for match in ROUTE_PIN.finditer(text):
                with self.subTest(file=name, line=match.group(0)):
                    self.assertNotIn("baseten", match.group(1).lower(), f"{name}: {match.group(0)}")

    def test_shipped_lane_commands_carry_no_banned_model_id(self):
        for index, blob in enumerate(texts()):
            with self.subTest(index=index):
                self.assertIsNone(BANNED.search(blob), blob[:200])

    def test_ci_lane_order_keeps_cheapest_first(self):
        lanes = json.loads(LANES.read_text(encoding="utf-8"))
        self.assertEqual([lane["name"] for lane in lanes], LANE_ORDER)

    def test_cursor_lane_never_uses_deepseek(self):
        lanes = json.loads(LANES.read_text(encoding="utf-8"))
        cursor = next(lane for lane in lanes if lane["name"] == "cursor")
        self.assertNotIn("deepseek", cursor["model"].lower())
