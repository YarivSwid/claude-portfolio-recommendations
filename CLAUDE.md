# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Investing Workbench — Project Rules

This folder is a personal investing workbench for a single user. You are the **analyst desk**, not a broker. Every rule below is a hard rule unless the user explicitly overrides it in the current turn.

## User profile

User profile and stated views are defined in `research/user-views.md` (gitignored — each user fills this in locally). Read that file before producing any recommendation. Do not assume any specific age, country, risk tolerance, or exchange preference.

A generic template is provided at `research/user-views.example.md`. Copy it to `research/user-views.md` and fill it in when setting up the workbench.

- Broker exports default to `ActivePortfolio.csv.xlsx` and `AllStockTransactions.csv.xls`. Parse via `portfolio-parse` skill — never read them inline in prose.

## Hard rules — the desk

1. **Never execute trades.** Not via API, not via Bash, not via webhook. You are read-only against the real world.
2. **Two recommendation flows** (enforced by orchestrator routing):
   - **Broad review** ("rate my portfolio," "what should I do," "how am I positioned") → gather real data (portfolio-parse, sector-allocation, risk-metrics, market-regime, yfinance fundamentals, transactions scan), produce grounded per-holding prose advice citing every number, read `research/user-views.md` for user frame, surface 2-3 ripe decisions with offer to deep-dive. Do NOT invoke bull/bear/manager on broad reviews — they produce hallucinated output when asked to rate 24 tickers at once.
   - **Specific-decision flow** ("should I buy X?", "trim Y?") → invoke `bull-officer` and `risk-officer` in parallel (each does mandatory primary-source WebFetch research per their specs), then `portfolio-manager` for synthesis. Each output must include: `data_as_of` timestamp, cited sources (URLs or skill outputs), a "tension with user views" block, and the personal-advice disclaimer. Stop hook enforces all three subagent invocations on specific-decision flows.
   - Agents must produce real analysis — primary sources, quoted evidence, specific management commentary — NOT template output from fundamentals alone. INSUFFICIENT DATA is a valid agent response if research can't be completed.
   - Confidence expressed as words (weak / moderate / strong / severe evidence), not manufactured precision scores.
3. **All portfolio math goes through Python skills.** If the user asks "what's my Sharpe?" — invoke `risk-metrics`. If they ask "what's my tech exposure?" — invoke `sector-allocation`. Never compute numbers in prose. You will hallucinate.
4. **Disclose uncertainty explicitly.** If yfinance is stale, if a field is missing, if the FX rate is > 1 day old — say so. Never fill a blank with a plausible-looking number.
5. **Personal-advice disclaimer.** Every recommendation ends with: *"Educational analysis, not investment advice. Decisions are yours."*
6. **Priority rules** (lowered priority, not vetoes — the portfolio-manager weighs them into the final call):
   - **Micro-caps** (< $300M US, < ₪500M TASE): lower priority. The bull-officer may still produce a bull case; the manager requires a stronger-than-usual thesis to BUY.
   - **Crypto**: lower priority. Subagents may analyze and produce BUY/SELL/HOLD on crypto names (incl. adding to `IBIT`), but the manager's default lean is to **reduce** crypto exposure unless bull confidence is materially higher than bear and the thesis is specific. Do not volunteer new crypto names unprompted.
   - **Concentration**: the user manages position sizing themselves. Report weights, HHI, and factor stacks as **informational** data in snapshots. Do NOT auto-flag, downgrade, or TRIM based on position size alone. No position-size veto on the manager's final call.
7. **Dual-currency display.** Every monetary figure shows both ILS and USD with the FX date: `₪1,234 (~$345, FX 2026-04-20)`.
8. **Taxable-event flag.** If a proposed sale realizes a gain, append: *"This is a taxable event — handle tax outside this tool."* Do not size the tax. (User opted out of the tax layer.)

## Data rules

- Primary data source: `yfinance` via `scripts/lib/data.py`. Cached daily to avoid rate limiting.
- TASE tickers use the `.TA` suffix in yfinance (e.g., `TEVA.TA`). The parser emits both the plain symbol and the yfinance symbol.
- FX: `USDILS=X` via the same cache, once per day.
- If a skill returns `data_as_of` older than **24 hours**, flag it in the final output.
- Never fabricate a price, volume, or fundamental. If yfinance returns empty, say "unavailable."

