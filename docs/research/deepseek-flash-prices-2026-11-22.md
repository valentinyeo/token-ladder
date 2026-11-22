# DeepSeek V4.1 Flash 1017: where to buy it cheapest (2026-11-22)

Research only. No keys used, no spend, nothing changed.
All prices are USD per 1M tokens. "Cached" means a cache-hit read of input.
Pages marked "no page date" showed no date; they were read on 2026-11-22.

## 1. Answer first

**Is the plan-pick order (vercel, deepseek, opencode, codex, cursor, openrouter) cheapest first? No.**
Two reasons:

1. DeepSeek direct is the most expensive way to buy this model per token, apart from Cursor. It charges list price, and list price doubles in peak hours. It sits second.
2. OpenCode Go is a 10x deal (about $10 buys $100 of DeepSeek Flash use) and should be first. Vercel Token Pass is a 5x deal ($30 buys $150 of use). Both are monthly allowances that expire, so they should be burned before any pay-as-you-go lane.

**My pick for the reorder (DeepSeek lanes only; codex and cursor keep their own pace rules):**

1. `opencode` (Go Plus if we upgrade, see below)
2. `vercel` (Token Pass, after fixing the model id, see "Fix first")
3. `commandcode` GOAT (new, optional, $10 for up to $100)
4. `openrouter` pinned to one cheap provider (see section 4)
5. `deepseek` direct, only off-peak (skip it 01-04 and 06-10 UTC, Monday to Friday)
6. never `cursor` for DeepSeek (it bills at list price from prepaid credit and is not covered by the plan)

**Cheapest 3 routes** (for our measured workload, section 3):

1. Vercel Token Pass: effective 20% of list, about $0.03 in / $0.0006 cached / $0.12 out while the $150 credit lasts. $30 per month, Pro team needed.
2. OpenCode Go or Go Plus: $10 or $40 per month for a $100 or $200 monthly DeepSeek Flash allowance (about 10x and 5x). Our key is already there.
3. Pay-as-you-go through OpenRouter pinned to StreamLake, DeepInfra flex or Pearl Research: about half the cost of DeepSeek direct for our token mix (fp8 or better).

### Fix first (found while checking snapshots)

- **Vercel lane id.** plan-pick's vercel lane calls `deepseek/deepseek-v4.1-flash-1017`. Vercel's Token Pass docs say credit only pays for the exact id `deepseek/deepseek-v4.1-flash` (which Vercel says routes to the 1017 snapshot). With the `-1017` id the call may bill AI Gateway credits at the serving provider's rate (list $0.30 / $1.20 at Baseten, 2x Token Pass list) instead of the pass. ci/lanes.json already uses the right id. Check the Vercel dashboard, Observability, `resolved_provider` should say `token-pass`.
- **Same name, different model.** On OpenRouter, `deepseek/deepseek-v4.1-flash` (no suffix) is the older 2026-09-10 snapshot, not 1017. ci/lanes.json uses that id for its openrouter lane, so that lane runs an older model. Use `deepseek/deepseek-v4.1-flash-1017`. On Vercel and OpenCode the same plain name means 1017, so the name is not safe across gateways.
- **DeepSeek model name.** DeepSeek's own docs list `deepseek-flash` (= V4.1-Flash-1017). ci/lanes.json sends `deepseek-v4.1-flash`, which is not in the docs. Not tested (no key used); likely a 404. plan-pick's lane already uses `deepseek-flash`.
- **OpenCode "monthly window full" does not match usage.** Pi session files show OpenCode Go carried about 24.6M new input, 1.55B cached and 7.6M output in 4 days, about $13 at list. The stated DeepSeek Flash limit is $100 per month. Either earlier days of the month used the rest, or the shared pool counts differently. A read-only check is `GET https://opencode.ai/inference/go/v1/usage` with the key (5-hour, weekly, monthly meters). I did not call it.
- **No 1070 snapshot exists.** The "V4.1 Flash 1070" in the X post is not on DeepSeek's docs, OpenRouter (50 endpoints, all `20261017`), Vercel or OpenCode. Everything says 1017. Likely a typo.
- **Stray model attempts in pi history** (0 tokens each): 1 message to provider `baseten`, 7 messages to `deepseek-v4-pro-0813` on OpenRouter. Both are on the banned list. Worth a look at who called them.

## 2. Snapshot check, route by route

