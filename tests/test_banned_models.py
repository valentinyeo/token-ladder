import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config.json"
LANES = REPO / "ci" / "lanes.json"
BANNED = re.compile(r"baseten|deepseek[\w./-]*-pro|deepseek-v4-pro", re.I)
SCAN_PATHS = [CONFIG, LANES]


def texts():
    got = []
    for path in SCAN_PATHS:
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
    "deepseek",
    "google",
    "cursor",
]


class BannedModelTests(unittest.TestCase):
    def test_no_baseten_or_deepseek_pro_model_ids(self):
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