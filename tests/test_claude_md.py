from pathlib import Path
import unittest

REPO = Path(__file__).resolve().parents[1]


class ClaudeMdTests(unittest.TestCase):
    def setUp(self):
        self.lines = (REPO / "CLAUDE.md").read_text().splitlines()
        self.workflow = (REPO / ".github/workflows/tests.yml").read_text().splitlines()

    def command_line(self, name):
        lines = [line for line in self.lines if line.startswith(f"- {name}: ")]
        self.assertEqual(len(lines), 1, f"CLAUDE.md must have one - {name}: line")
        return lines[0]

    def ci_command(self, name):
        step = None
        for line in self.workflow:
            line = line.strip()
            if line.startswith("- "):
                step = line.removeprefix("- name: ")
            elif step == name and line.startswith("run: "):
                return line.removeprefix("run: ")
        self.fail(f"tests.yml must have a run command for {name}")

    def test_under_80_lines(self):
        self.assertLess(len(self.lines), 80)

    def test_command_lines_exist(self):
        for name in ("Build", "Test", "Ship"):
            with self.subTest(name=name):
                self.command_line(name)

    def test_build_matches_ci(self):
        self.assertEqual(self.command_line("Build").split("`")[1], self.ci_command("Compile"))

    def test_test_matches_ci(self):
        self.assertEqual(self.command_line("Test").split("`")[1], self.ci_command("Run tests").removesuffix(" -v"))

    def test_ship_has_required_checks_and_deployment(self):
        line = self.command_line("Ship")
        for required in ("secret-scan", "--squash", "pull --ff-only"):
            with self.subTest(required=required):
                self.assertIn(required, line)


if __name__ == "__main__":
    unittest.main()
