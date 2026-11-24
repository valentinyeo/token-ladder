#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verdict import answer_text, find_verdict


def main():
    if len(sys.argv) != 3:
        print("usage: read-verifier-answer.py ANSWER.json OUT.md", file=sys.stderr)
        raise SystemExit(2)
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    text = answer_text(payload)
    if not text:
        print("read-verifier-answer: empty assistant content", file=sys.stderr)
        raise SystemExit(1)
    verdict, why = find_verdict(text)
    if not verdict:
        print(f"read-verifier-answer: {why}", file=sys.stderr)
        raise SystemExit(1)
    Path(sys.argv[2]).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
