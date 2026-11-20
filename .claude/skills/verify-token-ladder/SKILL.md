---
name: verify-token-ladder
description: Verify Token Ladder CLI and CI (plan-pick, pools, ladder-call). Use after changes to bin/, ci/, or config.json. Fresh clone only; never touch the live home state or spend credit on real lanes.
---

# Verify Token Ladder

Surface: Python CLI (`bin/token-ladder`, `bin/plan-pick`) and CI helpers (`ci/ladder-call`, `ci/lint-direct-model-calls`). Read `features/README.md` before driving. The live copy under the operator home drives real runners: never run `collect`, `mode`, or `grok-set` there.

## Launch

No server. From the repo root:

```
python3 -m py_compile bin/token-ladder bin/plan-pick bin/check-tests-changed tests/*.py ci/ladder-call ci/lint-direct-model-calls ci/ladder-call-stub-server.py ci/run-verifier ci/verdict.py ci/build-verifier-request.py ci/read-verifier-answer.py ci/regression-selftest.py && echo COMPILE_OK
```

## Doctor

```
python3 --version; test -x bin/token-ladder && test -x bin/plan-pick && test -f config.json && echo LAYOUT_OK
```

## Drive

Use `HOME=$(mktemp -d)` and `TOKEN_LADDER_LOCAL_CONFIG` pointing at `.test-missing-config.local.json` (same as unit tests) so pools and state stay isolated.

Pick one journey from `features/` and run its commands literally. For a PR-sized check also run:

```
python3 -m unittest discover -s tests -p 'test_*.py' 2>&1 | tail -5
node tools/strip-comments.mjs --check
python3 ci/lint-direct-model-calls .
```

## Evidence

Capture exit codes plus the stdout/stderr lines named in the feature file. Paste them in the ticket or PR; do not commit proof under the repo. Compile-only checks are not proof.

## Cleanup

`rm -rf` only the `mktemp` homes you created. Evidence stays in the ticket or PR comment.

## Helpers

Stub HTTP for ladder-call journeys:

```
port=$(python3 ci/ladder-call-stub-server.py & echo $! > /tmp/tl-stub.pid; sleep 0.2; head -1 /proc/$(cat /tmp/tl-stub.pid)/fd/1 2>/dev/null || true)
```

Prefer the ladder-actions workflow recipe: start `ci/ladder-call-stub-server.py`, point `LADDER_CALL_URL_OPENCODE_GO` at `http://127.0.0.1:$PORT/zen/go/v1/chat/completions`, pass stub keys, echo a minimal request JSON into `python3 ci/ladder-call --purpose verify-skill`.