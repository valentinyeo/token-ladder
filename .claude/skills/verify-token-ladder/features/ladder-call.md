# ladder-call and lint

CI chat completions go through `ci/ladder-call`; workflows must not call model APIs directly. `ci/lint-direct-model-calls` enforces that.

## Sub-features

- `ladder-stub` answers through the local stub server on the opencode-go path.
- `answer-text` reads `message.content`, and falls back to `message.reasoning` when a reasoning model returns empty content.
- `require-verdict` asks a lane once more for a verdict from the tail of its own answer when the answer holds none, then skips to the next lane.
- `exclude-families` skips every lane whose model belongs to a comma-separated excluded family, including models routed through other providers. It fails when all lanes are excluded or a lane's family is unknown. The gateway runs a DeepSeek lane and a `zai/glm-5.3-flash` judge lane, so a DeepSeek author still gets a judge.
- `images` takes up to 8 `.png`/`.jpg`/`.jpeg` paths, 2 MiB each and 6 MiB in total, and sends each one as an `image_url` `data:` part merged into the last user message. Only lanes with `"vision": true` in `ci/lanes.json` receive an image request; every other lane is skipped with `no vision`. Family exclusion still runs first. A lane answering an image request with `400`, `415` or `422` is skipped with `<code> with images`, while a `400` on a request without images still stops the run.
- The PR verifier reads every `Author model: <family>/<model>` line. It warns and fails on missing author metadata, and fails on unknown families. Other ladder-call users keep the default lane order when exclusions are empty.
- `lint-clean` reports no violations on this repository tree.

## How to get to it (user POV)

- Maintainers add GitHub Actions jobs that need a hosted model and wire them through `.github/actions/ladder-call` or `ci/ladder-call`.

## Driving it with bash

Preconditions:

- Stub keys only: `AI_GATEWAY_API_KEY=stub-gw` and `OPENCODE_API_KEY=stub-oc`.

- **Stub server.** Run `python3 ci/ladder-call-stub-server.py > /tmp/tl-stub-port & stub_pid=$!; sleep 0.3; port=$(cat /tmp/tl-stub-port); export LADDER_CALL_URL_OPENCODE_GO=http://127.0.0.1:${port}/zen/go/v1/chat/completions LADDER_CALL_URL_VERCEL_AI_GATEWAY=http://127.0.0.1:${port}/v1/chat/completions`.
- **ladder-call.** Run `printf '%s\n' '{"messages":[{"role":"user","content":"hi"}]}' | python3 ci/ladder-call --purpose verify-skill`. Exit `0`. stderr ends with `ladder-call: lane opencode-go answered (verify-skill)`. stdout JSON `choices[0].message.content` is `action-ok`.
- **ladder-call verdicts.** Run `printf '%s\n' '{"messages":[{"role":"user","content":"hi"}]}' | python3 ci/ladder-call --purpose pr-verifier --require-verdict` against the stub. Exit `3`, stderr holds `lane opencode-go skipped: no verdict` then `ladder-call: no lane answered`, because `action-ok` is not a verdict.
- **ladder-call images.** Write a one-pixel PNG, then run `printf '%s\n' '{"messages":[{"role":"user","content":"hi"}]}' | python3 ci/ladder-call --purpose journey-judge --images /tmp/tl-shot.png` against the same stub. Exit `0`, stderr holds `ladder-call: attaching 1 image(s)` and `ladder-call: lane opencode-go answered (journey-judge)`. The stub answers `action-ok` whichever body arrives, so check the request body through `tests/test_ladder_call.py` when you need to see the `image_url` parts. A `.gif`, a missing path or a ninth file exits `2` with the reason.
- **Lint.** Run `python3 ci/lint-direct-model-calls .`. Exit `0`. stdout empty.

Cleanup: `kill "$stub_pid" 2>/dev/null || true`.

## Gotchas

- Without stub URLs, ladder-call hits real providers and may spend quota.
- A reasoning model can return empty `content` and put its plan in `reasoning` plus `tool_calls`; ladder-call answers from reasoning.
- A reasoning model given a large diff can spend every `max_tokens` on reasoning and never write a verdict; `--require-verdict` then makes one small decide call on the same lane with the tail of that answer.
- lint flags direct `api.deepseek.com` and similar; fix violations with `ladder-call`, not allow-list entries, unless emergency-documented.