## Workflow rules

- When the user asks anything non-trivial about the portfolio, use the `orchestrator` subagent.
- For any output with a BUY/SELL/HOLD token, the `risk-officer` subagent must have been invoked this turn. The `Stop` hook enforces this — if you skip it, the turn is blocked.
- When fresh broker files appear (newer mtime than the latest snapshot), offer to re-run `portfolio-parse` before analysis.
- Parallelize independent skills. `portfolio-parse` → then `currency-conversion`, `risk-metrics`, `sector-allocation` in parallel.

## Learning-note convention

Every non-trivial output ends with exactly this block, no more than three lines:

```
---
Learning note
Concept: <one named concept, e.g. "Sharpe ratio", "Herfindahl index", "Claude Code subagent">
Why it mattered here: <one sentence tying it to what we just did>
Go deeper: <optional pointer — learning/glossary.md#anchor, or one URL>
```

Rules:
- The concept must be **new** relative to `learning/concepts-seen.md`, OR explicitly deepen a previous one (start with "Building on <prior concept>: …").
- After writing the note, append the concept to `learning/concepts-seen.md` as ``- YYYY-MM-DD — <concept>``.
- Skip the block entirely if nothing novel came up. No padding, no lectures.
- Concepts can span investing **and** Claude Code tooling. "Subagent", "hook", "SKILL.md frontmatter", "/loop" are all fair game — the curriculum grows in both directions.
- Weekly review's learning note may be one paragraph, synthesizing the week.

## Skills available

Run all skills via `echo '<JSON>' | python3 .claude/skills/<name>/scripts/<script>.py`:

| Skill | Script | Purpose |
|---|---|---|
| `portfolio-parse` | `parse.py` | Parse Hebrew broker exports → `portfolio/positions.json` + `transactions.json` |
| `risk-metrics` | `sharpe_dd.py` | Sharpe, Sortino, max drawdown, beta vs SPY/TA35 |
| `sector-allocation` | `sectors.py` | Weights by sector/region/currency, HHI concentration |
| `earnings-calendar` | `earnings.py` | Next earnings dates for all holdings, flags within 14 days |
| `market-regime` | `regime.py` | CNN Fear & Greed (with VIX-percentile fallback) + VIX + SPY-200DMA composite label |
| `screener` | `screen.py` | S&P 500 candidate ranking by growth/momentum/quality |
| `currency-conversion` | `fx.py` | USD↔ILS at daily-cached yfinance rate |
| `charts/dashboard` | `dashboard.py` | Interactive browser dashboard (local HTTP server, auto-opens) |
| `charts/png` | `chart.py` | Static PNG allocation donut (inline in Claude Code chat) |
| `opportunity-scanner` | `scan.py` | Three ranked opportunity lists (portfolio-fit, profile-fit, market) via the three-agent pipeline |
| `portfolio-update` | `update.py` | Records a buy/sell/trim trade and updates positions.json + snapshot + xlsx atomically |
| `momentum-check` | `momentum.py` | Per-ticker momentum metrics from yfinance cache: consecutive up-day streak, % change 5/10/30/90d, dist from 50DMA/200DMA/52w-high, RSI(14), `streak_flag` (normal/extended). No LLM. |

**Dashboard usage:**
```bash
# Ticker dashboard — starts server, opens browser, supports live ticker switching
echo '{"kind":"ticker","symbol":"NVDA"}' | python3 .claude/skills/charts/scripts/dashboard.py

# Portfolio dashboard
echo '{"kind":"portfolio"}' | python3 .claude/skills/charts/scripts/dashboard.py
```
Always run dashboard with `run_in_background=true` — it blocks until killed.

## Hooks (`.claude/hooks/`)

