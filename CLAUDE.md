# CLAUDE.md

Token Ladder reads how full each AI subscription is (Claude, Codex, Grok, OpenRouter) for every consumer that asks. Product scope, rules and data sources are in `README.md`.

The program is Rust in `rust/`. `~/.local/bin/token-ladder` and `~/.local/bin/plan-pick` are symlinks to `rust/target/release/token-ladder`; the binary dispatches on the name it was called by.

## Commands

- Build: `python3 -m py_compile bin/check-tests-changed bin/gateway_burn.py bin/tl-checkpoint bin/worker-shims/gh bin/worker-shims/sleep tests/*.py ci/ladder-call ci/ladder-agent ci/ladder-agent-stub-server.py ci/model_families.py ci/lint-direct-model-calls ci/ladder-call-stub-server.py ci/run-verifier ci/verdict.py ci/build-verifier-request.py ci/read-verifier-answer.py ci/regression-selftest.py ci/scan-repos` (no build output; it only checks every Python script parses).
- Test: `timeout 600 python3 -m unittest discover -s tests` (CI runs this on every push and pull request).
- Rust: `cd rust && timeout 300 cargo test --locked && timeout 300 cargo clippy --locked --all-targets -- -D warnings && timeout 300 cargo build --release` (CI runs test and clippy in the required `rust` check, `.github/workflows/rust.yml`).
- Ship: Open a PR from a `ceo/<ticket>` branch; the required checks `secret-scan`, `tests`, `no-comments`, `rust` and `pstack-verifier` must be green; `gh pr merge --squash --delete-branch`; then on the primary server `git -C ~/projects/token-ladder pull --ff-only`, `cd ~/projects/token-ladder/rust && cargo build --release` and `systemctl --user daemon-reload` (the live unit points into that checkout). Check it live with `~/.local/bin/token-ladder status`.
- Diff checks: `bin/check-tests-changed origin/main` (CI runs this).
- Worker plan: `~/.local/bin/plan-pick` (or `--json`) prints which lane runs the next background worker, why, and the rung. `--plans` prints what each subscription includes. `--run` launches the picked lane, moves to the next one on a 429, 402, quota error or silent exit, and refuses a brief that tells the worker to wait for CI. `--lane-ok <lane>` answers for one lane (exit 0 allowed, 3 refused); `--cloud` keeps the call compatible with cloud-agent's launch check. `--claude-helper` answers for an in-chat Claude helper (exit 0 allowed under pace, 3 refused). `--loop-interval` prints the seconds a loop should wait. `--not-vendor <vendor>` excludes vendors. `--burn` and `--pace-log` report recorded spend and launch pace. `--limited`, `--set-credit`, `--read-credits` record or read lane state.
- Run: `token-ladder collect | status | pools --json | grok-set | route | mode claude|codex|auto | hook-prompt | hook-bash | hook-agent`.

## Layout

- `rust/src`: the whole program. `main.rs` (CLI and plan-pick surface), `collect.rs`, `picker.rs`, `runner.rs`, `hooks.rs`, `usage.rs`, `ledger.rs`, `config.rs`, `domain.rs`. Standard library plus `serde`, `serde_json`, `chrono`, `clap`, `ureq`.
- `rust/tests`: `cli.rs`, `collect.rs`, `runner.rs`, driving the built binary.
- `bin/`: the remaining Python helpers, `check-tests-changed`, `tl-checkpoint`, `gateway_burn.py`, and `worker-shims/` (`gh`, `sleep` refusals for workers).
- `config.json`: floor, thresholds, lanes, plans, Codex bonus reset dates.
- `tools/`: `strip-comments.mjs` (the no-comments check), `pi-lane-pins.mjs` and `pi-commandcode.mjs` (pi worker lane pins).
- `tests/`: every test file imports `tests/_load.py`, which sets an empty `HOME` first.
- `systemd/`: the user timer that runs `collect`.

## Rules

| Rule | Enforced by |
|---|---|
| CLAUDE.md has Build, Test and Ship lines, under 80 lines, matching CI | `tests/test_claude_md.py` |
| Every change to `bin/` or `rust/` comes with a test change | `bin/check-tests-changed` in CI |
| No code comments across the tree | `node tools/strip-comments.mjs --check` in the required `no-comments` CI check |
| Tests never read the real home folder | `tests/_load.py` sets an empty `HOME` |
| No token in output or the repo | gitleaks and trufflehog on every change (required check `secret-scan`) and a weekly full rescan, `tests/test_secret_scan.py`; collectors print usage numbers only |
| The monitor is passive: it never switches a session, presses a Codex reset or turns on OpenRouter | judgment, review |
| State lives in `~/.local/state/token-ladder/`, never in the repo | judgment, review |
