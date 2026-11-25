# CLAUDE.md

Token Ladder reads how full each AI subscription is (Claude, Codex, Grok, OpenRouter) for every consumer that asks. Product scope, rules and data sources are in `README.md`.

## Commands

- Build: `python3 -m py_compile bin/token-ladder bin/check-tests-changed bin/plan-pick bin/gateway_burn.py tests/*.py ci/ladder-call ci/ladder-agent ci/ladder-agent-stub-server.py ci/model_families.py ci/lint-direct-model-calls ci/ladder-call-stub-server.py ci/run-verifier ci/verdict.py ci/build-verifier-request.py ci/read-verifier-answer.py ci/regression-selftest.py ci/scan-repos` (no build output; it only checks every script parses).
- Test: `timeout 600 python3 -m unittest discover -s tests` (CI runs this on every push and pull request).
- Ship: Open a PR from a `ceo/<ticket>` branch; the required checks `secret-scan`, `tests`, `no-comments` and `pstack-verifier` must be green; `gh pr merge --squash --delete-branch`; then `git -C ~/projects/token-ladder pull --ff-only` on the primary server, the live copy (the Claude hooks and the systemd units point at it). The mirror server gets it on the next `collect` run (timer, every 5 minutes, rsync in `mirror()`). Check it live with `bin/token-ladder status`.
- Diff checks: `bin/check-tests-changed origin/main` (CI runs this).
- Worker plan: `bin/plan-pick` (or `--json`) prints which lane runs the next background worker; `--plans` (or `--plans --json`) prints what each subscription includes, where it runs and its extra cost; text output also quotes a credit plan’s remaining cycle allowance (`usd_per_cycle`); `--burn` reports this month's recorded spend per lane and caller (order in `config.json` `lanes`; skips a lane that is used up, under its credit floor, ahead of pace or marked limited); `--pace-log` prints plan-pick launches since each plan's own window start beside that plan's used share and pace; `--run` launches it and, on a 429, 402, quota error or silent exit, marks the lane limited and runs the next one; it prints every lane's own output and one line saying what that lane did, records a worker-shim refusal as `failure: environment` with the refused command, and records a run that exits 0 with no output at all as `failure: silent`; neither climbs a rung. `--loop-interval` prints how many seconds a loop should wait before its next tick, from Claude's pace (thresholds in `config.json` `loop_interval`). `--not-vendor <vendor>` picks a reviewer from another vendor; repeat the flag or pass a comma list to exclude several vendors. `--claude-helper` answers for in-chat Claude helpers: exit 0 allowed under pace, exit 3 refused over pace or OFF; `--claude-emergency-until <ISO8601|off> --reason <text>` holds that answer at allowed until the set time and then lets it refuse again on its own. Sessions call `~/.local/bin/plan-pick`, a symlink to it.
- Run: `bin/token-ladder collect | status | pools --json | grok-set | mode claude|codex|auto | hook-prompt | hook-bash`.

## Layout

- `bin/token-ladder`: the whole program, Python 3 standard library only. No dependencies.
- `bin/plan-pick`: pool state in, one lane out (plan, launch command, reason). Reads `token-ladder pools --json`, live OpenCode windows and console credit, or a UTC balance anchor less OpenCode's own billed usage from the console usage export (recorded run costs when that read fails) for at most seven days (`--set-credit opencode USD`, optional `--credit-at` and `--credit-source`; $0.50 per unknown-cost run) (Go Plus is $40/month with $200 included; monthly 100% means included allowance gone; use held prepaid credit above the configured $2 floor, never top up; at the floor skip until renewal) and each credit lane's balance (cached in `~/.local/state/token-ladder/lane-credits.json`, refreshed in the background every 30 minutes); `config.json` keys `lanes`, `pace_warn_ratio`, `limited_hours`, `credit_limited_hours`, `credit_floor_usd`, `opencode_reader_command`, `opencode_credit_floor_usd`, `opencode_extra_usage`, `opencode_auto_topup`, `opencode_monthly_renewal`, `antigravity_reserve_share_pct`, `antigravity_max_daily_runs`.
- `config.json`: floor, thresholds, Codex bonus reset dates.
- `tools/`: `strip-comments.mjs` (the no-comments check) and `pi-lane-pins.mjs` (the pi extension that applies the `ci/lanes.json` gateway and provider pins to the pi worker lanes).
- `tests/`: every test file imports the program from `tests/_load.py`, which sets an empty `HOME` first.
- `systemd/`: the user timer that runs `collect`.

## Rules

| Rule | Enforced by |
|---|---|
| CLAUDE.md has Build, Test and Ship lines, under 80 lines, matching CI | `tests/test_claude_md.py` |
| Every change to `bin/` comes with a test change | `bin/check-tests-changed` in CI |
| No code comments across the tree | `node tools/strip-comments.mjs --check` in the required `no-comments` CI check |
| Tests never read the real home folder | `tests/_load.py` sets an empty `HOME` |
| No token in output or the repo | gitleaks and trufflehog on every change (required check `secret-scan`) and a weekly full rescan, `tests/test_secret_scan.py`; collectors print usage numbers only |
| The monitor is passive: it never switches a session, presses a Codex reset or turns on OpenRouter | judgment, review |
| State lives in `~/.local/state/token-ladder/`, never in the repo | judgment, review |
