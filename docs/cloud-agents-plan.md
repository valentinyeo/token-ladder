# Cloud agents: moving workers off the VPS

Why: one VPS runs ~25 chats plus every code worker, and it overloads. We already pay for Claude, ChatGPT (Codex) and Cursor, and all three include cloud agents that run on the vendor's machines.

Researched 2026-11-20. Facts marked (checked) were run or read on this server. The rest come from vendor docs and are unverified until a lane runs.

## What pstack (Lauren Tan / poteto) intends

- Parallel workers each get their own machine: "Give each agent its own worktree, or run them as cloud agents, which each get their own machine." (pstack `skills/poteto-help/SKILL.md:145`, checked)
- `swarm` spawns workers with `environment: "cloud"` by default, and uses `local` only when a worker needs the user's computer (`skills/swarm/SKILL.md:27`, checked).
- The Claude Code adapter maps `environment: "cloud"` to a local worktree, and skips steps that need Cursor cloud agents (`docs/harness/claude-code.md:24,61,67`, checked). **On our setup, pstack's "cloud" therefore lands on the overloaded VPS.** That is the gap.

## What we have

- `plan-pick` has six lanes: vercel, deepseek, opencode, codex, cursor and openrouter. Every lane runs a blocking **local** command. There is no cloud lane.
- CLIs are installed and logged in: claude 2.1.329, codex-cli 0.166.0 and cursor-agent 2026.11.19 (checked).
- `codex cloud exec|status|list|diff|apply` exists (checked). One cloud task already ran for this repo: "Add routing decision for cloud roles", Nov 8, READY, +95/-3, never applied (checked). The link is https://chatgpt.com/codex/tasks/task_e_6af0b6c226208326ad137036b93bf744
- `~/.local/bin/claude-cloud-task` wraps `claude --cloud`. Many Claude cloud sessions already exist on the account.

## Per vendor

| | Launch | Results | Notes |
|---|---|---|---|
| Codex cloud | `codex cloud exec --env <ENV_ID> [--branch B] "<prompt>"`, then `status` | `codex cloud diff` / `apply`, or ask the task to open a PR | Shares the ChatGPT plan allowance. The environment is set up once per repo in the web UI. |
| Cursor cloud | REST `POST /v1/agents` with `repos[0].url`, `startingRef`, `model.id`, `autoCreatePR` | Poll the run. The PR link is in `git.branches[].prUrl` | Needs a Cursor API key. Billed at API rates against a spend limit. About 8 run at once (forum post, unverified). |
| Claude cloud | `claude --cloud "<task>"` (clones the pushed branch) | It pushes a branch, so watch `gh pr list` | The repo must have the Claude GitHub App. It counts toward plan limits. |

Docs: https://learn.chatgpt.com/docs/cloud , https://cursor.com/docs/cloud-agent/api/endpoints , https://code.claude.com/docs/en/claude-code-on-the-web

## Plan

1. **Where work goes.**
   - Code workers go to the Codex cloud and Cursor cloud lanes. Per `pstack-models.md`, code workers never run on Claude.
   - Claude cloud is only for Claude-native jobs, and only when `plan-pick --claude-helper` allows it.
   - Reviews and verifiers stay on GitHub runners.
   - Chats Valentin talks to stay on the VPS, and so does anything that needs the QA phone, the local app or board tokens.
2. **New lanes in `plan-pick`.** Add `codex-cloud` and `cursor-cloud`, then `claude-cloud`. Each wrapper blocks until the task is done, so `--run` and the lane fallback keep working. Each wrapper does five things:
   1. Push a work branch.
   2. Launch the task.
   3. Poll it.
   4. Print the PR or branch URL.
   5. On a limit, exit nonzero with "429" text so the next lane runs.
3. **Briefs must stand alone.** A cloud machine cannot read `brief.txt` or the server's files, so the brief carries everything the worker needs, or it is committed to the branch.
4. **Prefer cloud lanes when the server is busy.** When load is high, cloud lanes go first, because they cost no VPS CPU.

## Blockers

- The repo must be on GitHub, with the vendor's GitHub app or connector installed.
- Codex needs one cloud environment per repo, created in the web UI.
- Cursor needs an API key and a spend limit. That is a money and keys call for Valentin.
- No usage reader exists yet for Codex cloud or Cursor cloud allowances.

## First steps

1. Build `codex-cloud` first. Everything it needs is already working (checked). Test it on one small token-ladder ticket.
2. Review and apply or discard the Nov 8 Codex task above.
3. Build `cursor-cloud` after Valentin approves a Cursor API key and spend limit.
