# Google tokens (Q3) and cheaper plans (Q5), 2026-11-22

Account update, 2026-11-22 ~22:00Z: the dashboard shows $24.64 available prepaid credit, monthly 100% used, weekly 0%, and 5-hour 0%. Monthly 100% exhausts the included allowance. Valentin's later rule supersedes the earlier skip-all-extra-usage recommendation below: use held prepaid credit above $2, then wait until the 30 November renewal; auto top-up is OFF and must never be enabled. Read credit live, never extrapolate stale readings. DeepSeek direct is retired at -$0.01, with no more top-ups. Its measured last-30-day cost was $59.93 for 8.11B tokens over 67,963 requests, about $7.40 per 1B tokens, ~98.7% cached. Earlier price comparisons below remain research history.

Research only. No keys read or used, no spend, no config changed. All pages were read on 2026-11-22 unless a date is given. Prices are USD per 1M tokens. "Cached" means a cache-hit read of input. Earlier DeepSeek findings are in `deepseek-flash-prices-2026-11-22.md` and are not repeated.

## 1. Top line

**Q3, Google: do not wire Google in as a worker lane. Wire it as a CI review judge only, on a paid, prepaid AI Studio key, model `gemini-3.5-flash-lite`.** Gemini costs 4 to 10 times more than DeepSeek Flash per worker run because our work is 98.7% cached reads and Google's cached price is 8 to 25 times DeepSeek's. A Google judge is still worth having because it is a different vendor than DeepSeek, so `plan-pick --not-vendor deepseek` has someone to hand reviews to. There are no usable free Gemini tokens for private code: the free tier trains on your data, and the Google Cloud trial credit no longer covers Gemini.

**Q5, cheaper plans: no plan is clearly cheaper per real worker run than what we have.** Plain OpenCode Go and CommandCode GOAT are about $0.003 per median run if their $100 allowances are fully used. Our plan is OpenCode Go Plus ($40/month, $200 DeepSeek allowance), about $0.007 per median run, like Vercel Token Pass. Valentin confirmed Go Plus; its included allowance is currently used up. Everything else is the same price or worse, or forbids headless use. Add CommandCode GOAT only as extra capacity (a third monthly allowance), not as a saving.

## 2. What a worker run costs (our measured mix)

Median run from the earlier research (769 sessions): 68K new input, 2.79M cached reads, 24K output, 52 turns. Heavy run (p90): 194K new, 20.3M cached, 87.5K output.

| Model and route | New in | Cached | Out | Median run | vs DeepSeek | If the cache does NOT hit (all input at new price) |
|---|---|---|---|---|---|---|
| DeepSeek V4.1 Flash, list off-peak | 0.15 | 0.003 | 0.60 | **$0.033** | 1.0x | $0.44 |
| Gemini 2.5 Flash-Lite (deprecated, reroutes Dec 1) | 0.10 | 0.01 | 0.40 | $0.044 | 1.3x | $0.30 |
| Gemini 3.1 Flash-Lite | 0.25 | 0.025 | 1.50 | $0.123 | 3.7x | $0.75 |
| **Gemini 3.5 Flash-Lite** | 0.30 | 0.03 | 2.50 | **$0.164** | 4.9x | $0.92 |
| Gemini 3.8 Flash (to double on 2027-01-01) | 0.75 | 0.075 | 3.75 | $0.35 (then $0.70) | 10.5x (21x) | $2.23 |
| GLM 5.3 Flash (Z.ai, Vercel and Synthetic list) | 0.15 | 0.03 | 0.50 | $0.106 | 3.2x | $0.44 |
| Synthetic subscription deduction rate, DeepSeek V4.1 Flash | 0.06 | 0.003 | 0.30 | $0.020 | 0.6x of list (but charged against a weekly credit) | $0.18 |
| Our OpenCode Go Plus effective, DeepSeek Flash ($200 for $40) | | | | about $0.007 | 0.2x | |
| CommandCode GOAT effective ("up to $100" for $10 plus fee) | | | | about $0.003 | 0.1x | |
| Vercel Token Pass effective ($150 for $30) | | | | about $0.007 | 0.2x | |

Arithmetic: median = 0.068 x new + 2.79 x cached + 0.024 x out (prices per 1M). Gemini output includes thinking tokens, which Google bills as output ("every token the model processes is counted and billed"), so the Gemini output column is a floor.

