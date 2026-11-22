# plan-pick

plan-pick chooses a worker lane from pool state, prints human or JSON plans, gates Claude-helper use, and can launch a stub worker with `--run`.

## Sub-features

- `pick-json` prints `plan`, `command`, and `reason` as JSON.
- `pick-text` prints the worker plan line for operators.
- `plans-list` lists configured lanes and the no-extra-cost rule.
- `loop-interval` prints sleep seconds from Claude pace state.
- `claude-helper` allows or refuses Sonnet helper use from pool pace.
- `run-stub` runs a stub lane command end to end without real spend.
- `prepaid-credit` reads OpenCode windows and console credit live, uses held prepaid credit above $2, and waits until monthly renewal once the floor is reached.
- `burn` reports current-month recorded pi costs and the separate measured 30-day DeepSeek direct account total.

## How to get to it (user POV)

- Operator runs `bin/plan-pick` from any workspace when routing a code worker.
- Hooks and skills call `bin/plan-pick --json` or `--claude-helper` before starting work.

## Driving it with bash

Preconditions:

- `export HOME=$(mktemp -d)`
- `export TOKEN_LADDER_LOCAL_CONFIG=$PWD/.test-missing-config.local.json`
- `printf '%s\n' '{"codex":{"state":"ON","used_pct":40.0,"pace_ratio":0.5}}' > "$HOME/pools.json"`

- **JSON pick.** Run `bin/plan-pick --pools "$HOME/pools.json" --json`. Exit `0`. stdout parses as JSON with keys `command`, `plan`, `reason` and `"plan":"vercel"`.
- **Plans table.** Run `bin/plan-pick --plans`. Exit `0`. stdout contains `Included means no extra cost` and a `Cursor` plan line.
- **Plans JSON.** Run `bin/plan-pick --plans --json`. Exit `0`. stdout is a JSON array of plan objects from config. The `google` plan has `chat_only: true` and a `usage` object with unknown quota, limit and pace.
- **Google history fallback.** Create `$HOME/.gemini/antigravity-cli/`, copy `tests/fixtures/agy-history.jsonl` there as `history.jsonl`, then run `bin/plan-pick --plans`. Exit `0`. The Google line includes `Pool: google. Chat-only, never workers.`, a local daily count, `Limit: unknown` and `Pace: unknown`. The count depends on the current UTC day. No state files appear.
- **Loop interval.** Run `bin/plan-pick --pools "$HOME/pools.json" --loop-interval --json` with pools `{"claude":{"state":"ON","used_pct":40.0,"pace_ratio":1.16}}`. Exit `0`. stdout JSON has `"seconds":10800` and `"reason":"claude pace 1.16x ahead"`.
- **Claude helper refused.** Run `bin/plan-pick --pools "$HOME/pools.json" --claude-helper` with `{"claude":{"state":"ON","used_pct":40.0,"pace_ratio":1.8}}`. Exit `3`. stdout starts with `Claude helper: refused.`
- **Stub run.** In a temp dir, copy `bin/plan-pick`, write `config.json` with lanes `a` (`echo '429'; exit 1`) then `b` (`echo worker-done`), empty pools file, run `bin/plan-pick --pools "$HOME/pools.json" --run`. Exit `0`. stdout contains `worker-done`. stderr mentions `lane a hit a rate or usage limit`.

## Credit and retirement regression

Run `timeout 120 python3 -m unittest discover -s tests -p test_plan_pick.py -k PrepaidCreditTests`.
These checks use an isolated command stub, never a real key or browser. They prove monthly 100% with weekly/5-hour 0% uses $24.64 live credit rather than an 8-hour-old $5.87 estimate.
They cover the inclusive $2 floor, renewal hold, stale or missing live credit, read-only command flags, DeepSeek direct retirement from workers and CI, and the separate measured burn report.

## Gotchas

- Without `--pools`, plan-pick may read live meters; always pass `--pools` or the isolated `HOME` above during verification.
- `--run` with real `config.json` lanes can spend credit; use the stub lane config from the last bullet only.
- `--claude-helper` exit `3` is expected when pace is high; do not treat it as a crash.