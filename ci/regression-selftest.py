#!/usr/bin/env python3
import py_compile
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULES = (
    "run-verifier",
    "verdict.py",
    "build-verifier-request.py",
    "read-verifier-answer.py",
)


def main():
    for name in MODULES:
        path = HERE / name
        py_compile.compile(path, doraise=True)
    checks = (
        [sys.executable, str(HERE / "verdict.py"), "--self-test"],
        [sys.executable, str(HERE / "run-verifier"), "--self-test"],
    )
    for command in checks:
        subprocess.run(command, check=True)


if __name__ == "__main__":
    main()