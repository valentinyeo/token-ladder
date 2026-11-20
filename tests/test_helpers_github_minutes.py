import unittest

from _load import tl


class HelpersGithubMinutesTest(unittest.TestCase):
    def test_orchestrator_line_bans_ci_on_the_vps(self):
        line = tl.HELPERS["orchestrator"]
        self.assertIn("No CI, builds, tests or AI review on this VPS", line)
        self.assertIn("only on GitHub-hosted runners", line)
        self.assertNotIn("GitHub runs those", line)


if __name__ == "__main__":
    unittest.main()