| Route | Serves 1017? | Evidence |
|---|---|---|
| DeepSeek direct `deepseek-flash` | Yes | Docs: version DeepSeek-V4.1-Flash-1017. Old ids `deepseek-v4-flash` still accepted and redirected to it. |
| Vercel `deepseek/deepseek-v4.1-flash-1017` | Yes | Model listed, snapshot of 10/17. Alias `deepseek/deepseek-v4.1-flash` also goes to 1017 per Token Pass docs. |
| OpenCode Go/Zen `deepseek-v4.1-flash` | Yes | `/v1/models` returns `canonical_id: deepseek/deepseek-v4.1-flash-1017`. |
| OpenRouter `...-1017` | Yes | All 50 endpoints report `deepseek-v4.1-flash-20261017`. |
| OpenRouter `deepseek/deepseek-v4.1-flash` | **No, older (0910)** | Endpoints report `...-20260910`. Cheaper ($0.05 in) but not what we want. |
| Cursor | Yes | Doc page named "DeepSeek V4.1 Flash 1017". |
| CommandCode | Not stated | Page says "DeepSeek V4.1 Flash" only. |
| DeepRelay | Claims 1017 | Home page lists "V4.1 Flash 1017". Unverified. |

## 3. Price table: the main routes

CI = can a GitHub Actions job call it with a plain API key over an OpenAI-compatible endpoint? Subscription-only plans that need an interactive login cannot.

| Route | Input | Cached | Output | Caching works? | Limits | CI with plain key? | Source (read 2026-11-22) |
|---|---|---|---|---|---|---|---|
| DeepSeek direct, off-peak | 0.15 | 0.003 | 0.60 | Yes, automatic prefix cache | 2,500 concurrent (flash), HTTP 429 above; no RPM cap listed | **Yes** `https://api.deepseek.com` (OpenAI), `https://api.deepseek.com/anthropic` (Anthropic). Model `deepseek-flash` | https://api-docs.deepseek.com/quick_start/pricing and https://api-docs.deepseek.com/quick_start/rate_limit (no page date; mentions a Nov 18 2026 change) |
| DeepSeek direct, peak (Mon-Fri 01-04 and 06-10 UTC, about 21% of the week) | 0.30 | 0.006 | 1.20 | same | same | same | same |
| Vercel Token Pass, as billed | 0.15 list, but credit is $150 for $30 | 0.003 | 0.60 | Yes, implicit (served by Wafer) | Pass is per team, Pro teams, early access, waitlist when full. Credit resets monthly, no rollover. ZDR. | **Yes** `https://ai-gateway.vercel.sh/v1`. Key `AI_GATEWAY_API_KEY`. Model must be `deepseek/deepseek-v4.1-flash` to draw pass credit | https://vercel.com/docs/ai-gateway/token-pass (last updated 2026-10-14), https://vercel.com/changelog/vercel-token-pass-now-includes-one-hundred-fifty-dollars-of-monthly-tokens (2026-11-13) |
| Vercel Token Pass, effective | **0.03** | **0.0006** | **0.12** | yes | until $150 is gone (about 14 days of our load) | yes | same, my arithmetic (30/150 = 0.2) |
| Vercel after credit runs out (standard key falls back to gateway credits) | 0.30 | 0.007 | 1.20 | yes | none stated | yes | Vercel gateway `/v1/models` lists Baseten at 0.30/0.007/1.20 for `-1017`. Do not let this lane fall to Baseten (banned): add a provider `only` filter. |
| OpenCode Go ($10) / Go Plus ($40) | list 0.15 (peak 0.30) | 0.003 | 0.60 (peak 1.20) | Yes (pi shows cache read priced, cache write $0) | **DeepSeek Flash allowance $100 per month on Go, $200 on Go Plus.** 5-hour window 20% of monthly, weekly 50%. Est. 43,300 requests per 5h on Go. | **Yes** `https://opencode.ai/inference/go/v1` (OpenAI; pi uses `.../go/openai/v1`). The older `https://opencode.ai/zen/go/v1` still answers 200 on `/models`, so ci/lanes.json works. Key `OPENCODE_API_KEY`. Personal or service-account key. Asks for a custom User-Agent and a stable `x-opencode-session` header | https://opencode.ai/go (limits), https://opencode.ai/docs/go/ (windows, API, no page date), https://pi.dev/models/opencode-go/deepseek-v4-1-flash |
| OpenCode Go effective | about 0.015-0.03 | about 0.0003 | about 0.06-0.12 | | 10x on Go, 5x on Go Plus, if fully used | | my arithmetic |
| OpenCode Zen pay-as-you-go | 0.12 | 0.0024 | 0.48 | Yes | none stated. Top-up fee 4.6% + $0.31 | **Yes** `https://opencode.ai/zen/v1` | https://opencode.ai/docs/zen/ . Price is "60% off, now" (regular 0.30/0.006/1.20), so it can revert |
| OpenCode "DeepSeek V4.1 Flash Free" | 0 | 0 | 0 | n/a | free for a limited time. One third-party report says 200K context (paid is 1M). Free models may log prompts; not confirmed | Yes via Zen endpoint | https://opencode.ai/docs/zen/ , https://magica.com/news/opencode-deepseek-v4-1-flash-free-context-limit |
| OpenRouter, DeepSeek provider | 0.15 (peak 0.30, UTC schedule) | 0.003 | 0.60 | Yes | free models 50 per day (1,000 if $10 credit bought); paid model limits not stated. Credit fee 5.5% (min $0.80) | **Yes** `https://openrouter.ai/api/v1`. Model `deepseek/deepseek-v4.1-flash-1017` | https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints , https://openrouter.ai/docs/faq |
| OpenRouter batch variant `...-1017:batch` | 0.035 | 0.0029 | 0.149 | Yes | asynchronous, for non-urgent work | Yes, same base URL | https://openrouter.ai/api/v1/models (listing) |
| Cursor (prepaid credit) | about 0.30 | about 0.006 | about 1.20 | Yes, no cache-write fee | Third-party model: not covered by plan usage; draws prepaid credit | **No for plain chat.** `https://api.cursor.com` is the Cloud Agents API, "not a standalone inference or chat-completions API". The agent CLI needs a Cursor login | https://cursor.com/docs/models/deepseek-v4-1-flash-1017 (rates inferred from search summary; page body omitted the figures), https://cursor.com/docs/api |
| CommandCode GOAT ($10 + processing fee) | credit-based | | | not stated | "Up to $100" of DeepSeek V4.1 Flash credit per month, about 75K requests. Go tier: $1 for $10. Provider plan $15 with OpenAI and Anthropic compatible endpoints | **Yes for GOAT** ("API access"). Base URL not shown on the pricing page; read their docs before wiring | https://commandcode.ai/pricing (no page date). Third-party pages disagree ($70 per month cap); treat "up to $100" as a ceiling |

