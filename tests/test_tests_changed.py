import os
import subprocess
import tempfile
import unittest

from _load import load_bin

tc = load_bin("check-tests-changed")


def scratch_repo():
    path = tempfile.mkdtemp()
    git = lambda *a: subprocess.run(["git", "-C", path, "-c", "user.name=T", "-c", "user.email=1+t@users.noreply.github.com",
                                     "-c", "core.hooksPath=/dev/null", *a], check=True, capture_output=True)
    git("init", "-q")
    for name in ("bin/gateway_burn.py", "tests/test_gateway_burn.py"):
        os.makedirs(os.path.dirname(os.path.join(path, name)), exist_ok=True)
        open(os.path.join(path, name), "w").write("v0\n")
    git("add", "."); git("commit", "-qm", "base")
    open(os.path.join(path, "bin/gateway_burn.py"), "a").write("v1\n")
    git("commit", "-qam", "untested")
    open(os.path.join(path, "bin/gateway_burn.py"), "a").write("v2\n")
    open(os.path.join(path, "tests/test_gateway_burn.py"), "a").write("v2\n")
    git("commit", "-qam", "tested")
    return path


class TestsChangedCheck(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)

    def test_rule(self):
        self.assertEqual(tc.untested(["bin/gateway_burn.py", "README.md"]), ["bin/gateway_burn.py"])
        self.assertEqual(tc.untested(["bin/gateway_burn.py", "tests/test_gateway_burn.py"]), [])
        self.assertEqual(tc.untested(["README.md", "skill/SKILL.md"]), [])

    def test_untested_code_change_fails(self):
        os.chdir(scratch_repo())
        self.assertEqual(tc.untested(tc.changed_files("HEAD~2", "HEAD~1")), ["bin/gateway_burn.py"])

    def test_tested_code_change_passes(self):
        os.chdir(scratch_repo())
        self.assertEqual(tc.main(["x", "HEAD~1", "HEAD"]), 0)


if __name__ == "__main__":
    unittest.main()
