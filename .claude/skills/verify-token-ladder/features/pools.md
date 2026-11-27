# token-ladder pools

token-ladder reports subscription pool states read-only via `pools --json` and the human `status` summary.

## Sub-features

- `pools-json` prints claude, codex, grok and the chat-only google pool objects without network or subprocess side effects.
- `status-lines` prints TOKEN MAX, FLOOR, FEED, and per-provider lines on an empty home.

## How to get to it (user POV)

- Operator runs `token-ladder status` or `token-ladder pools --json` to see usage before routing work.

## Driving it with bash

Preconditions:

- `export HOME=$(mktemp -d)`
- `export TOKEN_LADDER_LOCAL_CONFIG=$PWD/.test-missing-config.local.json`
- `export B=$PWD/rust/target/release/token-ladder`

- **Pools JSON.** Run `"$B" pools --json`. Exit `0`. stdout JSON object has exactly keys `claude`, `codex`, `grok`, `google`, every provider `state` is `unknown` with `note: "no reading"`, and `google.chat_only` is `true`. `$HOME` stays empty (no new files).
- **Status empty home.** Run `"$B" status`. Exit `0`. stdout is one line: `Token ladder: Claude unknown; Codex unknown; Grok unknown; Google unknown. Mode unknown.`

## Gotchas

- Do not use `~/.local/state/token-ladder` as evidence; it is shared live state.
- `pools --json` must not create files under `$HOME`; if files appear, the command was not read-only.