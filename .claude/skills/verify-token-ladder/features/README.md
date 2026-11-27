# Token Ladder verification map

Read this index before driving. Each feature file is a user- or operator-facing path through the CLI and CI helpers.

## Baseline preconditions

- Repo root with `config.json` and an executable `rust/target/release/token-ladder` (`cd rust && cargo build --release --locked`).
- `HOME=$(mktemp -d)` and `TOKEN_LADDER_LOCAL_CONFIG=$PWD/.test-missing-config.local.json`.
- For ladder-call journeys: stub server and stub API keys only; never call production endpoints with real keys during map runs.

## Driving conventions

- Run commands from the repo root unless a feature says otherwise.
- Pool files for plan-pick live under `$HOME/pools.json` when a feature uses `--pools`.
- Keep quoted flags and lane names unchanged from the feature file.

## Features

- [plan-pick](./plan-pick.md) worker plan pick through `--plan-pick`: JSON output, plans listing, loop interval, Claude helper gate and the lane gate.
- [pools](./pools.md) read-only `pools --json` and `status` against isolated home.
- [ladder-call](./ladder-call.md) `ci/ladder-call` through the stub server and `ci/lint-direct-model-calls` on the repo tree.