Cache warning for Gemini (not verified): the Google pages I read document explicit caching (`cached_content` through the OpenAI-compatible endpoint, plus a storage fee of $0.50 to $1.00 per 1M tokens per hour). I could not confirm that implicit cache hits happen through pi's `google` provider for 3.x models. If they do not, a median run costs $0.92 on 3.5 Flash-Lite, 28 times DeepSeek. Test before trusting the $0.164.

## 3. Q3, Google offers

### 3.1 Which Google route gives free or included tokens

Every route either trains on your data, is closed to Gemini, or cannot run in CI.

| Offer | What you get | Private code OK? | Headless or CI? | Verdict |
|---|---|---|---|---|
| Gemini API free tier (AI Studio key) | Flash and Flash-Lite models plus Gemma 4. Reported: Gemini 3.8 Flash 5 RPM, 250K TPM, **20 requests per day**; Flash-Lite about 500 per day. Google no longer prints per-model free numbers on its docs page (only the AI Studio console shows them). Limits are per project, reset midnight Pacific. | **No.** Pricing page marks free-tier data "used to improve products: Yes", paid "No". Third-party guide adds "human reviewers may read them". | Yes, plain key | Public repos only. 20 requests per day cannot run a worker (one run is 52 turns). |
| Gemini API paid tier (prepay or postpay) | Same models, real limits, below | Yes, paid data is not used to improve products | Yes | **The usable route.** |
| Vertex AI (now called Gemini Enterprise Agent Platform) | Same models, Google Cloud terms | Yes | Yes, with a service account or Workload Identity | Needs real billing. $300 trial credit **excludes Gemini**: "Gemini API usage excluded from the $300 trial" since March 2026, and Vertex generative services excluded for sign-ups or upgrades on or after 2026-11-16. Price snapshots conflict ($0.30 vs $0.54 input for 3.5 Flash-Lite); not resolved. |
| Vertex express mode | 90-day free tier, `@gmail.com` accounts, Pre-GA. Quotas listed only for older models (10 RPM for 2.5 family). | Data use not stated | Yes, API key header | Not worth the risk. |
| AI Studio (the web app) | Same as API; shares the API terms | as above | n/a | Nothing extra. |
| Google AI Pro ($19.99), Ultra 5x ($99.99), Ultra 20x ($199.99 or $249.99, sources conflict) | Pro or Ultra raises daily limits: Gemini CLI quota table says 1,000 (Code Assist individual), 1,500 (Pro), 2,000 (Ultra) per day, 60 per minute. **But** the same page says "Gemini CLI was replaced by Antigravity CLI on June 18th, 2026" for Google One users. Antigravity: quota refreshes every 5 hours up to a weekly limit, overage by buying credits; forum reports of hard caps (about 200 per day) after May 2026, unconfirmed by Google. | Subscription terms, not API terms | **No for CI.** No GitHub Actions path with a personal login. `agy -p` headless is only forum-sanctioned, and several forum threads report 403 "ToS violation" bans for scripted or third-party-tool use of personal OAuth (Claude Code, OpenClaw, OpenCode named as not allowed). pi would count as a third-party tool. | Skip. Do not point pi at a subscription login. |
| Gemini Code Assist Standard or Enterprise (Workspace licences) | 1,500 or 2,000 requests per user per day | Yes | CI via the `run-gemini-cli` GitHub Action (auth options: API key, Vertex, Workload Identity, or Code Assist) | Needs seat licences; not what Valentin described. |
| Google for Startups Cloud Program | Up to $250K year one on the AI tier plus a year-two share; covers Gemini and Gemma, not partner models; needs VC funding and is at Google's discretion | Yes | Yes (Vertex) | Only if inne or Hypertask qualifies. Not checked. |
| `opencode-go` already lists `gemini-4-argon` in pi | Reachable today on a plan we pay for | Not checked (Go data terms not read) | Yes | The only Google model already available. Gemini 4 Argon list price is $2 / $0.10 / $10 and doubles on Dec 1, so not a worker model. |
| CommandCode `/provider/v1/models` (public list, 104 models) | Lists `google/gemini-3.5-flash-lite`, 3.8 Flash, 4 Argon and more | n/a | Yes | Per-model GOAT allowance for Gemini not shown on the pricing page. |

