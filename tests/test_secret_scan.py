import os
import re
import unittest

from _load import REPO

WORKFLOW = os.path.join(REPO, ".github", "workflows", "gitleaks.yml")


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.text = open(WORKFLOW).read()

    def test_runs_on_every_change_and_weekly(self):
        on = self.text.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        for trigger in ("push", "pull_request", "schedule", "workflow_dispatch"):
            self.assertRegex(on, rf"\n  {trigger}:", f"gitleaks.yml must run on {trigger}")
        self.assertRegex(on, r'cron: "[0-9]+ [0-9]+ \* \* [0-6]"', "the full rescan must run once a week")

    def test_job_name_matches_required_check(self):
        self.assertIn("\n    name: secret-scan\n", self.text,
                      "branch protection requires the check named secret-scan; keep the job name")

    def test_weekly_run_scans_full_history(self):
        self.assertIn('--log-opts "--all"', self.text)
        self.assertTrue(re.search(r'trufflehog git "file://\$PWD" \$\{SCAN_HEAD:\+', self.text),
                        "trufflehog must scan every branch when no head commit is set")


if __name__ == "__main__":
    unittest.main()
