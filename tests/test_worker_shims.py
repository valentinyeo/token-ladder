import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _load import REPO

SHIMS = str(Path(REPO).resolve() / "bin" / "worker-shims")


def fake(folder, name):
    path = Path(folder, name)
    path.write_text(f"#!/bin/sh\nprintf 'real {name}: %s\\n' \"$*\"\n")
    path.chmod(0o755)


class WorkerShimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.real = Path(self.tmp.name, "real")
        self.real.mkdir()
        for name in ("gh", "sleep"):
            fake(self.real, name)
        self.env = {"HOME": self.tmp.name,
                    "PATH": os.pathsep.join([SHIMS, str(self.real), "/usr/bin:/bin"])}

    def run_shim(self, name, *args):
        return subprocess.run([os.path.join(SHIMS, name), *args],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def test_sleep_refuses_anything_over_thirty_seconds(self):
        for args in (("240",), ("31",), ("1m",), ("2m",), ("1h",)):
            with self.subTest(args=args):
                out = self.run_shim("sleep", *args)
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertEqual(out.stdout, "")
                lines = out.stderr.strip().splitlines()
                self.assertEqual(len(lines), 1, out.stderr)
                self.assertEqual(lines[0], f"worker shim refused: sleep {' '.join(args)} "
                                           "(over 30s; workers push and exit, the parent waits)")

    def test_sleep_runs_the_real_program_up_to_thirty_seconds(self):
        for args in (("30",), ("5",), ("0.5",), ("20s",)):
            with self.subTest(args=args):
                out = self.run_shim("sleep", *args)
                self.assertEqual((out.returncode, out.stdout),
                                 (0, f"real sleep: {' '.join(args)}\n"), out.stderr)

    def test_gh_refuses_the_watching_forms(self):
        for args in (("pr", "checks", "--watch"), ("pr", "checks", "-w"), ("run", "watch")):
            with self.subTest(args=args):
                out = self.run_shim("gh", *args)
                self.assertEqual(out.returncode, 3, out.stderr)
                self.assertEqual(out.stdout, "")
                lines = out.stderr.strip().splitlines()
                self.assertEqual(len(lines), 1, out.stderr)
                self.assertTrue(lines[0].startswith(f"worker shim refused: gh {' '.join(args)} ("), lines[0])
                self.assertIn("workers push and exit, the parent waits", lines[0])

    def test_gh_passes_every_other_call_to_the_real_binary(self):
        for args in (("pr", "checks"), ("pr", "checks", "--json", "state"),
                     ("run", "view", "--log-failed"), ("pr", "create", "--fill")):
            with self.subTest(args=args):
                out = self.run_shim("gh", *args)
                self.assertEqual((out.returncode, out.stdout),
                                 (0, f"real gh: {' '.join(args)}\n"), out.stderr)


if __name__ == "__main__":
    unittest.main()
