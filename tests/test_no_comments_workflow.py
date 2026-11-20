import os
import unittest

from _load import REPO

WORKFLOW = os.path.join(REPO, ".github", "workflows", "no-comments.yml")


class NoCommentsWorkflowTests(unittest.TestCase):
    def setUp(self):
        with open(WORKFLOW) as workflow:
            self.text = workflow.read()

    def test_job_name_matches_required_check(self):
        self.assertIn("\n  no-comments:\n", self.text)
        self.assertIn("\n    name: no-comments\n", self.text,
                      "branch protection requires the check named no-comments; keep the job name")

    def test_checks_whole_tree(self):
        self.assertIn("\n      - run: node tools/strip-comments.mjs --check\n", self.text)


if __name__ == "__main__":
    unittest.main()
