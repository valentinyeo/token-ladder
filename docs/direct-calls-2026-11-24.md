# Direct model calls across valentinyeo repos, 2026-11-24

Inventory for VSTA-107. This file only reports; it changes no repo.

## How it was produced

```sh
gh repo list valentinyeo --limit 300 --no-archived --json nameWithOwner
# per repo: shallow clone, 180s limit, then
ci/scan-repos --clone-dir /tmp/w107 <owner/repo>   # 120s limit
```

147 non-archived repos, all cloned and scanned with no clone or scan failure.
`ci/scan-repos` runs `ci/lint-direct-model-calls` and adds rules the lint does
not have: `generativelanguage.googleapis.com` and reads of
`CLAUDE_CODE_OAUTH_TOKEN` / `CODEX_AUTH_JSON` under `.github/` and `tools/`.
`ci/lint-direct-model-calls` alone still passes `multiprompt`,
`agent-fleet` and `calorie-mcp-proto`; their hits come only from the wider scan.

## Counts

| Repo | Hits | Owning chat | Runs `ladder-call` today | Runs this scan as its own check |
|---|---|---|---|---|
| valentinyeo/multiprompt | 18 | Multi Bot | yes | yes |
| valentinyeo/postbox | 11 | unknown | no | no |
| valentinyeo/sf-companion | 10 | Software Factory | no | no |
| valentinyeo/agent-fleet | 1 | unknown | no | no |
| valentinyeo/calorie-mcp-proto | 1 | unknown | no | no |
| valentinyeo/pi-agent | 1 | unknown | no | no |
| 141 other repos | 0 | | | no |

42 hits in 6 repos, 141 repos clean. `multiprompt-android` is the only
other repo that already runs the scan as its own check, and it is clean.

## Every hit

