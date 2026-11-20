#!/usr/bin/env python3
"""Extract assistant text from a ladder-call answer file."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verdict import find_verdict


def message_content(result):
    try:
        choices = result.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text") or "")
            return "".join(parts).strip()
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""
    return ""


def main():
    if len(sys.argv) != 3:
        print("usage: read-verifier-answer.py ANSWER.json OUT.md", file=sys.stderr)
        raise SystemExit(2)
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    text = message_content(payload)
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