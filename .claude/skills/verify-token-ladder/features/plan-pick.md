# plan-pick

plan-pick chooses a worker lane from pool state, prints human or JSON plans, gates Claude-helper use, and can launch a stub worker with `--run`.

## Sub-features

- `pick-json` prints `plan`, `command`, and `reason` as JSON.
- `pick-text` prints the worker plan line for operators.
- `plans-list` lists configured lanes and the no-extra-cost rule.
- `loop-interval` prints sleep seconds from Claude pace state.
- `claude-helper` allows or refuses Sonnet helper use from pool pace.
- `lane-ok` checks a named lane or vendor with no fallback or burn exception. Exit `0` allows it; exit `3` prints one refusal reason. `--cloud` checks the same shared subscription pool for a cloud launch. Cursor uses `grok`, Codex uses `codex`, Claude uses `claude`, Antigravity uses `google`, and credit lanes use their own balances.
- `run-stub` runs a stub lane command end to end without real spend.
- `prepaid-credit` uses live OpenCode credit or a balance anchor less recorded run costs for at most seven days, stops at $2, names the monthly renewal date once the floor is reached, and clears that floor lock on a later live read above the floor.
- `burn` reports current-month recorded pi costs and the separate measured 30-day DeepSeek direct account total.
- `commandcode` reports weighted GOAT credit and refuses private or unknown repositories before launching pi.
- `antigravity-burn` spends the whole Google plan by default: `antigravity_reserve_share_pct` ships at 100 and there is no daily run cap. A lowered share skips the lane past that share of a known daily limit, and `antigravity_max_daily_runs` caps today's `antigravity` launches.

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
- **Google history fallback.** Create `$HOME/.gemini/antigravity-cli/`, copy `tests/fixtures/agy-history.jsonl` there as `history.jsonl`, then run `bin/plan-pick --plans`. Exit `0`. The Google line includes `Pool: google. Shared with chats; the antigravity lane burns the full quota.`, a local daily count, `Limit: unknown` and `Pace: unknown`. The count depends on the current UTC day. Google writes no state; the CommandCode credit reader can populate its own cache.
- **Antigravity burns the full quota by default.** With `export PLAN_PICK_LAUNCHES=$HOME/launches.jsonl` and the shipped config, write `{"opencode":{"state":"OFF"},"grok":{"state":"OFF"},"google":{"state":"unknown","used":9,"limit":10}}` to `$HOME/pools.json` and run `bin/plan-pick --pools "$HOME/pools.json" --json`. Exit `0` and `"plan":"antigravity"`, because the 100% share never trips a known daily limit.
- **Antigravity share and cap.** Write `{"antigravity_reserve_share_pct":60}` to `$TOKEN_LADDER_LOCAL_CONFIG` and the same pools file with `"used":7`; the plan is `vercel` and the reason contains `antigravity (used up: daily limit: 7 of 10 today is at or past 60%)`. Write `{"antigravity_max_daily_runs":2}`, the same pools file with `{"state":"unknown","used":2}`, and two `{"at":<now-60>,"lane":"antigravity"}` lines into `$PLAN_PICK_LAUNCHES`; the plan is `vercel` and the reason contains `antigravity (used up: daily cap: 2 worker runs today, cap 2)`.
- **Antigravity quota fallthrough.** `timeout 120 python3 -m unittest tests.test_antigravity -v` from the repo root. Exit `0`. A `RESOURCE_EXHAUSTED` line in a temp `cli.log` sends the pick to vercel, a recovery line keeps the lane, and an early quota error in a run launches the next lane and records `antigravity` in the limits file.
- **CommandCode allowance.** Run `bin/plan-pick --plans --pools "$HOME/pools.json"`. Exit `0`. The CommandCode line includes `$10/month`, `$100 Flash usage/month`, `$20/5h` and `$50/week`, and never claims a repository visibility limit. An injected `credit:commandcode` pool's `note`, `used`, `limit` and `pace_ratio` appear without a network read.
- **CommandCode fixture and refusal.** Run `timeout 180 python3 -m unittest discover -s tests -p test_commandcode.py -v`. Exit `0`. The fixture reader preserves 70 monthly credits and 14/35 window caps. A stub `gh` marks the brief's target PRIVATE, and `--run` uses the next lane even with CommandCode burn enabled; PUBLIC runs the CommandCode stub. No production model call occurs.
- **Loop interval.** Run `bin/plan-pick --pools "$HOME/pools.json" --loop-interval --json` with pools `{"claude":{"state":"ON","used_pct":40.0,"pace_ratio":1.16}}`. Exit `0`. stdout JSON has `"seconds":10800` and `"reason":"claude pace 1.16x ahead"`.
- **Claude helper refused.** Run `bin/plan-pick --pools "$HOME/pools.json" --claude-helper` with `{"claude":{"state":"ON","used_pct":40.0,"pace_ratio":1.8}}`. Exit `3`. stdout starts with `Claude helper: refused.`
- **Lane gate.** Run `bin/plan-pick --pools "$HOME/pools.json" --lane-ok codex --cloud` with `{"codex":{"state":"ON","used_pct":40,"pace_ratio":0.5}}`. Exit `0`, stdout `codex: allowed`. Change the ratio to `1.5`. Exit `3`, stdout `codex: pace 1.50x is above 1.1x`, even if every other lane is unavailable. Run `timeout 120 python3 -m unittest discover -s tests -p test_plan_pick.py -k LaneOkTests` for skip expiry, vendor mapping, credit floors, Claude and prepaid cases.
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