| Repo | File:line | Provider / what the scan matched | Owning chat | What it looks like |
|---|---|---|---|---|
| valentinyeo/multiprompt | `.github/workflows/chat-delivery-formal.yml:87` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | Multi Bot | claude-code-action runs on the plan OAuth token instead of the ladder. |
| valentinyeo/multiprompt | `.github/workflows/chat-still.yml:77` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | Multi Bot | claude-code-action runs on the plan OAuth token instead of the ladder. |
| valentinyeo/multiprompt | `.github/workflows/maintain-verification.yml:58` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | Multi Bot | claude-code-action runs on the plan OAuth token instead of the ladder. |
| valentinyeo/multiprompt | `.github/workflows/pr-verifier.yml:230` | openai plan (CODEX_AUTH_JSON) | Multi Bot | Codex CLI runs on the plan login instead of the ladder. |
| valentinyeo/multiprompt | `.github/workflows/pr-verifier.yml:243` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | Multi Bot | claude-code-action runs on the plan OAuth token instead of the ladder. |
| valentinyeo/multiprompt | `.github/workflows/verify-skill-eval.yml:129` | openai plan (CODEX_AUTH_JSON) | Multi Bot | Codex CLI runs on the plan login instead of the ladder. |
| valentinyeo/multiprompt | `.github/workflows/verify-skill-eval.yml:162` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | Multi Bot | claude-code-action runs on the plan OAuth token instead of the ladder. |
| valentinyeo/multiprompt | `tools/pr-verifier.cjs:861` | openai plan (CODEX_AUTH_JSON) | Multi Bot | The verifier harness itself spawns the CLI with the plan login. |
| valentinyeo/multiprompt | `tools/pr-verifier.cjs:867` | openai plan (CODEX_AUTH_JSON) | Multi Bot | The verifier harness itself spawns the CLI with the plan login. |
| valentinyeo/multiprompt | `tools/pr-verifier.cjs:886` | openai plan (CODEX_AUTH_JSON) | Multi Bot | The verifier harness itself spawns the CLI with the plan login. |
| valentinyeo/multiprompt | `tools/pr-verifier.test.js:248` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | Multi Bot | Test fixture for the verifier above. |
| valentinyeo/multiprompt | `tools/pr-verifier.test.js:379` | openai plan (CODEX_AUTH_JSON) | Multi Bot | Test fixture for the verifier above. |
| valentinyeo/multiprompt | `tools/pr-verifier.test.js:428` | openai plan (CODEX_AUTH_JSON) | Multi Bot | Test fixture for the verifier above. |
| valentinyeo/multiprompt | `tools/publish-verification-maintenance.test.mjs:44` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | Multi Bot | Test fixture for the verifier above. |
| valentinyeo/multiprompt | `tools/verify-skill-eval.mjs:130` | openai plan (CODEX_AUTH_JSON) | Multi Bot | The verifier harness itself spawns the CLI with the plan login. |
| valentinyeo/multiprompt | `tools/verify-skill-eval.mjs:144` | openai plan (CODEX_AUTH_JSON) | Multi Bot | The verifier harness itself spawns the CLI with the plan login. |
| valentinyeo/multiprompt | `tools/verify-skill-eval.test.mjs:181` | openai plan (CODEX_AUTH_JSON) | Multi Bot | Test fixture for the verifier above. |
| valentinyeo/multiprompt | `tools/verify-skill-eval.test.mjs:189` | openai plan (CODEX_AUTH_JSON) | Multi Bot | Test fixture for the verifier above. |
| valentinyeo/postbox | `tools/check-ladder-calls.py:17` | own scanner (no call) | unknown | Postbox ships its own copy of this scan; the lines are its rule table. False positive. |
| valentinyeo/postbox | `tools/check-ladder-calls.py:18` | own scanner (no call) | unknown | Postbox ships its own copy of this scan; the lines are its rule table. False positive. |
| valentinyeo/postbox | `tools/check-ladder-calls.py:19` | own scanner (no call) | unknown | Postbox ships its own copy of this scan; the lines are its rule table. False positive. |
| valentinyeo/postbox | `tools/check-ladder-calls.py:20` | own scanner (no call) | unknown | Postbox ships its own copy of this scan; the lines are its rule table. False positive. |
| valentinyeo/postbox | `tools/check-ladder-calls.py:21` | own scanner (no call) | unknown | Postbox ships its own copy of this scan; the lines are its rule table. False positive. |
| valentinyeo/postbox | `tools/check-ladder-calls.py:22` | own scanner (no call) | unknown | Postbox ships its own copy of this scan; the lines are its rule table. False positive. |
| valentinyeo/postbox | `tools/test-check-ladder-calls.sh:46` | own scanner fixture (no call) | unknown | Test fixtures for that scanner. False positive. |
| valentinyeo/postbox | `tools/test-check-ladder-calls.sh:49` | own scanner fixture (no call) | unknown | Test fixtures for that scanner. False positive. |
| valentinyeo/postbox | `tools/test-check-ladder-calls.sh:52` | own scanner fixture (no call) | unknown | Test fixtures for that scanner. False positive. |
| valentinyeo/postbox | `tools/test_voice_language.py:47` | anthropic.com/v1 in asserted evidence | unknown | Asserts the product formatter evidence came from api.anthropic.com. The scan cannot see app code; confirm by hand. |
| valentinyeo/postbox | `tools/verify-wazig/check-voice-language.py:9` | anthropic.com/v1 in asserted evidence | unknown | Same assertion, product side. Confirm by hand whether the formatter calls Anthropic directly. |
| valentinyeo/sf-companion | `tools/README.md:9` | provider URLs and keys in docs (no call) | Software Factory | Documents the balance readers below. No model call. |
| valentinyeo/sf-companion | `tools/README.md:10` | provider URLs and keys in docs (no call) | Software Factory | Documents the balance readers below. No model call. |
| valentinyeo/sf-companion | `tools/README.md:12` | provider URLs and keys in docs (no call) | Software Factory | Documents the balance readers below. No model call. |
| valentinyeo/sf-companion | `tools/README.md:13` | provider URLs and keys in docs (no call) | Software Factory | Documents the balance readers below. No model call. |
| valentinyeo/sf-companion | `tools/export-factory-feed.py:4` | provider credit endpoints (no completion) | Software Factory | GETs /v1/credits and /user/balance to show spend. No model call, but it does contact providers directly. |
| valentinyeo/sf-companion | `tools/export-factory-feed.py:98` | provider credit endpoints (no completion) | Software Factory | GETs /v1/credits and /user/balance to show spend. No model call, but it does contact providers directly. |
| valentinyeo/sf-companion | `tools/export-factory-feed.py:102` | provider credit endpoints (no completion) | Software Factory | GETs /v1/credits and /user/balance to show spend. No model call, but it does contact providers directly. |
| valentinyeo/sf-companion | `tools/export-factory-feed.py:145` | provider credit endpoints (no completion) | Software Factory | GETs /v1/credits and /user/balance to show spend. No model call, but it does contact providers directly. |
| valentinyeo/sf-companion | `tools/export-factory-feed.py:149` | provider credit endpoints (no completion) | Software Factory | GETs /v1/credits and /user/balance to show spend. No model call, but it does contact providers directly. |
| valentinyeo/sf-companion | `tools/export-factory-feed.py:166` | provider credit endpoints (no completion) | Software Factory | GETs /v1/credits and /user/balance to show spend. No model call, but it does contact providers directly. |
| valentinyeo/agent-fleet | `.github/workflows/claude-review.yml:31` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | unknown | claude-code-action runs Claude with the plan OAuth token. |
| valentinyeo/calorie-mcp-proto | `.github/workflows/pstack-verifier.yml:182` | anthropic plan (CLAUDE_CODE_OAUTH_TOKEN) | unknown | The verifier step hands Claude Code the plan OAuth token. |
| valentinyeo/pi-agent | `tools/verify-static.mjs:17` | direct key read (env.DEEPSEEK_API_KEY) | unknown | An assertion in a static check, but it asserts the server talks to DeepSeek with its own key. |

## Reading the count

- `multiprompt` 18: the PR verifier, the skill-eval harness and four
  workflows run Claude Code or Codex CLI on plan logins. Real gap.
- `postbox` 11: 9 of them are the repo's own scanner and its test
  fixtures matching themselves. 2 assert the product formatter used
  `api.anthropic.com`; app code sits outside `.github/` and `tools/`,
  so treat those as unconfirmed.
- `sf-companion` 10: `export-factory-feed.py` reads provider credit
  balances, not completions, and the README documents them.
- `pi-agent` 1, `calorie-mcp-proto` 1, `agent-fleet` 1: single lines.
  `pi-agent` flags an assertion about its DeepSeek key; the other two
  hand a Claude plan token to a CLI.

Owning chat is `unknown` wherever the repo name does not map to one of
the chats named in the ticket.