Why caching matters so much: see section 5. Our workload is 98.7% cached-read tokens, so the cached price and whether the cache survives between turns matter more than the headline input price.

## 4. Every OpenRouter provider for `deepseek/deepseek-v4.1-flash-1017` (50 endpoints)

Source: https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints (read 2026-11-22, live API). All endpoints serve snapshot 20261017 and report implicit caching. Quantization as listed. Context is 1M except StreamLake (1.02M) and Alibaba (1.0M). Uptime 100% over 30 minutes except where "n/a". "Cost for our 4-day load" uses the measured token mix in section 5 (71M new input, 5.58B cached, 28.2M output); DeepSeek direct off-peak is $44.27 for the same load.

| Provider | In | Cached | Out | Quant | 4-day load $ |
|---|---|---|---|---|---|
| StreamLake | 0.075 | 0.0015 | 0.30 | fp8 | 22.1 |
| Ambient | 0.045 | 0.0025 | 0.25 | fp4 | 24.2 |
| DeepInfra flex | 0.0784 | 0.0024 | 0.2352 | fp8 | 25.6 |
| Alibaba (x2 from 00:00 to 14:00 UTC) | 0.09 | 0.0018 | 0.36 | n/a | 26.6 off-window |
| Pearl Research | 0.05 | 0.003 | 0.30 | fp8 | 28.7 |
| Cloudflare | 0.10 | 0.003 | 0.60 | n/a | 40.7 |
| Sail Research (two entries) | 0.04 | 0.007 | 0.10 | fp4 | 44.7 |
| DeepSeek | 0.15 | 0.003 | 0.60 | n/a | 44.3 |
| Fireworks spot, DigitalOcean, SiliconFlow | 0.15 | 0.003 | 0.60 | fp8 or n/a | 44.3 |
| Wafer (mxfp4) | 0.036 | 0.01 | 0.50 | mxfp4 | 72.4 |
| Particle AI flex | 0.03 | 0.015 | 0.275 | n/a | 93.5 |
| Together, Fireworks, Parasail, Mistral, Reka, CoreWeave, Near AI, Venice, Crusoe | 0.29-0.30 | 0.006-0.0075 | 1.20 | mixed | about 88 |
| Baseten (banned) | 0.30 / 0.60 | 0.007 / 0.014 | 1.20 / 2.40 | n/a | never |
| Fireworks us, Morph fast | 0.45 / 0.60 | 0.009 / 0.014 | 1.8 / 2.4 | | about 130-180 |

Others, not costed (all between): DeepInfra standard (0.098/0.0029/0.294) and priority (0.147/0.0044/0.441), InferenceNet 0.08/0.008/0.666, Baidu 0.0977/0.0021/0.3944, Inceptron, DekaLLM, Cohere, Ant Digital, River, Relace, Open Inference (0.05/0.002/0.80), PrimeIntellect, Morph fp8, Makora, Novita (0.165/0.0033/0.66), ModelRun, Nebius, Ionstream, PrimaLabs, Modal.

