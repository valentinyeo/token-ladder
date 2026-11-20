# token-ladder pools

token-ladder reports subscription pool states read-only via `pools --json` and the human `status` summary.

## Sub-features

- `pools-json` prints claude, codex, and grok pool objects without network or subprocess side effects.
- `status-lines` prints TOKEN MAX, FLOOR, FEED, and per-provider lines on an empty home.

## How to get to it (user POV)

- Operator runs `bin/token-ladder status` or `bin/token-ladder pools --json` to see usage before routing work.

## Driving it with bash

Preconditions:

- `export HOME=$(mktemp -d)`
- `export TOKEN_LADDER_LOCAL_CONFIG=$PWD/.test-missing-config.local.json`

- **Pools JSON.** Run `bin/token-ladder pools --json`. Exit `0`. stdout JSON object has exactly keys `claude`, `codex`, `grok`. `$HOME` stays empty (no new files).
- **Status empty home.** Run `bin/token-ladder status`. Exit `0`. stdout lines include `TOKEN MAX OK`, `FLOOR OK`, `FEED OK`, and provider lines with `? used, reset unknown`.

## Gotchas

- Do not use `~/.local/state/token-ladder` as evidence; it is shared live state.
- `pools --json` must not create files under `$HOME`; if files appear, the command was not read-only.