**"Valentin's Google tokens" is ambiguous. What it means decides the route:**
- If it is a Google AI Pro or Ultra subscription: it cannot be used from pi or GitHub Actions safely. Leave it for interactive Antigravity.
- If it is a free AI Studio key: use it only for public repositories (his repos are open source by default). For private code the free tier trains on the prompt.
- If it is prepaid AI Studio credit or old Cloud credit that predates 2026-03-02: that is the paid tier (no training). Use it for CI judges.

### 3.2 Gemini API paid price table (read 2026-11-22, standard tier)

| Model | In | Cached (plus storage) | Out | Free tier | Notes |
|---|---|---|---|---|---|
| Gemini 3.5 Flash-Lite | 0.30 | 0.03 (+$1.00 per 1M per hour) | 2.50 | yes, trains | Released 2026-07-21. No scheduled price change. |
| Gemini 3.1 Flash-Lite | 0.25 | 0.025 | 1.50 | yes, trains | Older, weaker at coding. |
| Gemini 2.5 Flash-Lite | 0.10 | 0.01 | 0.40 | yes, trains | Deprecated; reroutes to 3.5 Flash-Lite on 2026-12-01. |
| Gemini 3.6 and 3.8 Flash | 0.75 now, 1.50 from 2027-01-01 | 0.075 (0.15) | 3.75 (7.50) | yes, trains | Strongest cheap Gemini for coding, 2.5x the price of Flash-Lite. |
| Gemini 4 Hydrogen | 1.50 | 0.075 | 7.50 | yes, trains | |
| Gemini 4 Argon, Carbon | 2.00 (4.00 from 2026-12-01) | 0.10 (0.20) | 10 (20) | no | |
| Gemini 3.1 Pro Preview | 2.00 / 4.00 over 200K | 0.20 | 12 / 18 | no | |

Free-tier models, rate limits, tiers (from the rate-limits page): Free: no spend needed. **Tier 1: link a billing account** (limits per model are in the AI Studio console; the one model table printed, "Gemini Decision 1", shows Free 200 RPM / 50K TPM / 10K RPD, Tier 1 1,500 RPM / 1M TPM / 50K RPD; Tier 2 and 3 not captured). **Tier 2: $100 cumulative spend plus 3 days. Tier 3: $1,000 plus 30 days.** Monthly spend caps per billing account: Tier 1 $250, Tier 2 $2,000, Tier 3 $20,000+. Spend-based limit in a rolling 10 minutes: Tier 1 $10, Tier 2 and 3 $200. Prepay credits: $5 to $5,000, expire after 12 months, **a $0 balance returns HTTP 402** (not 429) across all linked projects. Source: https://aistudio.google.com/docs/billing

### 3.3 Best Gemini for coding at the lowest price, against DeepSeek V4.1 Flash

Pick: **Gemini 3.5 Flash-Lite** (`gemini-3.5-flash-lite`). Over 3.1 Flash-Lite because Google's own tables show Terminal-Bench 2.1 54.0 vs 31.0 and SWE-Bench Pro 54.2 vs 38.3 for $0.04 more per median run. Over 3.8 Flash because 3.8 Flash is 2x the price per run now and 4x from 2027-01-01 for 5 points of SWE-bench.