Read this carefully:

- The cheapest-looking input price (Particle AI, Wafer, Sail) is the worst for us, because their cached price is 3x to 5x higher and cached reads are most of our tokens. Pick by cached price and output price, not input price.
- fp4 endpoints (Ambient, Sail, Inceptron) are a real quality risk for coding agents. Prefer fp8 or better (StreamLake, DeepInfra, Pearl).
- StreamLake, Alibaba and Baidu are China-hosted; Pearl and Ambient are small vendors. The pi history shows our code goes through providers with unknown retention. Check each provider's data policy before pinning.
- OpenRouter can hop between providers between turns, which breaks the cache and quietly turns a 0.0015 cache price into full input price. Pin one provider (`provider.only`, `allow_fallbacks: false`) or the saving disappears.
- These are listed rates with 5.5% credit fee on top. I did not measure real cache hit rates on any non-DeepSeek provider.

## 5. Where our DeepSeek tokens go (measured)

Measured from the pi session files in `~/.pi/` for the last 4 days (769 worker sessions, 55,549 model turns), summing the usage fields. Aggregate token counts only.

| Measure | Value |
|---|---|
| New (uncached) input | 71.0M |
| Cached reads | 5,575.8M (98.7% of all input tokens) |
| Output | 28.2M |
| Median run | 68K new input, 2.79M cached, 24K output, 52 turns |
| Heavy run (90th percentile) | 194K new, 20.3M cached, 87.5K output, 153 turns |
| By route | vercel 2.69B cached, opencode-go 1.55B, deepseek direct 1.34B, openrouter 0.0007B |
| Cost split at DeepSeek off-peak list | cached reads 38%, output 38%, new input 24% |

Cost of the same 4-day load by route (arithmetic from the prices above):

| Route | 4-day load | Median run | Heavy run |
|---|---|---|---|
| Vercel Token Pass effective | $8.9 | $0.007 | $0.029 |
| Hy4 flash, OpenRouter, 50% promo | $14.8 | $0.010 | $0.050 |
| OpenRouter StreamLake | $22.1 | $0.017 | $0.071 |
| OpenRouter DeepInfra flex | $25.6 | $0.018 | $0.085 |
| Hy4 flash, list after Nov 30 | $29.6 | $0.020 | $0.099 |
| OpenCode Zen pay-as-you-go | $35.4 | $0.026 | $0.114 |
| DeepSeek direct off-peak (and Vercel list) | $44.3 | $0.033 | $0.143 |
| DeepSeek direct blended with peak share | $53.6 | $0.040 | $0.173 |
| DeepSeek direct or Cursor at peak | $88.5 | $0.066 | $0.285 |

That is about $332 per month for the whole fleet at off-peak list (44.27 / 4 x 30), or roughly $400 with peak hours. Pi's own cost field gave $46.87 for the Vercel share alone, which matches peak-rate math, so pi may be pricing at peak rates.

What it means:

- **Three monthly allowances cover the whole fleet for about $80:** Go Plus $40 ($200 list), Token Pass $30 ($150 list), CommandCode GOAT $10 (up to $100). Together up to $450 of list-price use against about $332 of need. This assumes allowances count at list price and CommandCode delivers its ceiling.
- **Peak hours cost double.** About 21% of the week is peak at DeepSeek and OpenCode. A plan-pick rule that skips DeepSeek direct in peak windows (and prefers Go/Vercel then) avoids the 2x.
- **Cached-read price is the lever.** Anything with a cache price above $0.003 loses for our mix.

## 6. Other leads from the X account (treated as credible, verified against vendor pages)

