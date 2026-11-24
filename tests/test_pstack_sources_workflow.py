import os
import unittest

from _load import REPO

WORKFLOW = os.path.join(REPO, ".github", "workflows", "pstack-sources.yml")


class PstackSourcesWorkflowTests(unittest.TestCase):
    def setUp(self):
        with open(WORKFLOW) as workflow:
            self.text = workflow.read()

    def test_verifier_job_matches_required_check(self):
        self.assertIn("\n  verifier:\n", self.text,
                      "the job id stays verifier; the gateway caller tag reads it")
        self.assertIn("\n    name: pstack-verifier\n", self.text,
                      "branch protection requires the check named pstack-verifier; keep the job name")

    def test_regression_gates_verifier(self):
        self.assertIn("\n  verifier:\n    name: pstack-verifier\n    needs: regression\n", self.text)


if __name__ == "__main__":
    unittest.main()
