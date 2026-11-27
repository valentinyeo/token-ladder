# plan-pick

plan-pick chooses a worker lane from pool state, prints human or JSON plans, gates Claude-helper use, and can launch a worker with `--run`. It is the same binary, reached as `plan-pick` on argv[0] or with `--plan-pick <flags>`.

## Sub-features

- `pick-json` prints `plan`, `command`, `reason` and the rung as JSON.
- `pick-text` prints the worker plan line for operators.
- `plans-list` lists the configured plans, where they run and their extra cost.
- `loop-interval` prints sleep seconds from the `status.json` Claude pace.
- `claude-helper` allows or refuses Sonnet helper use from pool pace.
- `lane-ok` checks a named lane or vendor with no fallback. Exit `0` allows it; exit `3` prints one refusal reason. `--cloud` keeps the shared-pool check cloud-agent calls, and needs `--lane-ok`.
- `run` launches the picked lane and moves on when a lane is limited. `rust/tests/runner.rs` covers it with stub lanes.

## How to get to it (user POV)

- The operator runs `~/.local/bin/plan-pick` from any workspace when routing a code worker.
- Hooks and skills call it with `--json` or `--claude-helper` before starting work.

## Driving it with bash

Preconditions:

- `export HOME=$(mktemp -d)`
- `export TOKEN_LADDER_LOCAL_CONFIG=$PWD/.test-missing-config.local.json`
- `export B=$PWD/rust/target/release/token-ladder`
- `printf '%s\n' '{"codex":{"state":"ON","used_pct":40.0,"pace_ratio":0.5}}' > "$HOME/pools.json"`

- **JSON pick.** Run `"$B" --plan-pick --pools "$HOME/pools.json" --json`. Exit `0`. stdout parses as JSON with keys `plan`, `command`, `reason`, `rung`, `rung_source`, `rung_reason`, and `"plan":"opencode"` while only codex carries pool state.
- **Plans table.** Run `"$B" --plan-pick --plans`. Exit `0`. stdout has one line per plan, such as `Claude (anthropic): ... Extra cost: none.`
- **Plans JSON.** Run `"$B" --plan-pick --plans --json`. Exit `0`. stdout is an object with `config_path` and `plans`, one entry per config plan carrying `name`, `vendor`, `pool`, `chat_only` and `extra_cost`.
- **Antigravity worker refusal.** Write `{"google":{"state":"ON","used_pct":0,"pace_ratio":0}}` to `$HOME/pools.json` and run `"$B" --plan-pick --pools "$HOME/pools.json" --lane-ok antigravity`. Exit `3`, stdout `antigravity: chat-only pool, never workers`, even without `chat_only` in the pool input.
- **Antigravity hook refusal.** Pipe `{"tool_input":{"command":"timeout 60 agy --print code"}}` into `"$B" hook-bash`. Exit `0`, stdout denies the command with `Antigravity is orchestrator-only (chat-only)`. Repeat with `agy -p /usage --print-timeout 45s`; stdout is empty.
- **Loop interval.** Write `{"ts": 1795785009.0, "pools": {"claude": {"state": "ON", "used_pct": 40.0, "pace_ratio": 1.16}}}` to `$HOME/.local/state/token-ladder/status.json` and run `"$B" --plan-pick --loop-interval --json`. Exit `0`. stdout JSON has `"seconds":10800` and `"reason":"claude pace 1.16x ahead"`.
- **Claude helper.** With that same status file, run `"$B" --plan-pick --claude-helper`. Exit `3`. stdout starts with `Claude helper: refused.` On an empty home the answer is `allowed` with exit `0`.
- **Lane gate.** Run `"$B" --plan-pick --pools "$HOME/pools.json" --lane-ok codex --cloud`. Exit `0`, stdout `codex: allowed`. Run `"$B" --plan-pick --cloud` alone. Exit `2`, stderr `plan-pick: --cloud requires --lane-ok`.
- **Runner.** `cd rust && timeout 300 cargo test --locked` covers `--run` with stub lanes: the next-lane move on a 429 or empty credit, the worker-shim refusal, the null-lane exit 75, and the rung climb.

## Gotchas

- Without `--pools`, plan-pick may read live meters; always pass `--pools` or the isolated `HOME` above during verification.
- `--run` with real `config.json` lanes can spend credit; only the Rust stub tests run it during verification.
- `--claude-helper` exit `3` is expected when pace is high or the Claude pool is OFF; do not treat it as a crash.