| Lead | Verdict | Facts |
|---|---|---|
| **Mistral Pro $15 with "$300 Vibe usage" and DeepSeek in harnesses** | **Not confirmed.** | Mistral's pricing page: Pro $14.99 per month, "more access and usage", no dollar figure for Vibe, Vibe CLI runs on Devstral, no DeepSeek mentioned. Vibe config can point at OpenRouter, which would be pay-as-you-go anyway. Pro is a chat subscription; the API is separate Studio billing. Source: https://mistral.ai/en/pricing . Not a CI route. |
| **OpenCode Go $10 large DeepSeek allowance** | **Confirmed.** | $100 per month on Go, $200 on Go Plus ($40), Teams $20 per member (min 2). We have it. See table in section 3. An older page snippet said $80; live page says $100. |
| **CommandCode GOAT $10 for $100** | **Mostly confirmed, "up to".** | Vendor page: GOAT $10 plus processing fee, up to $100, about 75K requests, API access, DeepSeek V4.1 Flash $100. Go tier $1 for $10 but no API access. Third-party pages say $70 per month cap and conflicting per-window limits. |
| **DeepRelay $1 for 500M in + 50M out, weekly limits** | **Not verified, high risk, low value.** | deeprelay.ai exists ("Deeprelay, Inc.", support@deeprelay.ai). Home page: one OpenAI-compatible key for open models, a "flat monthly plan", lists DeepSeek V4.1 Flash 1017 at $0.27 to $0.54 per 1M output. The $1 plan, the 500M cap and the weekly limits are not on the page I could read. Upstream key source is not disclosed. Real list cost of 500M in + 50M out is about $105, so $1 is a 100x discount that an honest reseller cannot fund: expect pooled or promotional keys that may vanish. Our heavy week uses 5.6B cached tokens in 4 days, so a 500M cap (if it counts cached reads) lasts under a day. Do not send our code through it. Source: https://deeprelay.ai |
| **Hy4 Flash** | See section 7. | |
| **GLM 5.3 as rescue/reviewer** | **Credible. Cheapest good routes below.** | GLM 5.3 list: $1.40 in / $0.26 cached / $4.40 out (Vercel, Z.ai direct, Together, Fireworks). OpenRouter's cheapest rows are Wafer 0.07/0.0689/1.40 and Relace 0.07/0.0703/4.38 (fp4; odd cached price, treat as unproven). Included in: Vercel Token Pass (list price), OpenCode Go ($200 per month limit on a $10 plan, about 2,640 requests per 5h), Z.ai Coding Plan (Lite from $18, 2,000 credits per 5h and 10,000 per week, Pro and Max higher, off-peak half price, works with Claude Code, Codex, OpenCode and 20+ tools). GLM 5.3 Flash is $0.15 / $0.03 / $0.50 on Vercel. A review call is a few tens of thousands of tokens, so cost per review is cents. Sources: https://docs.z.ai/devpack/overview , https://opencode.ai/go , https://vercel.com/docs/ai-gateway/token-pass , OpenRouter endpoints API. CI: Vercel `https://ai-gateway.vercel.sh/v1` model `zai/glm-5.3`; OpenCode Go model `glm-5.3`. |
| **Nemotron 3 Ultra free via NVIDIA key** | **Real, usable for short review jobs, not as a worker.** | Free hosted endpoint on build.nvidia.com with a free key; CI base URL `https://integrate.api.nvidia.com/v1` (OpenAI style). Limits: developers report "Resource exhausted" on a 32-request worker limit and ask to go from 40 to 200 RPM; official limit page not found. OpenRouter free copy: 50 requests per day (1,000 with $10 credit bought). Paid: $0.50 in / $0.10 cached / $2.20 out on OpenRouter. Coding: NVIDIA reports SWE-bench Verified 71.9 (BF16), 69.7 (NVFP4); Vals via OpenRouter 69.0; Terminal-Bench 2.1 56.4 (NVIDIA). DeepSeek reports 90.6 on Terminal-Bench 2.1 for V4.1 Flash (self-reported; an independent check of the older V4 Flash 0731 gave 79). So it is weaker on terminal tasks. Free tiers may log prompts: no secrets or client code. Sources: https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b , https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b:free |

## 7. Hy4 Flash: should it become a worker lane ahead of DeepSeek Flash?

**Verdict: no. Add it as a trial lane behind DeepSeek Flash, free route first.**

Price (OpenRouter, only Tencent serves it, fp8; checked via API 2026-11-22):

| | Input | Cached | Output | Notes |
|---|---|---|---|---|
| OpenRouter now | 0.035 | 0.0015 | 0.1405 | This is the 50% launch discount (`discount: 0.5`), through Nov 30 |
| List after Nov 30 | 0.07 | 0.003 | 0.281 | TechNode / Tencent TokenHub: 0.5 yuan in, 2 yuan out, 0.02 yuan cached |
| Tencent direct (TokenHub) | 0.5 yuan | 0.02 yuan | 2 yuan | Half price until Nov 30. OpenAI and Anthropic style APIs. Base URL not captured |
| OpenCode Go | | | | Listed as "Hy4 flash Free", unlimited, "limited time" |
| Vercel | 0.035 | 0.0015 | 0.1405 | `tencent/hy4-flash` listed |

Why not ahead:

