import os
import unittest

WF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows")


class CiRunnerTest(unittest.TestCase):
    def test_every_workflow_job_runs_on_a_github_hosted_runner(self):
        names = sorted(n for n in os.listdir(WF) if n.endswith(".yml"))
        self.assertIn("tests.yml", names)
        for name in names:
            with open(os.path.join(WF, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("runs-on: ubuntu-latest", text, name)
            self.assertNotIn("self-hosted", text, name)


if __name__ == "__main__":
    unittest.main()
