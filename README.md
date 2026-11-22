# token-ladder

Token Ladder reads subscription usage for Claude, Codex (including `hax`), Grok and Google Antigravity. Google is a chat-only pool, never a worker lane.

`collect` runs on a timer and writes the state. `pools --json` prints the current pool states for any caller. `grok-set` records a Grok reading. Nothing here chooses a plan, reserves a pool, launches a session or holds a lease: those rules live in `spawn-worker` code and in the valentin-mode skill.

## Install

Put this checkout's `bin` directory on PATH using your normal shell configuration; the program makes no settings changes by itself.

The systemd user units in `systemd/` run `collect` every five minutes. Copy them to `~/.config/systemd/user/`, then `systemctl --user enable --now token-ladder.timer`. `pools` reads the `status.json` that this timer refreshes.

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

`plan-pick` runs a background worker on the first lane in `config.json` `lanes` that is free.

The Vercel AI Gateway lane spends Token Pass credit only, and the OpenRouter lane uses one upstream provider, so neither can fall to Baseten when its credit runs low.
`ci/lanes.json` holds both pins: `gateway` on the gateway lane, `provider` on the OpenRouter lane.
`ci/ladder-call` sends them as `providerOptions.gateway` and `provider` in the request body.
The two pi lanes on those providers load `tools/pi-lane-pins.mjs` with `-e`, and that extension writes the same two fields from the same file through pi's `before_provider_request` hook.
Change the pin in `ci/lanes.json` and both paths follow.

## Google Antigravity usage

`plan-pick --plans` shows the `google` pool as chat-only with used, limit and pace fields. `--plans --json` adds a `usage` object to its plan. `token-ladder pools --json` also includes `google`. Worker selection rejects this pool even if a local config adds a lane or burn override for it.

The read-only fallback counts `timestamp` metadata in `~/.gemini/antigravity-cli/history.jsonl` from midnight UTC through now. These timestamps are milliseconds since the Unix epoch. The count covers local history entries, which can include slash commands, not model requests, tokens or quota consumption. Other devices, retries, tool calls and unrecorded chats are not counted. The reader never opens credential or token files, calls a quota endpoint, signs in, buys overages or runs an agent.

Missing, unreadable, empty or wholly invalid history produces unknown usage. Malformed records and future timestamps are ignored. An older valid history produces zero local entries today, not proof of zero account usage. Quota percentage, limit, reset and pace remain unknown even when a local count is available. A missing Google reading never stops `plan-pick`.

### Quota source investigation, 2026-11-22

- `agy --help` lists no status or usage subcommand. No prompt was run.
- Local cache metadata, state field names and SQLite schemas expose conversation metadata, not a quota snapshot. CLI logs refer to `retrieveUserQuotaSummary` but contain no usable quota reading. Credential and token files were not opened.
- [Google's Antigravity plans](https://antigravity.google/docs/plans.md) describes Pro and Ultra quotas refreshing every five hours, with weekly limits. Quota depends on work and available capacity. Google publishes **no numeric daily Antigravity limit** there. Account-specific quota is shown on the app's Settings page, which this reader does not access.
- The [Gemini CLI quota table](https://geminicli.com/docs/resources/quota-and-pricing/) lists daily request caps of 1,000 for individuals, 1,500 for AI Pro and 2,000 for AI Ultra. These are Gemini CLI limits, not an Antigravity daily allowance. Applying 1,500 to this pool would invent a limit. The exact subscribed tier is also unconfirmed.

## OpenCode monthly allowance

OpenCode can report its monthly meter at 100% while the 5-hour and weekly windows still have room.
`plan-pick` keeps the OpenCode lane ON then, and moves to the prepaid credit pool only when the 5-hour or weekly window is full or at or above `opencode_monthly_slack_pct`, or when either reading is missing.
That key lives in `config.json` and defaults to 25.

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
| OpenRouter | `GET https://openrouter.ai/api/v1/key` | dollars spent vs cap (pay as you go, last resort) |
| Cursor | `plan-pick --plans`: Composer 2.5 and Grok 4.7, local and Cursor cloud agents, no extra cost | display only, reserved for the grok bot |
| Google Antigravity | Local history timestamps only, no credentials or quota API | UTC-day history count; quota, limit and pace unknown; chat-only |

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
4. **Dashboard page on admin.yeoux.net**: all bars, reset clocks, pace and history, regenerated by the timer.
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

## CI model calls

GitHub checks that need a hosted chat completion should not call DeepSeek,
OpenCode, the Vercel AI Gateway, OpenRouter, Google Gemini, Cursor, OpenAI or
Anthropic directly. Use the shared lane ladder in this repo instead.

`ci/ladder-call` reads a JSON chat request on stdin (`messages`, optional
`max_tokens` and `response_format`) and `--purpose <label>`. It walks the lanes
in `ci/lanes.json` when the matching key env var is set:
`GEMINI_API_KEY`, `CURSOR_API_KEY`, `AI_GATEWAY_API_KEY`, `OPENCODE_API_KEY`,
`DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`. The `cursor` lane runs
`cursor-agent` headless; the composite action installs it on the runner when
needed.
On payment or quota errors, rate limits, missing models (`404`, `408`, `425`),
5xx, timeouts or empty answers it falls through to the next lane; a `400`
request-body error stops the run. Each skip is logged on stderr, the provider
JSON goes on stdout, and exit `3` means every lane failed.

```yaml
- uses: valentinyeo/token-ladder/.github/actions/ladder-call@main
  env:
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    CURSOR_API_KEY: ${{ secrets.CURSOR_API_KEY }}
    AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}
    OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}
    DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  with:
    request-file: request.json
    purpose: pr-verifier
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
