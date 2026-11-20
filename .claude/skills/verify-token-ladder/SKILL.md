---
name: verify-token-ladder
description: Verify Token Ladder (Python CLI that reads AI subscription usage). Use after any change to bin/token-ladder or config.json to prove it parses, passes tests and prints sane pool states, all read-only.
---

# Verify Token Ladder

Surface: CLI, Python 3 standard library only. Read `CLAUDE.md` first. The live copy under the primary home drives real runners and timers: never run this skill there, never run `collect`, `mode` or `grok-set` (they write state). Work in a fresh clone.

## Launch

No server. Nothing to start or tear down. From the clone root:

```
python3 -m py_compile bin/token-ladder bin/check-tests-changed tests/*.py && echo COMPILE_OK
```

## Doctor

Run first. Wrong directory or Python means stop.

```
python3 --version; test -x bin/token-ladder && test -f config.json && echo LAYOUT_OK
```

## Drive

1. Tests (they set an empty `HOME` themselves, so they never read real state):
   `python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -5`
   Pass means the last line is `ALL TESTS PASSED`.
2. Diff gate (a `bin/` change needs a test change): `bin/check-tests-changed origin/main`.
3. Read-only status against an empty home, so no real state is touched:
   `HOME=$(mktemp -d) bin/token-ladder status`
   Expect `TOKEN MAX OK`, `FLOOR OK`, `FEED OK`, then one line each for Claude, Codex and Grok. With no state they read `? used, reset unknown`; that is correct.
4. Other read-only commands that are safe the same way: `pools --json`, `hook-prompt` and `route --repo X` (all with `HOME=$(mktemp -d)`).

Comment-free tree is a required CI check: `node tools/strip-comments.mjs --check`.

## Evidence

Paste the real output of steps 1 and 3 (last lines only) plus the exit codes. A change to hook output (`hook-prompt`, `hook-bash`, `hook-agent`) also needs the before and after text from the same command. Compile alone is not proof. Do not use the live `~/.local/state/token-ladder/` as evidence; it is shared.

## Cleanup

`rm -rf` only the `mktemp -d` dirs you made. `__pycache__` is gitignored. Evidence is your pasted output; keep it in the ticket or PR, not in the repo.

## Ship

Per `CLAUDE.md`: PR from a `ceo/<ticket>` branch, required checks `secret-scan`, `tests`, `no-comments` green, squash merge. Never add keys, private hosts or emails to any file.
