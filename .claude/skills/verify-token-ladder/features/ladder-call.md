# ladder-call and lint

CI chat completions go through `ci/ladder-call`; workflows must not call model APIs directly. `ci/lint-direct-model-calls` enforces that.

## Sub-features

- `ladder-stub` answers through the local stub server on the opencode-go path.
- `answer-text` reads `message.content`, and falls back to `message.reasoning` when a reasoning model returns empty content.
- `require-verdict` skips a lane whose answer holds no PASS, PASS+NOTES or FAIL verdict, then tries the next lane.
- `lint-clean` reports no violations on this repository tree.

## How to get to it (user POV)

- Maintainers add GitHub Actions jobs that need a hosted model and wire them through `.github/actions/ladder-call` or `ci/ladder-call`.

## Driving it with bash

Preconditions:

- Stub keys only: `AI_GATEWAY_API_KEY=stub-gw` and `OPENCODE_API_KEY=stub-oc`.

- **Stub server.** Run `python3 ci/ladder-call-stub-server.py > /tmp/tl-stub-port & stub_pid=$!; sleep 0.3; port=$(cat /tmp/tl-stub-port); export LADDER_CALL_URL_OPENCODE_GO=http://127.0.0.1:${port}/zen/go/v1/chat/completions LADDER_CALL_URL_VERCEL_AI_GATEWAY=http://127.0.0.1:${port}/v1/chat/completions`.
- **ladder-call.** Run `printf '%s\n' '{"messages":[{"role":"user","content":"hi"}]}' | python3 ci/ladder-call --purpose verify-skill`. Exit `0`. stderr ends with `ladder-call: lane opencode-go answered (verify-skill)`. stdout JSON `choices[0].message.content` is `action-ok`.
- **ladder-call verdicts.** Run `printf '%s\n' '{"messages":[{"role":"user","content":"hi"}]}' | python3 ci/ladder-call --purpose pr-verifier --require-verdict` against the stub. Exit `3`, stderr holds `lane opencode-go skipped: no verdict` then `ladder-call: no lane answered`, because `action-ok` is not a verdict.
- **Lint.** Run `python3 ci/lint-direct-model-calls .`. Exit `0`. stdout empty.

Cleanup: `kill "$stub_pid" 2>/dev/null || true`.

## Gotchas

- Without stub URLs, ladder-call hits real providers and may spend quota.
- A reasoning model can return empty `content` and put its plan in `reasoning` plus `tool_calls`; ladder-call answers from reasoning, and `--require-verdict` moves on when that plan holds no verdict.
- lint flags direct `api.deepseek.com` and similar; fix violations with `ladder-call`, not allow-list entries, unless emergency-documented.