| Benchmark (publisher) | Gemini 3.5 Flash-Lite | Gemini 3.8 Flash | DeepSeek | Note |
|---|---|---|---|---|
| SWE-bench, mini-SWE-agent (Vals, updated 2026-09-01, archived) | 75.0%, $0.31 per test | 80.0%, $2.19 per test | **V4 Flash 0731: 88.8%, $0.01 per test** | Vals has **no entry for V4.1 Flash**; 0731 is the closest. Vals says it no longer runs this benchmark on new releases. |
| SWE-Bench Pro public (Google) | 54.2% | not published | not published for V4.1 Flash | Different benchmark, do not compare with Vals SWE-bench. |
| Terminal-Bench 2.1 (Google) | 54.0% | not published | not comparable | |
| Terminal-Bench 4.0 (Vals, V4.1 Flash 1017 vs 3.8 Flash page) | not on that page | 19.70% | 31.82%, $0.54 per test (vs $8.65) | Page read through a summariser; the 19.70 figure also appears for the older 0910 snapshot elsewhere, so treat the Gemini value as unconfirmed. |
| Vibe Code Bench v1.1 (same page) | n/a | 78.65%, $6.87 per test | 87.73%, $0.58 per test | |
| Code Migration v1.1 (same page) | n/a | 38.65% | 67.52% | |
| DeepSWE v1.1 (DeepSeek's own report, TNW says unverified) | n/a | n/a | 74.2% (65.5% to 74.2% by scaffold) | |

Reading: DeepSeek V4.1 Flash beats Gemini 3.8 Flash on every coding benchmark both were run on, at about one tenth of the cost per test. Gemini 3.5 Flash-Lite is a reasonable judge, not a worker.

### 3.4 How to add Google as a lane

Verified in this checkout: pi already has built-in providers `google` (key `GEMINI_API_KEY`) and `google-vertex` (key `GOOGLE_CLOUD_API_KEY`, or Application Default Credentials with `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`). They do not show in `pi --list-models` only because no key is stored. pi's catalog has `gemini-3.5-flash-lite` at 0.30 / 0.03 / 2.50 for both providers, so it matches the pricing page.

**pi (worker command, if you ever want it anyway):**
```
~/.npm-global/bin/pi -p --thinking medium --provider google --model gemini-3.5-flash-lite "$(cat brief.txt)" </dev/null
```
Use the explicit id, not `gemini-flash-latest` or `gemini-flash-lite-latest` (aliases move and change price). Store the key with `pi auth` or export `GEMINI_API_KEY`; this research did not touch any key.

**ci/lanes.json (judge lane):**
```
{ "name": "gemini", "key_env": "GEMINI_API_KEY",
  "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
  "model": "gemini-3.5-flash-lite" }
```
Checked against `ci/ladder-call`: it sends `Authorization: Bearer <key>` (Gemini's OpenAI endpoint accepts that), sends only the caller's body plus `model` (no provider-routing fields to reject), and already skips on 401, 402, 403, 404, 408, 425, 429 and 5xx, so a $0 prepay balance (402) and rate limits both fall through to the next lane. Put it **last** among the DeepSeek lanes if the goal is a cheap fallback, or **first** when the author vendor is DeepSeek and the review must come from another vendor. Note Google calls the OpenAI endpoint "beta". Two things to test once: `reasoning_effort` mapping (`none` only works on 2.5 models), and that `usage` returns cached-token counts.

**config.json (plan-pick), suggested lane entry for a judge-only lane:** `"lane": "google", "credit": "google", "vendor": "google", "pool": null`, command with `--provider google --model gemini-3.5-flash-lite`, and a plan entry `"Google Gemini API (paid, prepaid)"`, `included_models: ["Gemini 3.5 Flash-Lite"]`, `extra_cost: "prepaid credit"`. plan-pick's credit readers (`BALANCES`) cover only deepseek, vercel, openrouter. Google has **no balance API I could find**, so the lane would show "no usage reader, a 429 or empty credit marks it limited". plan-pick's `CREDIT_ERROR` regex already matches "402" and "payment required", so a $0 prepay balance will mark the lane limited. Do not add the lane to the worker ladder (`vendor: google` with a worker command), or plan-pick could send 52-turn runs there at 5x the price.

**Limits plan-pick should respect (paid tier):**
- Tier 1 (billing linked): per-model RPM, TPM, RPD in the AI Studio console (not on the docs page). Per project, not per key. RPD resets at midnight Pacific.
- Tier 1 spend cap $250 per month per billing account; $10 spend limit per rolling 10 minutes.
- Prepay: $0 gives HTTP 402. Credits expire after 12 months.
- Free tier (public repos only): about 20 requests per day on 3.8 Flash, about 500 per day on Flash-Lite, reported by forum posts, not Google's docs.
- 3.5 Flash-Lite has no scheduled price change; 3.8 Flash doubles on 2027-01-01.

## 4. Q5, plans compared

Test used: (1) what the plan charges for a cache read, (2) whether the terms allow an automated agent through pi or a plain API key, (3) caps against our fleet need of about $330 a month of DeepSeek Flash at list (about 42B tokens a month, almost all cached reads).

| Plan | Price | Per median run for our mix | Monthly size vs fleet need | Terms for automated or headless use | Base URL | Verdict |
|---|---|---|---|---|---|---|
| **OpenCode Go** (not our plan) | $10 | about $0.003 if the $100 DeepSeek allowance is used | $100 list = 30% of the fleet | Built for "OpenCode and other coding agents"; wants typical agent traffic, its own user agent and a stable `x-opencode-session` header; pi is on the supported list. Abuse monitored. Automated use not addressed directly. | `https://opencode.ai/inference/go` | Cheapest, keep as first. |
| **OpenCode Go Plus** (we have it) | $40/month | about $0.007 if fully used | $200 DeepSeek allowance, confirmed by Valentin on 2026-11-22; not $400 | same | same | Included allowance is used up. Skip for workers; never use paid extra usage. |
| **CommandCode GOAT** | $10 plus an unstated processing fee | about $0.003 if "up to $100" is delivered | $100 list. 5-hour cap up to $20, weekly cap up to $50 | API access is included and documented ("any OpenAI or Anthropic client or agent can use the API", headless mode page exists). No automation ban found; Legal page not read. | `https://api.commandcode.ai/provider/v1` (`/chat/completions`, `/responses`, `/messages`, `/models`). Bearer key from Studio. `/models` answers without a key. | Ties Go. Worth adding as a third allowance. DeepSeek ids it lists: `deepseek/deepseek-v4.1-flash` ("latest", snapshot not stated) and `-fast`, `-ultrafast` variants (do not use, priced higher). GLM-5.3 Flash also $100. Go tier ($1) has no API access. |
| Vercel Token Pass (we have it) | $30 | about $0.007 | $150 list | ZDR, documented gateway | `https://ai-gateway.vercel.sh/v1` | Keep. Fix the model id first (see earlier file). |
| Cursor (we have it) | plan | included models only | n/a | CLI login | n/a | Not a DeepSeek route. |
| Codex, OpenRouter, DeepSeek direct | | $0.033 and up for DeepSeek direct | | | | Covered in the earlier file. |
| **Synthetic.new** | Go $9, Standard $30, Big $90, Bigger $300 | $0.020 (subscription deduction rate 0.06 / 0.003 / 0.30) | weekly credit only, dollar size per plan **not published** on pages I could read. A model table lists DeepSeek V4.1 Flash 2.70B tokens a month (equal to $251 of API list), plan not stated | Terms not found. Third-party: `https://api.synthetic.new/openai/v1` (secondhand) | | Cached rate equals DeepSeek list, so the saving is only on input and output. "Infinite mode" (does not count against limits, slower, DeepSeek V4.1 Flash only, 5x concurrency) is a lead worth one $9 trial, not a pick. Weekly-credit size unknown. |
| **Z.ai GLM Coding Plan** | Lite $18 (Pro and Max prices not shown) | GLM 5.3 Flash about $0.106 at list token rates | Credits: Lite 2,000 per 5 h and 10,000 per week; Pro 12,000 / 60,000; Max 28,000 / 140,000. Off-peak half price. Token-to-credit rate not shown. | **Restricted:** "strictly limited to use within officially supported tools and products". Lists ZCode, Claude Code, Codex, OpenCode, Cline and others; pi not confirmed on the list. Violations can freeze or ban the account (3 strikes). Scripts not addressed. | OpenAI `https://api.z.ai/api/coding/paas/v4`, Anthropic `https://api.z.ai/api/anthropic`. Coding-plan key is separate from the normal API key. | Flag. Not for CI judges. Quality is good (GLM 5.3 Flash 92% on Vals SWE-bench at $0.02 per test) but we already get GLM through Go, GOAT and Token Pass without the restriction. |
| **Moonshot Kimi Code** | China site: Go ¥49, Plus ¥99, Pro ¥199, Max ¥699, Ultra ¥1,399 per month; third-party USD $19 to $199 | not computed (pay-go price not verified) | Rolling 5-hour limit, no weekly limit on new plans, monthly pool shared with the whole Kimi app. Go has no coding quota. | **Restricted:** official tools only (Kimi Code CLI, Claude Code, Roo Code, Codex, OpenCode, Hermes); "using the key on tools not listed may be considered abuse". | `https://api.kimi.com/coding/v1` (third-party source) | Flag. Skip. |
| **Alibaba Qwen Token Plan** (the Coding Plan is being replaced) | Personal $8 / $16 / $25 / $80 per month (promo $6 / $10 / $18); old Coding Plan Pro $50 with 6,000 per 5 h, 45,000 per week, 90,000 per month | credits, no per-run figure | | **Restricted:** docs say "for interactive use in compatible AI tools"; a self-built tool that does not use the standard API cannot use the plan key. Singapore only. Keys `sk-sp-`. | `https://token-plan.maas.qwencloudapi.com` (docs) or `https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` (search snippet); old plan `https://coding-intl.dashscope.aliyuncs.com/v1` | Flag. Skip for CI. |
| **MiniMax M Plan** (was Token Plan, was Coding Plan) | Go 1x, Explore 3x, Build 7.5x. Prices not retrieved; third-party says Plus $20, older page $10 / $20 / $50 | unknown | 5-hour and weekly windows; text model M3.1 Flash | pi, Claude Code, Codex, Cursor and OpenCode are named as supported with an M Plan key. Automation not addressed. | not found | Allowed in pi on paper, but price and quota not verified. Not clearly cheaper. |
| **NanoGPT Pro** | $12 | | 60M input tokens a week, **cached input counts in full**, output free on included models | Terms not read | not found | **Fails our test:** our fleet uses about 10B input tokens a week, so 60M is 0.6%. |
| **Chutes** | monthly plans "no longer sold" | | | | `https://llm.chutes.ai/v1` | Dead end. |
| **Cerebras Code** | Pro $50 (24M tokens per day), Max $200 (120M per day) | | | | `https://api.cerebras.ai/v1` | Sold out on 2026-10-02 or merged into a Developer tier; unconfirmed. pi already has a `cerebras` provider with Qwen3.8 27B (64K context), too small for workers. |
| **Groq** | no flat coding plan found | | | | | Nothing to evaluate. |
| DeepRelay, Offloop, Baseten, DeepSeek Pro | | | | | | On the skip list. |

Other facts that matter for the choice:
- **GPT 5.6 Luna** is on Vals SWE-bench at 93.0% for $0.04 per test and CommandCode lists it on every plan (price not on the page). Not evaluated further here (price and plan terms not checked).
- **GLM 5.3 Flash** 92.0% at $0.02 per test is the best score per dollar on Vals after DeepSeek, and it is already inside Go, GOAT, Token Pass and Synthetic.
- Cheapest combination for the fleet, from allowances we can verify: Go $10 ($100) + Token Pass $30 ($150) + GOAT $10 (up to $100) = about $50 plus fees for $350 of list-price use against a $330 need. That is about $0.005 per median run all-in, versus $0.033 pay-as-you-go. No other plan beats this.

## 5. What to do

1. Keep the lane order from the earlier file. Add CommandCode GOAT as a lane only if a third allowance is needed; ids `deepseek/deepseek-v4.1-flash` on `https://api.commandcode.ai/provider/v1`.
2. Add Google as a judge-only lane with `gemini-3.5-flash-lite` and a prepaid AI Studio key. Do not add it to workers.
3. Do not buy Google AI Pro or Ultra, Z.ai, Kimi, Qwen Token Plan or NanoGPT for the fleet.
4. Optional $9 trial of Synthetic Go to check its weekly credit size and "infinite mode".

## 6. Could not verify

- Gemini implicit caching through pi's `google` provider for 3.x models (could change the Gemini median run from $0.164 to $0.92).
- Free-tier requests per day. "20 per day" for 3.8 Flash and "500 per day" for Flash-Lite come from a forum post dated 2026-09-03 and secondary guides; Google's docs page does not print them.
- Vertex AI price: snapshots show $0.30 and $0.54 input for 3.5 Flash-Lite.
- Express-mode data use; the page does not say.
- Whether Valentin's Google tokens are a subscription, a free key or prepaid credit (see 3.1).
- Terminal-Bench 4.0 Gemini score (read through a page summariser; matches the older 0910 DeepSeek figure, so it may be a mix-up). Artificial Analysis comparison page returned 404 and was not used.
- Vals has no SWE-bench entry for DeepSeek V4.1 Flash; 88.8% is for V4 Flash 0731.
- OpenCode Go Plus is resolved for our account: Valentin confirmed $40/month and a $200 DeepSeek allowance. Do not infer its allowance from a marketing multiplier.
- CommandCode: processing fee amount, Legal page terms, whether the `deepseek/deepseek-v4.1-flash` id is the 1017 snapshot, and whether GOAT meters the Gemini models.
- Synthetic: weekly credit dollars per plan, infinite-mode eligibility, terms, base URL (secondhand).
- Z.ai, Kimi, MiniMax: whether pi counts as an officially supported tool; MiniMax prices and base URL; Kimi USD prices.
- Alibaba and MiniMax, Z.ai usage-policy pages were read through a summariser; the quoted restrictions are from those summaries.

## 7. Sources

Google (all read 2026-11-22, none shows a page date):
- Pricing (redirect from ai.google.dev): https://aistudio.google.com/docs/pricing
- Rate limits and tiers: https://aistudio.google.com/docs/rate-limits
- Billing, prepay, credits: https://aistudio.google.com/docs/billing
- OpenAI compatibility: https://aistudio.google.com/docs/openai
- Free trial exclusions (Nov 16, 2026 cutoff): https://docs.cloud.google.com/free/docs/free-cloud-features
- Express mode: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/express-mode/overview
- Vertex pricing: https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing (not readable in full)
- Gemini CLI quotas, updated 2026-06-18: https://geminicli.com/docs/resources/quota-and-pricing
- Code Assist quotas: https://docs.cloud.google.com/gemini/docs/quotas
- Antigravity plans: https://antigravity.google/docs/plans.md
- Gemini 3.6 Flash and 3.5 Flash-Lite launch: https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-6-flash-3-5-flash-lite-3-5-flash-cyber/
- Gemini CLI GitHub Action: https://github.com/google-github-actions/run-gemini-cli
- Startups program: https://cloud.google.com/startup/ai
- Forum, free-tier 20 RPD (2026-09-03): https://discuss.ai.google.dev/t/gemini-3-8-flash-free-tier-20-rpd-is-too-limited-for-practical-evaluation/180609
- Forum, Pro quota changes (2026-05-08): https://discuss.ai.google.dev/t/usage-quotas-as-of-last-month-or-so-has-made-gemini-ai-pro-with-cli-and-antigraivity-worthless/144049
- Forum, 403 ToS bans: https://discuss.ai.google.dev/t/urgent-mass-403-tos-bans-on-gemini-api-antigravity-for-open-source-cli-users-paid-tier/124508
- Third-party free-tier guide (2026-10-15): https://neuraplus-ai.github.io/blog/gemini-api-free-tier.html

Benchmarks:
- Vals SWE-bench (updated 2026-09-01): https://www.vals.ai/benchmarks/swebench
- Vals DeepSeek V4.1 Flash 1017 vs Gemini 3.8 Flash: https://www.vals.ai/comparisons/deepseek_deepseek-v4.1-flash-1017-vs-google_gemini-3.8-flash
- Vals DeepSeek V4.1 Flash vs Gemini 3.7 Flash: https://www.vals.ai/comparisons/deepseek_deepseek-v4.1-flash-vs-google_gemini-3.7-flash
- DeepSeek V4.1-Flash report (DeepSWE): https://arxiv.org/pdf/2609.19969

Plans:
- OpenCode Go: https://opencode.ai/go , https://opencode.ai/docs/go/
- CommandCode: https://commandcode.ai/pricing , https://commandcode.ai/docs/provider , https://commandcode.ai/docs/resources/usage-limits , live model list https://api.commandcode.ai/provider/v1/models
- Z.ai: https://docs.z.ai/devpack/overview , https://docs.z.ai/devpack/usage-policy , https://docs.z.ai/devpack/quick-start
- Kimi: https://www.kimi.com/code/docs/en/kimi-code/membership , https://www.kimi.com/en/help/membership/membership-pricing
- Alibaba: https://docs.qwencloud.com/token-plan/overview
- MiniMax: https://platform.minimax.io/docs/guides/pricing-token-plan.md
- Synthetic: https://synthetic.new/pricing , https://synthetic.new/rate-limits , https://synthetic.new/pricing/models
- NanoGPT: https://nano-gpt.com/pricing
- Chutes: https://chutes.ai/pricing
- Cerebras Code (secondary): https://www.morphllm.com/cerebras-pricing

Local: pi catalog and provider list under `~/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/` (docs/providers.md, pi-ai provider data), `~/projects/token-ladder/ci/ladder-call`, `~/projects/token-ladder/bin/plan-pick`.
