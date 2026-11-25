# token-ladder

Token Ladder reads subscription usage for Claude, Codex (including `hax`), Grok and Google Antigravity. The Google Antigravity plan is shared by chats and the `antigravity` worker lane.

`collect` runs on a timer and writes the state. `pools --json` prints the current pool states for any caller. `grok-set` records a Grok reading. Nothing here chooses a plan, reserves a pool, launches a session or holds a lease: those rules live in `spawn-worker` code and in the valentin-mode skill.

## Install

Put this checkout's `bin` directory on PATH using your normal shell configuration; the program makes no settings changes by itself.

The systemd user units in `systemd/` run `collect` every five minutes. Copy them to `~/.config/systemd/user/`, then `systemctl --user enable --now token-ladder.timer`. `pools` reads the `status.json` that this timer refreshes.

## plan-pick

`plan-pick --run` asks the server first: it calls `vps-state --can-launch worker --no-write --brief` before launching a lane. On HOLD it starts nothing, logs lane `server-hold`, prints the vps-state line and exits 75 with the reason, so run it again in a few minutes. A missing binary, a timeout or any other exit never blocks a launch. Plain picks, `--json`, `--claude-helper` and `--lane-ok` skip the check.

## What the reader exposes

```sh
token-ladder pools --json
token-ladder grok-set --pct 42 --resets-at 2026-11-01T12:00:00Z
```

`pools --json` prints one object per pool: `used_pct`, `hours_left`, `resets_at`, `state` (`ON`, `WARN`, `OFF` or `unknown`) and `pace_ratio`. It is read-only and works offline against the last collected state. `pools` without `--json` prints the same states as one line each.

`grok-set` atomically writes `~/.local/state/token-ladder/grok.json` with `pct` (0-100), ISO `resets_at` and `updated_at=now`. The reader also accepts legacy `used_pct` in place of `pct`, but still requires ISO `updated_at`. A missing or malformed file, missing timestamp, expired reset, future update or update older than `grok_stale_hours: 3` makes Grok unknown.

Optional `burn` in `config.json` or `config.local.json`: `{"lane": "<lane or pool>", "until": "<ISO time with zone>"}`.
While `until` is in the future, `plan-pick` runs that lane first and ignores its pace ratio; other skips still apply and `--claude-helper` is unchanged.

## Worker lanes

`plan-pick` runs a background worker on the first free lane allowed by its rung in `config.json`.