1. **The saving is smaller than the headline.** Our tokens are 98.7% cached reads, and Hy4 Flash's list cached price (0.003) equals DeepSeek's. At list it saves about 33% on our mix ($29.6 vs $44.3 for 4 days), not 70%. Only the Nov 30 promo makes it 67% cheaper.
2. **Quality is not proven for long coding work.** No SWE-bench Verified score for Flash anywhere I could find. Tencent's own launch notes say WorkBuddy saw a slight decline on long coding tasks versus Hy4 and recommend Hy4 for complex engineering. On Vals Terminal-Bench 4.0 DeepSeek V4.1 Flash scores 19.7; Tencent reports 19.2 for Hy4 Flash (different publishers); Hy4 Preview gets 8.1 on Vals. The "8x cheaper per task" figure is Tencent's private WorkIndex benchmark, company reported.
3. **Limits.** Max output 64K tokens on OpenRouter (DeepSeek: 131K to 384K), single provider, no zero-data-retention flag, China-hosted.
4. **The popularity claim does not reproduce.** OpenRouter ranking snapshots I could find show DeepSeek V4.1 Flash 1017 first at about 7 to 9T tokens a day, and Hy4 (not Flash) second at about 2T a day. I could not find "20.9T for Hy4 Flash". Another listing shows Hy4 at 18.9T to 19.8T for the month. The post's numbers are not confirmed.
5. **The "overthinks less" claim** is the reverse of the model card statement for Hy4 Preview ("spends longer than necessary reasoning", "over-verifies"). Not shown for Flash either way.

