# plan-pick

plan-pick chooses a worker lane from pool state, prints human or JSON plans, gates Claude-helper use, and can launch a stub worker with `--run`.

## Sub-features

- `pick-json` prints `plan`, `command`, and `reason` as JSON.
- `pick-text` prints the worker plan line for operators.
- `plans-list` lists configured lanes and the no-extra-cost rule.
- `loop-interval` prints sleep seconds from Claude pace state.
- `claude-helper` allows or refuses Sonnet helper use from pool pace.
- `run-stub` runs a stub lane command end to end without real spend.
- `prepaid-credit` uses live OpenCode credit or a balance anchor less recorded run costs for at most seven days, stops at $2, names the monthly renewal date once the floor is reached, and clears that floor lock on a later live read above the floor.
- `burn` reports current-month recorded pi costs and the separate measured 30-day DeepSeek direct account total.
- `commandcode` reports weighted GOAT credit and refuses private or unknown repositories before launching pi.
- `antigravity-reserve` keeps chat room on the Google plan: a known daily limit skips the lane past `antigravity_reserve_share_pct` and an unknown limit caps launched runs at `antigravity_max_daily_runs`.

## How to get to it (user POV)

- Operator runs `bin/plan-pick` from any workspace when routing a code worker.
- Hooks and skills call `bin/plan-pick --json` or `--claude-helper` before starting work.

## Driving it with bash

Preconditions:

- `export HOME=$(mktemp -d)`
- `export TOKEN_LADDER_LOCAL_CONFIG=$PWD/.test-missing-config.local.json`
- `printf '%s\n' '{"codex":{"state":"ON","used_pct":40.0,"pace_ratio":0.5}}' > "$HOME/pools.json"`

- **JSON pick.** Run `bin/plan-pick --pools "$HOME/pools.json" --json`. Exit `0`. stdout parses as JSON with keys `command`, `plan`, `reason` and `"plan":"opencode"`.
- **Plans table.** Run `bin/plan-pick --plans`. Exit `0`. stdout contains `Included means no extra cost` and a `Cursor` plan line.
- **Plans JSON.** Run `bin/plan-pick --plans --json`. Exit `0`. stdout is a JSON array of plan objects from config. The `google` plan has `shared_with_chats: true` and a `usage` object with unknown quota, limit and pace.
- **Google history fallback.** Create `$HOME/.gemini/antigravity-cli/`, copy `tests/fixtures/agy-history.jsonl` there as `history.jsonl`, then run `bin/plan-pick --plans`. Exit `0`. The Google line includes `Pool: google. Shared with chats, so worker runs keep a reserve.`, a local daily count, `Limit: unknown` and `Pace: unknown`. The count depends on the current UTC day. Google writes no state; the CommandCode credit reader can populate its own cache.
- **Antigravity reserve.** With `export PLAN_PICK_LAUNCHES=$HOME/launches.jsonl`, write `{"opencode":{"state":"OFF"},"google":{"state":"unknown","used":9,"limit":10}}` to `$HOME/pools.json` and run `bin/plan-pick --pools "$HOME/pools.json" --json`. Exit `0`, `"plan":"vercel"`, and the reason contains `antigravity (used up: reserve for chats: 9 of 10 today is at or past 60%)`. Replace the reading with `{"state":"unknown","used":1}` and write fifteen `{"at":<now-60>,"lane":"antigravity"}` lines into `$PLAN_PICK_LAUNCHES`; the reason then contains `15 worker runs today, daily cap 15`.
- **Antigravity quota fallthrough.** `timeout 120 python3 -m unittest tests.test_antigravity -v` from the repo root. Exit `0`. A `RESOURCE_EXHAUSTED` line in a temp `cli.log` sends the pick to vercel, a recovery line keeps the lane, and an early quota error in a run launches the next lane and records `antigravity` in the limits file.
- **CommandCode allowance.** Run `bin/plan-pick --plans --pools "$HOME/pools.json"`. Exit `0`. The CommandCode line includes `$10/month`, `$100 Flash usage/month`, `$20/5h`, `$50/week` and `public repos only`. An injected `credit:commandcode` pool's `note`, `used`, `limit` and `pace_ratio` appear without a network read.
- **CommandCode fixture and refusal.** Run `timeout 180 python3 -m unittest discover -s tests -p test_commandcode.py -v`. Exit `0`. The fixture reader preserves 70 monthly credits and 14/35 window caps. A stub `gh` marks the brief's target PRIVATE, and `--run` uses the next lane even with CommandCode burn enabled; PUBLIC runs the CommandCode stub. No production model call occurs.
- **Loop interval.** Run `bin/plan-pick --pools "$HOME/pools.json" --loop-interval --json` with pools `{"claude":{"state":"ON","used_pct":40.0,"pace_ratio":1.16}}`. Exit `0`. stdout JSON has `"seconds":10800` and `"reason":"claude pace 1.16x ahead"`.
- **Claude helper refused.** Run `bin/plan-pick --pools "$HOME/pools.json" --claude-helper` with `{"claude":{"state":"ON","used_pct":40.0,"pace_ratio":1.8}}`. Exit `3`. stdout starts with `Claude helper: refused.`
- **Stub run.** In a temp dir, copy `bin/plan-pick`, write `config.json` with lanes `a` (`echo '429'; exit 1`) then `b` (`echo worker-done`), empty pools file, run `bin/plan-pick --pools "$HOME/pools.json" --run`. Exit `0`. stdout contains `worker-done`. stderr mentions `lane a hit a rate or usage limit`.
- **Real antigravity run.** Once, with a scratch clone and a one-file brief, run `bin/plan-pick --run` on a config whose only lane is `antigravity`, with `PLAN_PICK_LAUNCHES` under the temp home. Exit `0` and the repository gains the file the brief asked for. This spends plan quota; do it once per change, never in a loop.

## Credit and retirement regression

Run `timeout 120 python3 -m unittest discover -s tests -p test_plan_pick.py -k PrepaidCreditTests`.
Also run `timeout 120 python3 -m unittest discover -s tests -p test_plan_pick.py -k AnchoredCreditTests` and `timeout 120 python3 -m unittest discover -s tests -p test_plan_pick.py -k test_set_credit_and_usage_logs_drive_selection_without_browser_credit`.
These checks use isolated state and command stubs, never a real key or browser. They prove monthly 100% with weekly/5-hour 0% uses live credit or a stored anchor.
They cover recorded run-cost deductions, the $0.50 unknown-run default, copied message deduplication, unmatched launches, the inclusive $2 floor, live anchor replacement, seven-day expiry, renewal hold, UTC/source recording, and preservation during background refresh.
The CLI journey records an anchor, selects OpenCode without browser credit, adds pi costs, and selects Vercel once the balance drops below $2.
The existing checks also cover read-only command flags, DeepSeek direct retirement, and the separate measured burn report.

## Gotchas

- Without `--pools`, plan-pick may read live meters; always pass `--pools` or the isolated `HOME` above during verification.
- `--run` with real `config.json` lanes can spend credit; use the stub lane config from the last bullet only.
- `--claude-helper` exit `3` is expected when pace is high; do not treat it as a crash.