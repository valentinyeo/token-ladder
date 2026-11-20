import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _load import tl


class CloudRouteTests(unittest.TestCase):
    def setUp(self):
        self.cloud = {"codex": {"valentinyeo/token-ladder": "env1"}, "claude": {}}

    def test_cloud_destinations(self):
        self.assertEqual(tl.cloud_destination("valentinyeo/token-ladder", "codex", self.cloud), "codex-cloud")
        self.assertEqual(tl.cloud_destination("hypertask-ai/hypertask", "codex", self.cloud), "local")
        self.assertEqual(tl.cloud_destination("valentinyeo/token-ladder", "claude", self.cloud), "local")
        cloud = {"codex": {}, "claude": {"acme/app": "yes"}}
        self.assertEqual(tl.cloud_destination("acme/app", "claude", cloud), "claude-cloud")
        self.assertEqual(tl.cloud_destination("acme/app", "codex", None), "local")

    def test_route_commands(self):
        self.assertEqual(tl.route_line("hypertask-ai/hypertask", "codex", self.cloud)[1], tl.LOCAL_CODEX)
        self.assertEqual(tl.route_line("valentinyeo/token-ladder", "codex", self.cloud)[1],
                         "codex cloud exec --env env1")
        self.assertEqual(tl.route_line("acme/app", "claude", self.cloud)[1], "Agent tool in this session")
        cloud = {"codex": {}, "claude": {"acme/app": "yes"}}
        self.assertEqual(tl.route_line("acme/app", "claude", cloud)[1], "claude-cloud-task")

    def test_missing_env_id_stays_local(self):
        maps = tl.cloud_maps({"cloud": {"codex": ["valentinyeo/token-ladder"], "claude": []}}, {})
        self.assertEqual(maps, {"codex": {}, "claude": {}})
        self.assertEqual(tl.cloud_destination("valentinyeo/token-ladder", "codex", maps), "local")
        stored = {"codex": {"valentinyeo/token-ladder": "env1"}}
        maps = tl.cloud_maps({"cloud": {"codex": ["valentinyeo/token-ladder"], "claude": ["acme/app"]}}, stored)
        self.assertEqual(maps["codex"], {"valentinyeo/token-ladder": "env1"})
        self.assertEqual(maps["claude"], {})

    def test_mode_decision_is_unchanged(self):
        now = tl.iso_to_ts("2026-10-10T09:30:00+00:00")

        def bars(used, hours_left, minutes=10080):
            return tl.window(used, now + hours_left * 3600, minutes, now)

        claude = {"week": bars(99, 31), "five_hour": bars(1, 3, 300)}
        codex = {"week": bars(100, 50), "five_hour": tl.window(None, None, 300, now)}
        self.assertEqual(tl.decide(claude, codex, tl.load_config(), now)[0], "codex")


if __name__ == "__main__":
    unittest.main()