Can our harnesses run it? OpenCode Go lists `hy4-flash-free` and `hy4`, `hy4-preview` on its `/v1/models`, and pi already talks to the `opencode-go` and `openrouter` providers, so `--provider opencode-go --model hy4-flash-free` should work (not tested; pi's own model page for it returned 404). Cursor: not available as a Cursor-included model (not listed). CI: OpenRouter `tencent/hy4-flash`, Vercel `tencent/hy4-flash`.

Suggested trial: one lane `hy4-flash-free` on OpenCode for easy tickets and as a second-opinion reviewer until Nov 30, measure the retry and failure rate against DeepSeek, then decide.

## 8. Accounts we hold: price and what they give coding agents

Last launch date per lane is from `~/.local/state/token-ladder/launches.jsonl` (lane-limits.json only holds a Vercel "limited until" time).

| Account | List price | What it includes for coding agents | Our state | Last launch | CI with plain key? | Source |
|---|---|---|---|---|---|---|
| Claude (Anthropic) | Pro $17 per month annual or $20 monthly; **Max $100 = 5x Pro**; **Ultra $200 = 10x Pro weekly, no 5-hour limit**. The page shows no "20x" tier. Which tier we hold is not in config (`source: unknown`). | Claude Code included on all. Max and Ultra also list $100 and $200 per month of API credits and Fable up to half the weekly limit. | 32% of weekly used at pace 0.43x: well under-used (chats only). | n/a | Subscription login: no. The included API credits, if real, are an API key and could run a CI judge. Verify in the console. | https://claude.com/pricing |
| ChatGPT / Codex | Go $8; Plus $20; **Pro from $100 (5x)**, $200 tier (20x). Another page describes $100, $200, $500; not confirmed. | Codex included on Plus and above, Pro has longer sessions and three usage tiers. | Weekly limit used up (100%). | n/a | Subscription login: no (OpenAI API key is separate billing). | https://developers.openai.com/codex/pricing , https://chatgpt.com/pricing (Plus and Pro figures not shown in my fetch) |
| Cursor (shown on its page as "The X Subscription") | Free $0, Plus $8, Premium $30, Super $100, Ultra $200; Teams $30+ per user. Usage 4x, 15x, 50x Plus. | Cloud agents from Premium up. Composer 2.5 and Grok 4.7 are not named on the page; your config records "included, no extra cost, Valentin 2026-11-20". Third-party models (DeepSeek) are billed separately at list. | Only open worker lane; pool 32% used, pace 2.12x. | 2026-11-22 17:41 UTC | **No** for plain chat. Agent runs only via `https://api.cursor.com`. | https://cursor.com/pricing , https://cursor.com/docs/api |
| Vercel AI Gateway Token Pass | **$30 per team per month** (on top of a Pro seat), **$150 credit** (was $80 until 2026-11-13). Credit covers Kimi K3, K3.1 Pro, GLM 5.3, GLM 5.3 Flash, DeepSeek V4.1 Flash at list. | $150 of DeepSeek Flash at list = about 14 days of our load. Resets monthly. ZDR. Falls back to gateway credits after. | About $59 credit left (lane-credits.json 58.98); limited until 18:25 UTC. | 2026-11-22 17:25 UTC | **Yes** `https://ai-gateway.vercel.sh/v1` | https://vercel.com/docs/ai-gateway/token-pass |
| DeepSeek platform prepaid | Pay as you go. Minimum top-up and fees not on pages I read. | DeepSeek Flash only: 0.15 / 0.003 / 0.60 off-peak, double at peak. 2,500 concurrent. | $0.00 | 2026-11-20 10:59 UTC | **Yes** `https://api.deepseek.com` | https://api-docs.deepseek.com/quick_start/pricing |
| OpenCode Go (and Zen) | Go $10, Go Plus $40, Teams $20 per member. Zen is pay-as-you-go, top-up fee 4.6% + $0.31. | Go: $100 DeepSeek Flash per month ($200 Plus), GLM 5.3 $200 ($400 Plus), Hy4 $60, Kimi K3.1 Pro $15. 5-hour 20%, weekly 50% of monthly. Free models listed (DeepSeek Flash Free, Hy4 flash Free, Nemotron 3.5 Lightning Free, others) for a limited time. | Monthly window shown full; $5.87 estimated credit | 2026-11-21 18:13 UTC | **Yes** `https://opencode.ai/inference/go/v1`, Zen `https://opencode.ai/zen/v1` | https://opencode.ai/go , https://opencode.ai/docs/go/ , https://opencode.ai/docs/zen/ |
| OpenRouter | Pay as you go, 5.5% credit fee (min $0.80). No subscription. | Every model above at provider prices, plus batch variant. | -$0.20 (overdrawn) | not in the launches log as a launch since 2026-11-19 (23 messages in pi history) | **Yes** `https://openrouter.ai/api/v1` | https://openrouter.ai/docs/faq |
| Google | AI Pro $19.99 per month, a 5x tier $99.99, Ultra 20x $199.99 (9to5Google May 2026; older pages say Ultra $249.99, so conflicting). Gemini API: free tier and AI Studio billing exist; I could not get current quota numbers. | Pro and Ultra raise Gemini CLI and Code Assist daily limits and Jules task limits (numbers are in Google's developer docs, not captured). | Not in plan-pick. | n/a | Plans: no (interactive login). Gemini API key: yes, `https://generativelanguage.googleapis.com/v1beta/openai/` (standard Google endpoint; free-tier limits not verified here). | https://9to5google.com/2026/05/25/google-ai-plus-pro-ultra-gemini-features/ , https://blog.google/innovation-and-ai/technology/developers-tools/gemini-cli-code-assist-higher-limits/ |

### Which accounts look redundant

- **DeepSeek direct** is the weakest lane: highest per-token price among the DeepSeek routes except Cursor, doubled at peak, $0 balance. Keep a small balance only as an off-peak fallback with the 2,500 concurrency. Not worth a larger top-up.
- **OpenRouter** at -$0.20 with a 5.5% fee is only worth keeping if pinned to one cheap fp8 provider or used for the batch variant. As a generic fallback it is redundant with DeepSeek direct.
- **Vercel Token Pass and OpenCode Go overlap.** Both are monthly DeepSeek Flash allowances. Not redundant yet: one alone cannot cover the fleet (Go $100 is about 9 days, pass $150 about 14 days). Go Plus ($200) plus the pass ($150) covers the month. Do not buy multiple Go accounts to stack; that is likely against their terms.
- **Cursor** is not redundant: it is the only non-DeepSeek, non-Claude worker pool left, and it is included. But never spend prepaid credit on DeepSeek there.
- **Claude** is under-used (0.43x pace): either a smaller tier or move CI review judging onto it (Haiku 5.5 through included API credits, if confirmed) so the spare weekly share is used.
- **Google plans** have no worker lane today and cannot be called from CI with a plan login. Redundant for this fleet unless Gemini CLI or Jules is wanted.
- **ChatGPT/Codex Pro** is at 100%: the pool that is actually tight, not redundant.

## 9. CI review judge: lanes we can run with a plain key

For ci/ladder-call. Everything marked Yes has a plain API key and an OpenAI-compatible URL.

| Lane | Base URL | Model id to use | Note |
|---|---|---|---|
| vercel-ai-gateway | `https://ai-gateway.vercel.sh/v1/chat/completions` | `deepseek/deepseek-v4.1-flash` | Draws Token Pass credit. For a judge also try `zai/glm-5.3` (reviewer). Add provider `only: ["token-pass"]` so it never falls to Baseten |
| opencode-go | `https://opencode.ai/inference/go/v1/chat/completions` | `deepseek-v4.1-flash`, `glm-5.3`, `hy4-flash-free` | The `/zen/go/` path in lanes.json still works; switch to `/inference/go/` as documented |
| deepseek | `https://api.deepseek.com/chat/completions` | `deepseek-flash` (lanes.json has `deepseek-v4.1-flash`, check) | |
| openrouter | `https://openrouter.ai/api/v1/chat/completions` | `deepseek/deepseek-v4.1-flash-1017` (lanes.json has the older 0910 id), batch `...:batch` for nightly runs | Pin one provider |
| commandcode (GOAT) | not on the pricing page | | Add after reading their API docs |
| nvidia free | `https://integrate.api.nvidia.com/v1/chat/completions` | `nvidia/nemotron-3-ultra-550b-a55b` | Free, throttled; short reviews of non-secret diffs only |
| gemini API | `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` | Google's current Flash model | Free-tier limits not verified |
| Cursor, Claude Max, ChatGPT Pro, Google AI Pro/Ultra | none | | Interactive subscription login, not usable from CI |

## 10. Sources

Vendor pages and live APIs read on 2026-11-22 (page dates given where the page showed one):

- DeepSeek pricing: https://api-docs.deepseek.com/quick_start/pricing (no page date; references a Nov 18 2026 change)
- DeepSeek rate limits: https://api-docs.deepseek.com/quick_start/rate_limit
- OpenRouter DeepSeek 1017 endpoints (live JSON): https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash-1017/endpoints
- OpenRouter DeepSeek (0910) endpoints: https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints
- OpenRouter Hy4 Flash endpoints: https://openrouter.ai/api/v1/models/tencent/hy4-flash/endpoints
- OpenRouter GLM 5.3 and Nemotron endpoints: https://openrouter.ai/api/v1/models/z-ai/glm-5.3/endpoints , https://openrouter.ai/api/v1/models/nvidia/nemotron-3-ultra-550b-a55b/endpoints
- OpenRouter FAQ (fees, free limits): https://openrouter.ai/docs/faq
- OpenRouter rankings: https://openrouter.ai/rankings
- Vercel gateway models (live JSON): https://ai-gateway.vercel.sh/v1/models
- Vercel Token Pass docs (updated 2026-10-14): https://vercel.com/docs/ai-gateway/token-pass and https://vercel.com/docs/ai-gateway/token-pass/models-and-routing
- Vercel changelog, $150 credit (2026-11-13): https://vercel.com/changelog/vercel-token-pass-now-includes-one-hundred-fifty-dollars-of-monthly-tokens
- OpenCode Go limits: https://opencode.ai/go ; Go docs https://opencode.ai/docs/go/ ; Zen https://opencode.ai/docs/zen/ ; live model list https://opencode.ai/inference/go/v1/models
- pi model page: https://pi.dev/models/opencode-go/deepseek-v4-1-flash
- Cursor model doc: https://cursor.com/docs/models/deepseek-v4-1-flash-1017 ; API doc https://cursor.com/docs/api ; pricing https://cursor.com/pricing
- CommandCode pricing: https://commandcode.ai/pricing
- Mistral pricing: https://mistral.ai/en/pricing
- DeepRelay: https://deeprelay.ai
- Claude pricing: https://claude.com/pricing
- Codex pricing: https://developers.openai.com/codex/pricing ; ChatGPT pricing https://chatgpt.com/pricing
- Z.ai coding plan: https://docs.z.ai/devpack/overview
- NVIDIA Nemotron 3 Ultra: https://build.nvidia.com/nvidia/nemotron-3-ultra-550b-a55b
- Google plans: https://9to5google.com/2026/05/25/google-ai-plus-pro-ultra-gemini-features/

Secondary or promotional (used only for cross-checks, treat with care):

- Hy4 Flash launch and discount: https://magica.com/news/tencent-hy4-flash-lower-cost-hunyuan-model
- Hy4 pricing roundup: https://www.eesel.ai/blog/tencent-hy4
- DeepSeek V4.1 Flash vs Hy4 Preview, Vals: https://www.vals.ai/comparisons/deepseek_deepseek-v4.1-flash-vs-tencent_hy4-preview
- DeepSeek V4.1 Flash launch: https://venturebeat.com/technology/deepseek-v4-1-flash-debuts-with-0-003-1m-off-peak-cached-input-rate-and-benchmarks-eclipsing-gpt-5-6-sol-claude-opus-5
- OpenCode free DeepSeek tier: https://magica.com/news/opencode-deepseek-v4-1-flash-free-context-limit
- Nemotron forum rate-limit complaints: https://forums.developer.nvidia.com/t/nemotron-unusable-resource-exhausted-on-every-request/375438

## 11. What I did not do

- Did not call any priced or keyed endpoint, and did not read any key file.
- Did not finish the earlier broad Claude-transcript study (cache hit rate, loops, subagents). The scope changed before that started; only the pi worker token measurement above was done.
- Did not verify cache hit rates on non-DeepSeek providers, Cursor's exact DeepSeek rates (the page body hid the figures), CommandCode's API base URL, DeepRelay's plan terms, Mistral's Vibe usage figures, or Gemini API free-tier quotas.
