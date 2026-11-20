import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _load import REPO, load_bin


checkpoint = load_bin("tl-checkpoint")


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self.temp.name) / "worktree"
        self.repo.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), UNRELATED_SECRET="dummy")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Checkpoint Test")
        self.git("config", "user.email", "checkpoint@example.invalid")
        (self.repo / "tracked.txt").write_text("committed\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "fixture")
        self.command = [sys.executable, str(Path(REPO) / "bin/tl-checkpoint"), "--session", "RUNNER 11", "--role", "runner", "--harness", "hax", "--repo", str(self.repo), "--ticket", "https://app.hypertask.ai/detail/project-4060/790", "--next", "Open the focused change for review"]

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], env=self.env, text=True, stderr=subprocess.DEVNULL).strip()

    def run_checkpoint(self, *extra):
        return subprocess.run([*self.command, *extra], env=self.env, text=True, capture_output=True)

    def saved(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        path = Path(result.stdout.strip())
        return path, json.loads(path.read_text())

    def test_real_cli_captures_committed_workspace_and_handoff(self):
        path, record = self.saved(self.run_checkpoint("--pr", "https://github.com/valentinyeo/token-ladder/pull/21"))
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["session"], "RUNNER 11")
        self.assertEqual(record["role"], "runner")
        self.assertEqual(record["harness"], "hax")
        self.assertEqual(record["ticket"], "https://app.hypertask.ai/detail/project-4060/790")
        self.assertEqual(record["pullRequest"], "https://github.com/valentinyeo/token-ladder/pull/21")
        self.assertEqual(record["next"], "Open the focused change for review")
        self.assertEqual(record["workspace"], {"path": str(self.repo), "head": self.git("rev-parse", "HEAD"), "branch": "main", "dirty": []})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(path.parent.iterdir()), [path])
        self.assertNotIn(self.env["UNRELATED_SECRET"], path.read_text())

    def test_dirty_files_are_retained_without_copying_their_contents(self):
        (self.repo / "tracked.txt").write_text("unfinished change\n")
        (self.repo / ".env").write_text("PRIVATE_VALUE=private-file-content\n")
        before = self.git("status", "--porcelain=v1")
        path, record = self.saved(self.run_checkpoint())
        self.assertEqual(record["workspace"]["dirty"], [" M tracked.txt", "?? .env"])
        self.assertEqual(self.git("status", "--porcelain=v1"), before)
        self.assertEqual((self.repo / "tracked.txt").read_text(), "unfinished change\n")
        self.assertNotIn("private-file-content", path.read_text())
        self.assertIsNone(record["pullRequest"])

    def test_detached_head_is_saved_without_inventing_a_branch(self):
        self.git("checkout", "--detach")
        _, record = self.saved(self.run_checkpoint())
        self.assertIsNone(record["workspace"]["branch"])
        self.assertEqual(record["workspace"]["head"], self.git("rev-parse", "HEAD"))

    def test_invalid_inputs_do_not_change_an_existing_checkpoint(self):
        path, _ = self.saved(self.run_checkpoint())
        before = path.read_bytes()
        for args in [("--session", " "), ("--next", ""), ("--ticket", "https://example.invalid/790"), ("--pr", "https://example.invalid/pull/1"), ("--repo", str(self.home)), ("--harness", "deepseek-fast")]:
            with self.subTest(args=args):
                result = self.run_checkpoint(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_unborn_repository_is_refused_without_publishing_a_record(self):
        empty = self.repo / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", str(empty)], env=self.env, check=True, capture_output=True)
        result = self.run_checkpoint("--repo", str(empty))
        self.assertEqual(result.returncode, 2)
        self.assertIn("committed revision", result.stderr)
        self.assertFalse((self.home / ".local/state/token-ladder/checkpoints").exists())

    def test_concurrent_checkpoints_do_not_replace_each_other(self):
        first = subprocess.Popen(self.command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(self.command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        paths = []
        for process in (first, second):
            output, error = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, error)
            paths.append(Path(output.strip()))
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(len(list(paths[0].parent.iterdir())), 2)
        for path in paths:
            self.assertEqual(json.loads(path.read_text())["session"], "RUNNER 11")

    def test_publication_failure_keeps_prior_records_and_removes_temporary_files(self):
        path, _ = self.saved(self.run_checkpoint())
        before = path.read_bytes()
        args = type("Args", (), dict(session="RUNNER 11", role="runner", harness="hax", repo=str(self.repo), ticket="https://app.hypertask.ai/detail/project-4060/790", pr=None, next="Continue"))()
        with mock.patch.dict(os.environ, self.env), mock.patch.object(checkpoint.os, "link", side_effect=OSError("publication refused")):
            with self.assertRaisesRegex(OSError, "publication refused"):
                checkpoint.save_checkpoint(args)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_checkpoint_does_not_change_other_handover_files(self):
        handover = self.home / "handover"
        handover.mkdir()
        lease = handover / "manager.md"
        lease.write_text("Next 3 actions\n")
        before = lease.read_bytes()
        _, record = self.saved(self.run_checkpoint("--role", "manager", "--harness", "hax"))
        self.assertEqual(record["role"], "manager")
        self.assertEqual(lease.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