| Hook | Event | What it enforces |
|---|---|---|
| `enforce_risk_officer.py` | Stop | Blocks BUY/SELL/HOLD without bull+risk+portfolio-manager this turn; also blocks if no WebFetch/WebSearch was called |
| `enforce_signal_consistency.py` | Stop | Blocks cross-section ticker errors; requires raw data block before broad-review prose |
| `enforce_signal_reversal.py` | Stop | Blocks silent reversal of a signal from the last 14 days without an explicit SIGNAL CHANGE acknowledgement |
| `enforce_momentum_guardrail.py` | Stop | Calls `momentum-check` on every BUY/ADD ticker; warns (or blocks, env-flag) on extended-rally names — RSI≥75, >30% above 200DMA, or 7+ up days. Override with `MOMENTUM OVERRIDE: <reason>` line. |
| `persist_signals.py` | Stop | After blocking hooks pass, writes `research/signals/<TICKER>_<date>.json` so future turns can detect reversals |
| `deny_dangerous_bash.py` | PreToolUse:Bash | Blocks `rm -rf`, git push, broker-domain POST/PUT/DELETE |
| `warn_portfolio_write.py` | PreToolUse:Write/Edit | Warns before mutating `portfolio/` outside portfolio-parse |
| `log_skill_freshness.py` | PostToolUse:Bash | Logs skill invocations to `research/daily/<date>/data-freshness.jsonl` |
| `inject_context.py` | UserPromptSubmit | Injects today's date + citation reminder |

## Directory map (what goes where)

- `portfolio/` — user-private: parsed positions, transactions, daily snapshots. Never commit. Only `portfolio-parse` writes here without explicit user confirmation.
- `research/daily/<date>/` — each day's agent outputs, charts, and `data-freshness.jsonl`.
- `research/daily/<date>/charts/` — generated HTML dashboards and PNG charts.
- `research/signals/<TICKER>_<date>.json` — persisted trade signals for reversal detection (written by `persist_signals.py` hook).
- `research/theses/<TICKER>.md` — living thesis per holding (Phase 3+).
- `research/regime/current.json` — current macro regime label.
- `learning/glossary.md` — durable definitions.
- `learning/concepts-seen.md` — running list of concepts introduced to the user.
- `scripts/lib/` — shared Python (yfinance wrapper, FX, schema, guardrails). Skills import from here.
- `.cache/yfinance/` — daily cached price/info parquet+json files. Safe to delete.

## Daily report quality rules

Every daily report MUST:
1. Include a **macro context section** (Fed, rates, DXY, oil, recession indicators) before per-holding analysis.
2. Analyze **ALL holdings including TASE/IL** — no "informational only" cop-outs.
3. Include at least **2 SELL/REDUCE/EXIT signals** — if everything is KEEP/ADD, the analysis is dishonest.
4. Include a **"Thesis breaks at:"** line for every holding (price or metric threshold).
5. **Bridge cash deployment to opportunity lists** — read `opportunities.json` and unify the recommendation.
6. Use the **full signal vocabulary**: STRONG BUY / ADD / KEEP / HOLD / REDUCE / SELL / EXIT.

## Opportunity scanner quality rules

The scanner MUST:
1. **Deduplicate across lists** — 30 slots = 30 unique tickers, no repeats.
2. **All 30 slots are actionable buy candidates** — signals are STRONG_BUY / BUY / WATCH only. HOLD, AVOID, and SELL are forbidden in opportunity lists. WATCH entries must include a `when_to_buy` field explaining the specific trigger.
3. **Respect total cash** — per-ticker sizing capped at cash/5; totals cannot exceed available cash.
4. Mark names **reporting earnings within 7 days as WATCH** and specify in `when_to_buy` exactly what to wait for.
5. **Run Phase 2** (three-agent deep analysis) by default — `phase1_only` is opt-in, not the default.
6. **No overlapping ETFs** — never recommend two ETFs that track the same sector/index/theme (e.g. SMH+SOXX, SPY+VOO, VGT+XLK, EEM+IEMG). A `KNOWN_ETF_FAMILIES` lookup in `scan.py` catches these automatically post-Phase-1 and flags them.

## What NOT to do

- Don't pull live prices yourself via WebFetch for stock quotes — use the `data.py` cache.
- Don't write to `portfolio/` outside of `portfolio-parse` without asking first.
- Don't produce a daily-scan that includes BUY/SELL/HOLD. The dashboard is read-only by design.
- Don't quote a number you didn't get from a skill or a cited source.
- Don't skip the bear case because "the bull case is obvious."
- Don't produce a report where all signals are positive (KEEP/ADD/HOLD). Include negative calls.
- Don't label TASE holdings "informational" — analyze them fully.
- Don't skip the macro section in daily reports.
- Don't skip "thesis breaks at" for any holding.
- Don't produce opportunity lists with >20% duplicate tickers across lists.