For the default medium rung, the Cursor lane runs first while its `first_until` time is in the future (`2026-11-24T23:59:00Z`, when Valentin's Cursor plan ends). After that time the order is the table below.

| Order | Lane | Included usage / credit |
|---|---|---|
| 1 | OpenCode Go | Plan windows, then prepaid credit |
| 2 | Antigravity (`agy`) | Google Antigravity plan quota, burned fully by the lane |
| 3 | Vercel Token Pass | Token Pass balance only |
| 4 | CommandCode GOAT | $10/month; up to $100 Flash/month, $20/5h, $50/week |
| 5 | OpenRouter | Prepaid credit |
| 6 | Codex | Subscription pool |
| 7 | Cursor | Subscription pool; last after `first_until` |

### Agent-held lanes

When Multibot runs agent chats on a plan, `plan-pick` keeps that plan for them and tries its lane last. Multibot writes `~/.local/state/multiprompt/agent-providers.json` atomically after every workspace AI switch:

```json
{"updated": "2026-11-24T10:00:00Z", "chats": [{"workspace": "HT Product", "chat": "HT Bugs", "tmux": "live-abc", "provider": "antigravity", "model": "gemini-4-neon", "since": "2026-11-24T09:40:00Z"}], "by_provider": {"antigravity": 3, "codex": 1, "claude": 2}}
```

A lane is held when `by_provider[<lane>] > 0`, and provider names are the lane names above. A held lane keeps its rung eligibility but moves behind every lane that is not held, and the pick line says `antigravity held by agent chats; tried last`. `claude` is not a worker lane, so it never holds one. A missing or unreadable file, a file older than 24 hours by `updated`, or a count that is not a number changes nothing. A malformed file changes nothing and prints one note. `plan-pick` only reads the file. `PLAN_PICK_AGENT_PLANS` overrides the path, for tests.

### Worker rungs and first-review cost

The goal is the cheapest model that passes its first review. Starting rungs per model need a later eval. This change supplies the measurement and climb mechanism, not that eval.

Add one line to `brief.txt`:

```text
Rung: light
```

The values are `light`, `medium`, and `hard`. No line means `medium`. Invalid or duplicate lines are errors. `plan-pick --run` and dry-run picks read this line. Use `--brief FILE` to preview another brief without running a worker.

`config.json` `rungs` lists allowed lanes in order. Each entry can override `model` and `effort`.

- Light uses DeepSeek Flash lanes and the pinned Vercel GLM 5.3 Flash lane, at low effort.
- Medium keeps the existing order, including the temporary Cursor priority.
- Hard allows only Codex `gpt-6.1-sol` at high effort and Antigravity `gemini-4-neon-high`.

Pool limits, credit floors, manual skips, and pace apply within each rung. No capacity in light or medium climbs to the next rung. A climbed rung with no free lane falls back to the highest lower rung that has one and says so in one line (`rung hard has no free lane; running at medium`), and the climb stays recorded, so the next run tries the higher rung first. `--run` exits 75 only when no rung has a free lane, meaning wait. Rung picks never use the legacy least-ahead-of-pace fallback or bypass pace through a burn override.

A work failure raises the next run one rung: the worker ran and exited nonzero for its own reason, ran and produced no PR when one was asked for, or the PR's first verifier returned FAIL. A lane limit (429, 402, quota, empty or silent exit, no lane left, exit 75) is recorded as `failure: lane`, and an exit 0 with no output at all as `failure: silent`. A worker-shim refusal is recorded as `failure: environment` with the refused command. None of those climbs: the next run stays on its brief rung. `--run` prints the lane's own output, then one line saying what the lane did, so an early give-up is visible in the caller's log. Plain `plan-pick`, `--json`, `--lane-ok` and `--run` share one rung resolution and print the resolved rung with its reason. A later PASS does not erase a first FAIL. A successful retry keeps its climbed rung. A failed hard run stays hard and appears in the report's `hard_failed` count.

The job identity is its lease ticket id, otherwise the SHA-256 of the brief. The picker detects its own ancestor's local fleet lease. You can also pass `--lease FILE` or set `PLAN_PICK_LEASE`. A lease receipt can supply `ticket_id`, `ticket`, or `key`; fleet receipts use the ticket filename. `rung-attempts.json` beside `launches.jsonl` stores the latest attempt per job, with `rung_climbed` when the run fell back below its climbed rung. Neither file contains brief text.

New launches append linked start and finish records. Start records contain rung, rung source, lane, model, effort, and ticket. Finish records contain exit code, PR URL, `failure` (`work`, `lane`, `silent` or `environment`), the refused command when a worker shim said no, run-local input/cached/output tokens, and USD cost. JSON output from pi, hax, Antigravity, and Cursor supplies usage. Missing usage stays unknown, never zero. Input counts exclude cached reads. Prices in `list_prices` are USD per million tokens. All costs are labelled `list-equivalent`, including subscription lanes; these are comparison costs, not invoices.

```sh
plan-pick --rung-report --last 50
```

The metric is total cost of the last N finished launches divided by the count that passed first review. Lower is better. Failed attempts remain in the numerator. The report prints job, pass, unknown, and error counts, then first-pass rate and average known cost per rung and model. Review rates exclude unknown verdicts and include run failures. Errors count failed runs, malformed records, and failed review reads. Any missing cost makes the overall metric unavailable. No passes also makes it unavailable. Unknown reviews make the result provisional.

The command reads each PR's earliest verifier PASS/FAIL comment through `gh api --paginate`. It caches definitive verdicts in `verifier-cache.json` beside the launch log. Ordinary author comments do not count. Old launch records remain readable, but lack finish data; `legacy_missing_finish` reports how many cannot enter this metric. `unfinished` counts new launches without a finish.

`tests/fixtures/rung-report.json` fixes the sensitivity workload. Two cheap FAILs and one strong PASS cost $1.20 per first-pass job. If all three pass at the same costs, the metric is $0.40. The fixture makes no claim about actual model quality.

Never use Hypertask `brain-*` labels or any ticket difficulty tag to select a rung. Never make an extra model call to judge difficulty. Only the brief and recorded outcomes select the rung.

### Antigravity worker lane

`agy models` on this server lists `gemini-4-neon-high`, Gemini 4 Neon (High), and that slug is the lane's model. The lane runs `agy --print "$(cat brief.txt)" --model gemini-4-neon-high --dangerously-skip-permissions --print-timeout 45m --output-format json` in the brief directory, with the worker timeout also recorded as the lane's `print_timeout` field.

No `--add-dir` is needed. A probe run in one directory created a file in a separate clone and read `git status --short` there, and a second run answered `gh version 2.103.3 (2026-10-26)`. `agy` keeps a 0s print timeout by default, which waits forever; the lane sets 45m instead.

Google documents this use. [Headless mode](https://antigravity.google/docs/cli/headless/) describes print mode for scripting and CI pipelines, including `-p`, `--output-format json`, `--print-timeout`, `--dangerously-skip-permissions` and a worked CI example, so automated headless runs are supported rather than banned. The [terms](https://antigravity.google/terms) ban third-party access to the service: "Using third party software, tools, or services to access the Service (e.g. using OpenClaw with Antigravity OAuth) is a breach of this Agreement." This lane runs Google's own `agy` and never lifts a credential into another client, so it stays inside that line. The [plans page](https://antigravity.google/docs/plans.md) lists CLI access in the baseline of every plan and describes five-hour and weekly quotas; it publishes no numeric daily limit.

No extra cost holds only while overages are off. [CLI AI credits](https://antigravity.google/docs/cli/credits/) says the `useG1Credits` setting and the **AI Credit Overages** setting decide whether exhausted quota spends purchased credits, so that setting must stay on **Never**. The lane never changes it and never buys credit.

The lane burns the whole Antigravity quota by default. `antigravity_reserve_share_pct` is 100, so no daily limit is held back for chats, and there is no default per-day run cap. The lane stops only when today's newest quota line in `~/.gemini/antigravity-cli/cli.log` is a `RESOURCE_EXHAUSTED` or quota-exceeded error; then the next lane runs. Lower `antigravity_reserve_share_pct` below 100 to keep part of a known daily limit for chats, or set `antigravity_max_daily_runs` to cap launched runs per UTC day from the `antigravity` entries in `~/.local/state/token-ladder/launches.jsonl`. Neither number is a quota reading: the local history cannot separate chats from workers.

A run that fails on `RESOURCE_EXHAUSTED`, 429 or a quota error marks the lane limited for `rate_limited_minutes` (default 5) and launches the next lane. Provider challenges and fast failures before work use `fast_fail_minutes` (default 5). A second fast 4xx failure on the same lane within an hour parks it for `fast_fail_repeat_minutes` (default 60) instead, so a lane that always 400s stops killing the next worker too. Empty credit still uses `credit_limited_hours` (default 12); silent failures use `limited_hours`. An `agy` quota error written to `cli.log` also marks the `google` pool OFF. A print timeout is not a quota error, even when the agent's work summary mentions quota.

New entries in `lane-limits.json` contain `until`, `reason`, `source` and `error_line`. Both plan-pick and the dashboard show the diagnostic. Numeric expiries and the older `LANE:reason` format remain readable. Manual limits default to source `manual`.

When every lane is skipped and Codex pace is above 1.3x, plan-pick returns `plan: none` with `next_free` for the soonest expiring limit whose pool is otherwise usable. It waits instead of launching a limited lane early or sending another job to Codex. Run `--run` again after that time. At or below 1.3x, or without an eligible expiry, the existing least-ahead fallback stays.

### CommandCode GOAT

The [GOAT allowance](https://commandcode.ai/docs/plans/goat) is a weighted 70-credit monthly pool. Flash currently gets up to $100 of usage. The live caps are 14 credits per five hours and 35 per week, equivalent to about $20 and $50 of Flash usage. These are shared across models, not separate model balances. [Usage limits](https://commandcode.ai/docs/resources/usage-limits) describes the weighting and first-request window resets. Promotional multipliers can change under the [terms](https://commandcode.ai/terms).

The reader calls `https://api.commandcode.ai/alpha/billing/credits` and `/alpha/billing/subscriptions` with the same API key. It caches monthly credit, window caps and resets, and billing dates without storing the key or account identifiers. `plan-pick --read-credits commandcode` refreshes it; `--plans` shows the cached balance and monthly pace. Missing, stale or exhausted readings disable the lane. Purchased extra credits are excluded from selection, although the provider itself can spend existing extra credits after a plan limit.

The pi lane loads `tools/pi-commandcode.mjs` and uses the [OpenAI-compatible Provider API](https://commandcode.ai/docs/provider). Set `COMMANDCODE_API_KEY` in the environment, or the launch command reads the `credential` field of 1Password item `Commando Code API Key` with `yeo-key`. No key belongs in `config.json` or a PR.

The Provider API docs explicitly support agent clients and coding plans. Valentin approved this lane for every job on 2026-11-23, including private and internal repositories and unknown visibility. Repository visibility does not restrict selection or launch, including burn overrides, and selection never inspects the target repository. The [terms](https://commandcode.ai/terms) prohibit automated website requests, and the [privacy policy](https://commandcode.ai/privacy) allows up to 30 days of prompt retention and does not guarantee upstream no-training without [ZDR](https://commandcode.ai/docs/resources/zdr).

A three-output-token chat confirmed `https://api.commandcode.ai/provider/v1` and `deepseek/deepseek-v4.1-flash` on 2026-11-22. It supports chat judging technically, but is not in `ci/lanes.json` yet. A future judge lane would need `COMMANDCODE_API_KEY`; no GitHub secret is set by this change.

The Vercel AI Gateway lane spends Token Pass credit only, and the OpenRouter lane uses one upstream provider, so neither can fall to Baseten when its credit runs low.
The gateway carries two lanes, both pinned to Token Pass: the DeepSeek lane and, right after it, a `zai/glm-5.3-flash` judge lane. The Token Pass already covers that model, so the judge adds no cost, and a DeepSeek-authored PR still has a judge from another family.
`ci/lanes.json` holds both pins: `gateway` on the gateway lanes, `provider` on the OpenRouter lane.
`ci/ladder-call` sends them as `providerOptions.gateway` and `provider` in the request body.
The two pi lanes on those providers load `tools/pi-lane-pins.mjs` with `-e`, and that extension writes the same two fields from the same file through pi's `before_provider_request` hook.
Change the pin in `ci/lanes.json` and both paths follow.

## Google Antigravity usage

`plan-pick --plans` shows the `google` pool as shared with chats, with used, limit and pace fields. `--plans --json` adds a `usage` object to its plan. `token-ladder pools --json` also includes `google`. The `antigravity` worker lane reads this pool and burns it fully by default, as described under [Antigravity worker lane](#antigravity-worker-lane).

The read-only fallback counts `timestamp` metadata in `~/.gemini/antigravity-cli/history.jsonl` from midnight UTC through now. These timestamps are milliseconds since the Unix epoch. The count covers local history entries, which can include slash commands, not model requests, tokens or quota consumption. Other devices, retries, tool calls and unrecorded chats are not counted. The reader never opens credential or token files, calls a quota endpoint, signs in, buys overages or runs an agent.

Missing, unreadable, empty or wholly invalid history produces unknown usage. Malformed records and future timestamps are ignored. An older valid history produces zero local entries today, not proof of zero account usage. Quota percentage, limit, reset and pace remain unknown even when a local count is available. A missing Google reading never stops `plan-pick`.

### Used up from the agy log

The newest quota-related line in `~/.gemini/antigravity-cli/cli.log` for the current day decides one extra thing. When that line is a `RESOURCE_EXHAUSTED`, rate-limit or quota-exceeded error, `token-ladder pools` prints the pool as OFF and used up at the time of the line, and `plan-pick --plans` adds `Used up at <ISO time>.` with `used_up` and `used_up_at` in `--plans --json`. A later successful quota refresh, or no error, leaves today's behaviour alone. agy writes log times in local time; the reader shows them in UTC. Only `cli.log` is opened, never `antigravity-oauth-token` or any other file in that folder, and the reader still never runs `agy`.

### Quota source investigation, 2026-11-22

- `agy --help` lists no status or usage subcommand. No prompt was run.
- Local cache metadata, state field names and SQLite schemas expose conversation metadata, not a quota snapshot. CLI logs refer to `retrieveUserQuotaSummary` and log quota refresh errors, but hold no numeric quota reading. Credential and token files were not opened.
- [Google's Antigravity plans](https://antigravity.google/docs/plans.md) describes Pro and Ultra quotas refreshing every five hours, with weekly limits. Quota depends on work and available capacity. Google publishes **no numeric daily Antigravity limit** there. Account-specific quota is shown on the app's Settings page, which this reader does not access.
- The [Gemini CLI quota table](https://geminicli.com/docs/resources/quota-and-pricing/) lists daily request caps of 1,000 for individuals, 1,500 for AI Pro and 2,000 for AI Ultra. These are Gemini CLI limits, not an Antigravity daily allowance. Applying 1,500 to this pool would invent a limit. The exact subscribed tier is also unconfirmed.

## OpenCode monthly allowance

Valentin has **OpenCode Go Plus**, $40/month with a $200 monthly DeepSeek allowance.
`config.json` `plans` holds the price and allowance. The reader converts the monthly percentage to dollars against that allowance.
`plan-pick --plans` shows the monthly, weekly, and 5-hour readings separately.
A monthly reading of 100% or more means used up, even when weekly and 5-hour readings are zero.
Valentin's 2026-11-22 dashboard showed $24.64 available credit, monthly 100% used, weekly 0%, and 5-hour 0%.
The monthly renewal was 30 November, about 8 days and 1 hour away. Weekly reset was 2 hours and 19 minutes away.
Extra Usage is on. Enabled models are `beam-free`, `deepseek-v4.1-flash`, and `glm-5.3-flash`; workers still use DeepSeek Flash only.

Prepaid credit already held gets used. Once the included allowance is gone, workers use OpenCode Extra Usage while the live or anchored available credit is above `opencode_credit_floor_usd`, which defaults to $2.
At or below the floor, the lane is skipped until the monthly renewal, and the reason names that renewal date. Worker selection records that renewal in the existing lane-limits file; a later live credit read above the floor clears that lock, while a rate-limit lock stays.
Auto top-up is OFF. The reader never purchases credit or changes billing settings.

`opencode_reader_command` configures the installed read-only `opencode-read` command.
Every pick without an injected OpenCode pool and every `--plans` call runs it with `--print` for current quota windows, then `--credits --print` for current console credit.
The usage API has no dollar balance. The console credit command requires an open, signed-in Multiprompt browser pane.
Each command has a 180-second timeout. Live usage must be no older than five minutes to authorize included usage; live credit must be no older than one minute.
A successful live credit read replaces the balance anchor, even when included allowance remains or the quota reader fails.
The live monthly reset is authoritative; historical exhausted windows can authorize prepaid usage only before their reset or the configured `opencode_monthly_renewal`.
Missing monthly usage cannot authorize a worker.

When the browser pane is unavailable, record a known balance in `~/.local/state/token-ladder/lane-credits.json`:

```sh
plan-pick --set-credit opencode 24.64
```

This records the current UTC time and source `Valentin dashboard screenshot`. Use `--credit-at` for the observation time and `--credit-source` to name a different source:

```sh
plan-pick --set-credit opencode 24.64 --credit-at 2026-11-22T22:00Z --credit-source "Valentin dashboard screenshot"
```

Without live credit, available credit is the anchor less recorded `opencode-go` pi message costs since that time, grouped by run.
Other providers and messages at or before the anchor are excluded. Copied message IDs are counted once.
Launch records are matched to pi runs by directory and launch time. An unmatched launch or a run with missing or invalid costs deducts a conservative $0.50 default, once per run, plus any known costs.
The plan labels anchored balances, the observation time and source, deductions, and the number of defaulted runs. It never estimates costs from token counts.
Anchors older than seven days, future anchors, and invalid balances close prepaid usage. A live read replaces the anchor and starts deductions again from its timestamp.

## Rules

| Rule | Enforced by |
|---|---|
| Use prepaid credit already held down to its floor; never top up automatically. OpenCode stops at $2 until monthly renewal, and a live read above the floor clears that lock. | `PrepaidCreditTests.test_prepaid_credit_floor_is_inclusive_and_configurable`, `test_floor_lock_clears_on_a_live_read_above_the_floor_and_names_the_date`, `test_live_credit_above_the_floor_leaves_a_rate_limit_lock_alone`, `test_live_reader_executes_read_only_commands_and_keeps_zero_credit` in `tests/test_plan_pick.py` |
| Monthly 100% means the included allowance is used up, regardless of weekly and 5-hour readings. Live credit or an anchor no older than seven days can authorize Extra Usage. Deduct run costs, or $0.50 per unknown-cost run. | `AnchoredCreditTests`, `CommandTests.test_set_credit_and_usage_logs_drive_selection_without_browser_credit`, `PrepaidCreditTests` |
| DeepSeek direct is retired; no more top-ups or worker/CI selection. | `PrepaidCreditTests.test_deepseek_direct_is_retired_from_workers_and_ci` |

### Lane history

2026-11-22: retired DeepSeek direct at a -$0.01 balance, with no more top-ups. It previously ran after OpenRouter in the worker order and was also a CI fallback.

## Monthly burn report

```sh
plan-pick --burn
plan-pick --burn --json
```

The report totals this UTC calendar month's recorded pi message costs per lane and per caller directory from `~/.pi/agent/sessions/*/*.jsonl`.
It also counts launches from `~/.local/state/token-ladder/launches.jsonl`.
Each new `--run` attempt records `caller` as `MULTIBOT_SESSION`, or cwd when the name is absent.
Older launches use their recorded cwd. Chat names and directories remain separate when the logs cannot link them.
The report never reads or prints worker briefs, messages, or credentials.

Recorded costs are not invoices or extra-usage charges. They do not distinguish included usage from paid usage.
Missing costs remain unknown, with an unpriced-message count; the report never estimates dollars from token counts.
Codex and Cursor launches without pi cost records show unknown spend rather than zero.

The report also includes Valentin's separate DeepSeek direct account measurement as of 2026-11-22: **$59.93 for 8.11B tokens over 67,963 requests in 30 days**, about **$7.40 per 1B tokens**, approximately 98.7% cached.
This is a measured account total, not a list-price estimate. It is shown separately and never added to the calendar-month pi cost totals.

## Pace log

```sh
plan-pick --pace-log
plan-pick --pace-log --json
```

One line per lane: how many plan-pick launches happened since that plan's own window started, beside the plan's own counter (used share, share of the window gone, and pace).
Launches come from `~/.local/state/token-ladder/launches.jsonl`, one line per `--run` lane attempt.
Counters come from the same pools that `plan-pick` reads, so the two numbers always describe the same plan.
A window that has just reset still shows a pace: the weekly used share is measured against at least 2% of the week, so the first hours after a reset are not blank.
Codex is looked up on `PATH` and in the usual install folders, so a timer or worker shell without `~/.npm-global/bin` still gets the live reading instead of a stale session log.
No tokens are read or printed. Pass a path to `--pace-log` to read a saved launch log instead of the live one.

## Claude emergency allowance

`--claude-helper` refuses while Claude is above its weekly pace. An emergency keeps it saying allowed past that pace until a set time, then it refuses again on its own.

```sh
plan-pick --claude-emergency-until 2026-11-23T23:59:00Z --reason "workers blocked"
plan-pick --claude-emergency-until off
```

The first writes `~/.local/state/token-ladder/claude-emergency.json` with `until`, `reason` and `set_at`. The second deletes the file. The time needs a zone; a missing zone or a missing `--reason` prints a message and exits 2.

While `until` is in the future, `--claude-helper` exits 0 and its line names the time, the reason and Claude's pace. A Claude pool that is used up or OFF still refuses, so an emergency never reopens a spent pool. Once `until` passes, the file counts as gone and `plan-pick` deletes it on the next read, so chats refuse again with nobody turning it off.

## Loop interval

Chats that wake on a timer cost a turn every time they wake, so they ask how long to wait before the next tick:

```sh
plan-pick --loop-interval
plan-pick --loop-interval --json
```

The first prints one line, the seconds to wait and why: `7200 claude pace 0.62x on pace`. The second prints `{"seconds": N, "pace_ratio": R, "used_pct": U, "reason": "..."}`.

It reads the Claude pool the same way `token-ladder pools --json` does, and the thresholds live in `config.json` `loop_interval`:

| Claude pool | Seconds |
|---|---|
| base | 7200 |
| pace above 1.0 | 10800 |
| pace above 1.2, or 95% used or more | 14400 |
| unreadable | 7200, reason `claude pool unknown` |

Never under 1800 seconds. The command always exits 0: a watcher can call it without guarding. Board-drain's watcher calls it for its heartbeat.

## Shared pace warnings

Collection compares usage against elapsed reset-window time.
Claude warns above 1.15x; others above 1.3x.
Both weekly and five-hour bars count.
Fresh Grok reports use the weekly window.
Each brain warns at most every two hours.

Active pane titles identify HT manager sessions.
Only leads that start runners get warnings: Product and Support.
Design, Growth and Finance start none, so they are never pinged.
Delivery uses the existing `session-say` safety checks.
Busy or typed-in prompts never receive keystrokes.
Queued, unavailable and offline recipients also get notes:
`~/poteto-handover/ladder-alerts.md`.
Delivery records live in `~/.local/state/token-ladder/pace-alerts.jsonl`.
Cooldown state uses `pace-alerts.json`, under a collection lock.
No Telegram, resets or automatic session switches.

## Grok reports

A reporting session reads the Grok Bot usage page and records the bar without browser automation or credentials:

```sh
token-ladder grok-set --pct 42 --resets-at 2026-11-01T12:00:00Z
```

The command writes an atomic `grok.json` in the state folder. A stale or invalid report makes Grok unknown; callers that need a number assume 70% used for planning (`unknown_assumed_pct`).

## Historical design (2026-10-10)

### Scope (agreed 2026-10-10)

1. **The reader** shows each pool’s usage/reset/state; consumers ask it and decide for themselves.
2. **The monitor is passive.** It reads the bars, writes the status file and the dashboard, and recommends a mode. It never switches a session, never presses a Codex reset, never turns on OpenRouter.
3. **Hooks** report all three pools, refuse model IDs containing `fast`, and block paid/DeepSeek/router fallbacks; they never block hax merely because of legacy CLAUDE mode.

## The problem

Subscriptions reset on their own clocks. Usage piles onto whichever one is the habit, so one plan runs dry early while another resets with tokens left unused.

Snapshot from 2026-10-10 (read live from the APIs below):

| Plan | Used (weekly) | Resets in | Time gone | Pace |
|---|---|---|---|---|
| Claude Max | 34% | 31 h | 82% | 48 points behind: about 60% will expire unused |
| Codex (ChatGPT Pro) | 60% | 91 h | 46% | 14 points ahead: runs dry about 40 h before reset |

Right call for this week: helpers move from Codex to Claude until Claude catches up, while keeping a floor for orchestrator sessions.

## Data sources (all proven readable on the VPS)

`plan-pick --plans` is the source of truth for what each subscription includes, where it runs and whether it costs extra (from `config.json` `plans`). Read it before quoting a price or asking for a spend cap.

| Source | How | Windows |
|---|---|---|
| Claude | `GET https://api.anthropic.com/api/oauth/usage` with the OAuth token from `~/.claude/.credentials.json` | `five_hour`, `seven_day` (utilization %, `resets_at`) |
| Codex | `rate_limits` in the newest `~/.codex/sessions/**/*.jsonl` token_count event (or `codex app-server` rate limit call) | `primary` weekly (`used_percent`, `resets_at`), credits balance |
| OpenCode Go Plus ($40/month) | `opencode-read --print` and `--credits --print`; `plan-pick --plans` | Separate monthly, weekly and 5-hour windows; $200 included allowance; live or anchored prepaid credit above $2 after included allowance is gone; never top up |
| OpenRouter | `GET https://openrouter.ai/api/v1/key` | dollars spent vs cap (pay as you go, last resort) |
| Cursor | `plan-pick --plans`: Composer 2.5 and Grok 4.7, local and Cursor cloud agents, no extra cost | display only, reserved for the grok bot |
| Google Antigravity | Local history timestamps only, no credentials or quota API | UTC-day history count; quota, limit and pace unknown; shared with chats and the antigravity lane |

## Core idea: pace, not percent

For each window:

- `time_gone = elapsed / window_length`
- `pace = used - time_gone` (negative means behind, tokens about to expire)
- `burnable = 100 - used - reserve` (reserve = orchestrator floor, Claude only)

Ranking rule for helper work:

1. Never OpenRouter automatically. When both subscriptions are out, the ladder asks Valentin (Telegram, yes or no) and waits.
2. Never push a provider whose five-hour window is above 80% (orchestrators would stall).
3. Among the rest, pick the highest urgency: `urgency = burnable points / hours until they expire`. Example 2026-10-10: Claude 51 points in 31 h = 1.6 per hour; Codex reset 100 points in 12 days = 0.35 per hour. Claude wins. Tie goes to Codex.
4. When Claude is chosen, stop once it reaches `time_gone - small margin` or hits the reserve.

### Telemetry pace data

Each account window has `pace` in `status.json`.
Values use quota percentage points, not tokens.

| Field | Meaning |
| --- | --- |
| `per_hour` | Percentage points burned during the last hour. |
| `per_day` | Percentage points burned during the last 24 hours. |
| `ratio` | Used percentage divided by elapsed window percentage. |
| `verdict` | `above` above 1.1; `below` below 0.9; otherwise `on`. |
| `empty_hours` | Remaining quota divided by measured hourly burn. |

Missing or stale observations stay `null`.
Gaps above ten minutes leave affected rates unknown.
Measured zero burn stays `0`.
Zero burn has no exhaustion forecast.
Already empty quotas report `0` hours.
Resets never turn unused quota into burn.
Daily burn may exceed 100% across short-window resets.
Legacy history remains readable.
New history records retain each account's reset timestamp.
Helper guidance still uses percentage-point differences.

## Codex bonus resets (seen 2026-10-10)

The Codex account has 3 "Full reset" credits. Each one refills the 5-hour and weekly limits right away, and each expires if unused:

| Reset | Expires |
|---|---|
| 1 | Oct 22, 22:27 (Berlin) |
| 2 | Oct 29, 19:47 |
| 3 | Nov 7, 00:20 |

This changes the math. Codex running dry before its weekly reset is fine: that is when a reset pays off most. An expired reset is a full week of Codex thrown away.

Reset rules:

1. Use the reset that expires soonest first.
2. Use it when Codex weekly is at 95% or more and the normal weekly reset is more than 24 h away. A reset used just before a normal one is wasted.
3. If a reset will expire within 3 days and Codex is not near full, run Codex harder (Codex mode, more helpers) so the reset is worth using.
4. Pace for Codex counts the remaining resets as extra weeks: capacity until Nov 7 = normal weekly refills + 3.
5. Claude still burns to its own pace target, because unused Claude expires too. The two goals do not conflict: Claude mode only uses spare room inside sessions already running.

To check: whether the reset list can be read (and a reset used) through the Codex login on the server, or only by clicking in the ChatGPT settings.

## Memory constraint (measured 2026-10-10)

The VPS has 94 GB. 28 Claude CLI processes use 10.7 GB (about 380 MB each, up to 690 MB for long sessions). 58 hax and Codex processes use 565 MB in total (about 10 MB each).

So "use more Claude" must never mean "start more Claude processes". Claude mode uses the built-in subagents of an orchestrator that is already running: they live inside that process and start no new one.

## Two modes

**Codex mode (default):** Codex runners start on Sol medium, helpers use Terra medium for code and Luna low for lookups, and Sol high is for hard bugs, security or money only.

**Claude mode (when Claude is behind pace):** each orchestrator keeps working on Opus and hands helper work to its own subagents, using Claude's own ladder of models:

| Rung | Model | Use for |
|---|---|---|
| Advisor | Fable | judgment calls only: before committing to an approach, before saying done |
| Orchestrator | Opus 5.5 | planning, deciding, talking to Valentin |
| Worker | Sonnet 5.5 | implementation, summaries, multi-file reading |
| Scout | Haiku 5.5 | lookups, greps, one-fact questions |

Rules in Claude mode: no new `claude` processes, subagents return short summaries (so the orchestrator stays small), Fable stays rare.

Claude mode ends on its own once Claude is back on pace or reaches the 15% floor, and helpers go back to Codex.

**OpenRouter:** only after Valentin says yes.

## How every session learns the current mode

A prompt hook can report all three pool states. The newest line wins and is passed into every helper prompt.

Every prompt also shows the shared 1Password account budget (`1Password: N/1000 reads today.`), including Client's Token max line via the mirrored status file. Each collect checks the free `op service-account ratelimit` command against the rolling 24-hour cap: at 50%, back off new key lookups and read each key once per job; at 80%, freeze lookups except for live production work and tell Valentin. Failed checks show `1Password: count unknown.`

## Pieces

1. **Collector**: plain script, no LLM, systemd timer every 5 minutes. Writes `~/.local/state/token-ladder/status.json` with every bar and the mode.
2. **Prompt hook**: injects pool states on demand.
4. **Dashboard page on admin.yeoux.net**: renders every plan, pool, worker lane, reset clock, pace, history, and active manual skip (`lane-limits.json`) from the same config and readers `plan-pick --plans` uses, regenerated by the timer and published via `yeoshare`.
5. **Consumers**: dashboards, panels and workers read `pools --json` or `status.json`; plan choice is not made here.

## Open questions

- Does Fable use count against the same weekly Claude bar, or a separate one? The usage API shows no separate bar today; watch it.
- Which accounts count: one Claude and one Codex today, or more.

## Built (v0.1.0, 2026-10-10)

- `bin/token-ladder`: `collect` (timer), `status`, `pools --json`, `grok-set`, `route`, `mode claude|codex|auto`, `hook-prompt`, `hook-bash`. Python 3, no dependencies.
- `config.json`: floor, thresholds, Codex bonus reset expiry dates, and `plans` (what each subscription includes, where it runs and its extra cost; add new plans here).
- `systemd/`: user timer, every 5 minutes (`systemctl --user status token-ladder.timer`).
- Hooks in `~/.claude/settings.json`: prompt = pool states; Bash = no model ID containing `fast`, no paid fallback, no `cc-deepseek-apikey`/`ccr` (`TOKEN_LADDER_DEEPSEEK_OK=1` only after Valentin’s yes). Agent uses its own Claude pool, never legacy CODEX routing.
- State: `~/.local/state/token-ladder/` (`status.json`, `history.jsonl`, `resources.jsonl` (7 days of RAM, load, process weight), `dashboard.html`). Dashboard: https://admin.yeoux.net/p/3320214b/dashboard.html
- Tests: `python3 -m unittest discover -s tests -p 'test_*.py'`, run by CI on every push and pull request.

Measured: one Agent subagent (haiku) started no new `claude` process and added 19 MB to the parent session (444 to 463 MB).

Hooks and consumers read Grok reports written by `grok-set`.

## Server pressure

Each run records free RAM, load and the count and RSS of claude, node and codex processes. The server is "tight" when RAM free is below `tight_ram_free_gb` or the 5-min load is above `tight_load_per_core` times the cores; two runs in a row switch it on, two switch it off. While tight, the prompt hook line tells sessions to start no new helpers or test runs. The mode does not change.

## Two clusters (2026-10-10)

1. **Main ladder** (Hypertask and every other non-Client project): user `valentin` on the primary server (`hypertask-vps`, primary) and the mirror server. Claude and Codex both on the main account. the primary server runs the timer and mirrors the repo and `status.json` to the mirror server after every check; both servers have the hooks and `/token-ladder`. A mode switch on the mirror server is forwarded to the primary server.
2. **Client plan**: user `client` on the mirror server, Claude on user@example.invalid. No ladder, only a pace target: its sessions get a "Token max" line on every prompt telling them how hard to push to use the plan up by its weekly reset.

## Global resets (2026-10-11)

OpenAI resets Codex for everyone often (5 global resets in the 30 days to Oct 11, announced by Tibo, @thsottiaux, usually hours ahead). Unused room is wiped early, so holding Codex back loses value. The ladder reads the free whenaireset.com feed (it tracks the announcements) every 20 minutes:

- **Rate:** the window counts as `h / (1 + rate * h)` hours long, rate = global resets per hour over 30 days. With 5 a month, a fresh 160 h Codex week counts as about 75 h, so Codex gets work sooner.
- **Announced but not landed:** the window ends at the announced time and every session is told to burn that plan now.
- The X API was tried for reading his posts directly; it is pay per use (402 after two calls), so it is not used.

## Session checkpoints

`tl-checkpoint` records a handoff without starting, stopping or switching a session:

```sh
bin/tl-checkpoint --session 'RUNNER 11' --role runner --harness hax \
  --repo "$PWD" \
  --ticket https://app.hypertask.ai/detail/project-4060/790 \
  --next 'Open the focused change for review'
```

Add `--pr https://github.com/OWNER/REPO/pull/NUMBER` when a pull request exists.
The command prints the saved JSON file's path under
`~/.local/state/token-ladder/checkpoints/`. Each record has its own identifier;
concurrent saves never overwrite an earlier checkpoint. Records are published
atomically with owner-only permissions. An invalid request or unreadable Git
revision publishes nothing.

Version 1 records contain the session name, role, harness, ticket URL, optional
pull request URL, next action, UTC save time, and workspace path, revision,
branch and dirty-file status. A detached revision has a null branch. Dirty files
remain in their original worktree; their contents and environment credentials
are never copied into the record. This is a handoff snapshot, not a worktree
backup or an ownership grant.

## Gateway spend report

Run `python3 tools/measure-burn.py --gateway` for the Vercel AI Gateway report without CPU sampling.
The existing full burn report also includes this view.
It reads `AI_GATEWAY_API_KEY`, falling back to `pi auth print-api-key --provider vercel-ai-gateway`.
Keys stay in memory and are never printed.

The report uses `/v1/credits` to derive the monthly Token Pass cycle from `token_pass.resets_at`.
It queries `/v1/report` twice, grouped by model and tag, across the team rather than just the reporting key.
Spend uses `total_cost`, not `market_cost`.
Only `caller=` tags count toward caller totals; `untagged` is the remaining spend without a caller tag.
Each request must carry at most one `caller=` tag.
PR, brief and ticket tags overlap caller tags and are not added again.

The API accepts inclusive UTC dates, not timestamps.
The first date therefore includes spend before the cycle's reset hour.
The report prints both the cycle timestamps and this date-only limitation.
Reports can lag a few minutes.
Malformed reports and unavailable credentials produce an explicit error, never a fabricated zero.
The gateway-only command exits `1` when no report is available.

[Vercel's reporting reference](https://vercel.com/docs/ai-gateway/observability-and-spend/custom-reporting)
documents the API, tags, and pricing.
Reporting requires Pro or Enterprise and a regular AI Gateway key.
Token Pass-scoped keys return HTTP `403` for `/v1/report` even when `/v1/credits` works.
Do not mint a key or upgrade a plan without the owner's approval.
Vercel charges for tag writes and reporting queries.

## CI model calls

GitHub checks that need a hosted chat completion should not call DeepSeek,
OpenCode, the Vercel AI Gateway, OpenRouter, Google Gemini, Cursor, OpenAI or
Anthropic directly. Use the shared lane ladder in this repo instead.

`ci/ladder-call` reads a JSON chat request on stdin (`messages`, optional
`max_tokens` and `response_format`) and `--purpose <label>`. It walks the lanes
in `ci/lanes.json` when the matching key env var is set:
`GEMINI_API_KEY`, `CURSOR_API_KEY`, `AI_GATEWAY_API_KEY`, `OPENCODE_API_KEY`,
`OPENROUTER_API_KEY`. DeepSeek direct is retired, so `DEEPSEEK_API_KEY` is not selected. The `cursor` lane runs
`cursor-agent` headless; the composite action installs it on the runner when
needed.
On payment or quota errors, rate limits, missing models (`404`, `408`, `425`),
5xx, timeouts or empty answers it falls through to the next lane. A `400` that
reports no credit or `no_providers_available` in its error body also falls
through; any other `400` request-body error stops the run. Each skip is logged
on stderr with the status and a short reason (`no credit`, `no provider`), the
provider JSON goes on stdout, and exit `3` means every lane failed.

```yaml
- uses: valentinyeo/token-ladder/.github/actions/ladder-call@main
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
    AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}
    OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  with:
    request-file: request.json
    purpose: pr-verifier
```

Every Vercel ladder call, including a verdict retry, sends the documented `ai-reporting-tags` header.
The caller tag is `caller=<repo>-<job>`, using the repository name without its owner and `github.job`.
Set the optional `caller` action input to override the job slug, for example `verifier` produces `caller=multiprompt-verifier`.
The action adds `pr=<number>` on `pull_request` and `pull_request_target` events.
Direct CLI calls outside Actions default to `caller=token-ladder-cli`.
Slugs accept only letters, digits, underscores and hyphens; long caller tags use a hash suffix within Vercel's 64-character limit.
Never put secrets, prompts, user IDs, branch names or other user data in `caller`.

The plan-pick Vercel pi extension sends `providerOptions.gateway.tags` with `caller=plan-pick-worker`,
`brief=<12-character SHA-256 prefix of the worker directory>` and, when the brief has a `Ticket VSTA-136` line, `ticket=VSTA-136`.
Raw directory names and brief text are never tagged.
Provider pins remain unchanged, and non-Vercel lanes receive no new reporting tags.

### Images

A check that has to look at screenshots passes them through the same action. `images`
takes a comma- or newline-separated list of `.png`, `.jpg` or `.jpeg` files: at most 8,
2 MiB each and 6 MiB in total. `ci/ladder-call --images` and `LADDER_CALL_IMAGES` take
the same list. A list over either cap, a file that is not a PNG or JPEG, or a path
that is not a file exits `2` before any lane is called.

Each file becomes an OpenAI-style `image_url` part holding a `data:` URL, merged into the
last user message of the request. Only lanes marked `"vision": true` in `ci/lanes.json`
receive an image request. Every other lane is skipped with `no vision`, and family
exclusion runs before that, so an excluded vision lane is never called.

Marked lanes, from each provider's own model list on 2026-11-24:

| Lane | Model | Why it can take images |
|---|---|---|
| `opencode-go` | `deepseek-v4.1-flash` | `image/jpeg`, `image/png`, `image/gif`, `image/webp` input |
| `vercel-ai-gateway` | `deepseek/deepseek-v4.1-flash` | `text, image, pdf` input, `vision` tag |
| `vercel-ai-gateway-zai` | `zai/glm-5.3-flash` | `text, image, video` input, `vision` tag |
| `openrouter` | `deepseek/deepseek-v4.1-flash-1017` | model is `text+image->text`; StreamLake publishes no endpoint flag |
| `google` | `gemini-2.5-flash` | OpenAI-compatibility docs show `image_url` data URLs |

`cursor` is not marked: that lane hands `cursor-agent` one text prompt and has no way to
pass a file, so it reads as `no vision` even in a lanes file that marks it.

A lane that answers an image request with `400`, `415` or `422` is skipped with
`<code> with images` and the next lane runs, because that answer says the lane cannot read
the image. A `400` on a request without images still stops the run.

```yaml
      - uses: valentinyeo/token-ladder/.github/actions/ladder-call@main
        env:
          AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}
        with:
          request-file: request.json
          purpose: journey-verifier
          images: |
            artifacts/journey-1.png
            artifacts/journey-2.png
```

`ci/lint-direct-model-calls` scans `.github/` and `tools/` for direct provider
URLs, direct reads of model API key env vars in code, and repo-local model
fallback (HTTP `402`/`429` or credit handling in the same file as a model key
or endpoint). Workflow `env:` lines that only pass keys into `ladder-call` are
allowed. It fails with
`use valentinyeo/token-ladder/.github/actions/ladder-call@main` unless
`.github/ladder-call-allow` lists an unexpired
`<path>:<reason>:<until YYYY-MM-DD>` entry for that file; expired allow lines fail.
The reusable action is
`valentinyeo/token-ladder/.github/actions/lint-direct-model-calls@main`.

`ci/scan-repos` shallow-clones non-archived repos for a GitHub owner, runs
`ci/lint-direct-model-calls`, also flags `generativelanguage.googleapis.com` and
`CLAUDE_CODE_OAUTH_TOKEN` / `CODEX_AUTH_JSON` reads under `.github/` and `tools/`,
lists GitHub secret names per repo, and prints a tab-separated table including
whether each repo already references `ladder-call`.

## CI agent runs

Use `ladder-agent` instead of `claude-code-action` when a check needs tools,
child agents or a multi-step journey. `ladder-call` remains the route for a
single chat completion.

Replace the Claude action step with this example. Keep your checkout and any
project setup steps before it. No Claude or Codex plan login is needed.

```yaml
- uses: actions/checkout@v4
- name: Agent verification
  id: agent
  uses: valentinyeo/token-ladder/.github/actions/ladder-agent@main
  env:
    OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}
    AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  with:
    prompt: |
      Read AGENTS.md. Inspect the proposed change, run the verification
      journeys, and report PASS or FAIL with evidence. Use tools to check
      your claims. If needed, delegate focused tasks with pi -p.
    working-directory: .
    exclude-families: ${{ env.EXCLUDE_FAMILIES }}
    timeout-seconds: '1800'
- name: Read final answer
  env:
    ANSWER_FILE: ${{ steps.agent.outputs.answer-file }}
  run: cat "$ANSWER_FILE"
```

The action installs `@mariozechner/pi-coding-agent@0.73.1` on Node 22 and runs
`pi -p --mode json` in the supplied directory. Built-in tools, repository
skills and Pi extensions remain enabled. Translate Claude-specific tool or
subagent instructions to Pi equivalents when moving a prompt.

It tries keyed lanes in this order: `opencode-go`, `vercel-ai-gateway`
(DeepSeek), `vercel-ai-gateway-zai` (GLM), then `openrouter`. Models and provider
pins come from `ci/lanes.json`. Both Vercel lanes stay on Token Pass;
OpenRouter stays on StreamLake with provider fallback disabled.
Missing keys, exhausted credit, rate limits, unavailable providers, timeouts
and empty final answers move to the next lane. Structured provider failures
count even if Pi exits zero. Work errors and bad requests stop the action.
Each retry starts a fresh conversation but keeps workspace changes, so use
retry-safe prompts for tasks with external effects.

`exclude-families` accepts the same comma-separated family names as
`ladder-call`. When omitted, it inherits `EXCLUDE_FAMILIES` from the job.
Exclusions apply before any provider call. Pi uses an isolated agent directory
without saved plan logins. Child `pi` processes inherit its lane, model and
provider pins. Pi's internal provider retries are disabled so the ladder can
advance. `timeout-seconds` limits each lane, including tools and child agents.

Outputs are `lane`, `model`, `exit-code` and `answer-file`. The answer file is
UTF-8 text containing only the final assistant answer, not tool events or
intermediate narration. Files have unique paths under `RUNNER_TEMP`.
On failure the file is empty. Exit `3` means no lane answered; another nonzero
exit reports an agent or setup failure. `lane` and `model` identify the last
attempt on failure, or are empty if no lane was attempted. A successful agent
process does not imply its verdict is PASS; the calling check must interpret
the answer.

The `ladder agent composite action` CI job runs the real pinned Pi against a
streaming stub provider. It checks lane fallback, file tools, a child agent,
provider pins, family exclusions and separate output files without live keys.
`lint-direct-model-calls` accepts `ladder-agent` as a ladder route.

## Adding the scan to a repo

Any repo can run this scan on its own pull requests, so a new direct call fails
that repo's checks instead of waiting for the next inventory run.
`docs/direct-calls-2026-11-24.md` records where the scan stands across the repos.

1. Add the step after `actions/checkout`. The step scans the workspace, so the
   checkout must come first.

   ```yaml
         - uses: actions/checkout@v4
         - name: No direct model calls
           uses: valentinyeo/token-ladder/.github/actions/lint-direct-model-calls@main
   ```

2. Point it at another directory with the `repo` input. It defaults to `.`:

   ```yaml
           with:
             repo: services/worker
   ```

3. Allow one file that must keep a provider URL. Put a reason and an end date
   in `.github/ladder-call-allow`, one line per file:

   ```text
   tools/credits.py:reads provider balances only:2027-01-31
   ```

   An expired line fails the check, as does a line without a date.

4. Run it locally before pushing. From the repo you want to check:

   ```sh
   python3 ../token-ladder/ci/lint-direct-model-calls .
   ```

   From a directory outside both repos:

   ```sh
   ci/lint-direct-model-calls /path/to/repo
   ```

The action runs `ci/lint-direct-model-calls`, not `ci/scan-repos`. It reads
`.github/` and `tools/` only, so a call in application code underneath `src/`
stays invisible. `@main` means a change here reaches every repo on its next run;
pin a commit SHA if a repo needs a fixed version.
