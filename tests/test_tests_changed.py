import io
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from _load import REPO, load_bin, tl

tc = load_bin("check-tests-changed")


def scratch_repo():
    path = tempfile.mkdtemp()
    git = lambda *a: subprocess.run(["git", "-C", path, "-c", "user.name=T", "-c", "user.email=1+t@users.noreply.github.com",
                                     "-c", "core.hooksPath=/dev/null", *a], check=True, capture_output=True)
    git("init", "-q")
    for name in ("bin/token-ladder", "tests/test_ladder.py"):
        os.makedirs(os.path.dirname(os.path.join(path, name)), exist_ok=True)
        open(os.path.join(path, name), "w").write("v0\n")
    git("add", "."); git("commit", "-qm", "base")
    open(os.path.join(path, "bin/token-ladder"), "a").write("v1\n")
    git("commit", "-qam", "untested")
    open(os.path.join(path, "bin/token-ladder"), "a").write("v2\n")
    open(os.path.join(path, "tests/test_ladder.py"), "a").write("v2\n")
    git("commit", "-qam", "tested")
    return path


class TestsChangedCheck(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)

    def test_rule(self):
        self.assertEqual(tc.untested(["bin/token-ladder", "README.md"]), ["bin/token-ladder"])
        self.assertEqual(tc.untested(["bin/token-ladder", "tests/test_ladder.py"]), [])
        self.assertEqual(tc.untested(["README.md", "skill/SKILL.md"]), [])

    def test_untested_code_change_fails(self):
        os.chdir(scratch_repo())
        self.assertEqual(tc.untested(tc.changed_files("HEAD~2", "HEAD~1")), ["bin/token-ladder"])

    def test_tested_code_change_passes(self):
        os.chdir(scratch_repo())
        self.assertEqual(tc.main(["x", "HEAD~1", "HEAD"]), 0)


STATUS = {"ts": tl.time.time(), "mode": "claude", "why": "Claude has room", "advice": [],
          "claude": {"week": tl.window(10, None, 10080, 0)}, "codex": {"week": tl.window(10, None, 10080, 0)}}


class PastHookBugs(unittest.TestCase):
    def test_9ead927_mode_line_without_config(self):
        self.assertIn("Token ladder: Claude", tl.mode_line(STATUS))

    def hook_denies(self, command):
        stdin = io.StringIO('{"tool_input": {"command": %s}}' % tl.json.dumps(command))
        with patch.object(tl, "load_status", return_value=dict(STATUS)), patch("sys.stdin", stdin), \
                patch("sys.stdout", io.StringIO()):
            try:
                tl.hook_bash()
            except SystemExit:
                return True
        return False

    def test_cf59be2_hax_help_passes_in_claude_mode(self):
        self.assertFalse(self.hook_denies("hax --provider=codex -p hi"))
        self.assertFalse(self.hook_denies("hax --help"))
        self.assertFalse(self.hook_denies("hax --version"))


if __name__ == "__main__":
    unittest.main()
