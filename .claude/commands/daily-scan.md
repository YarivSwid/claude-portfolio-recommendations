---
description: One-screen morning dashboard — parses fresh broker exports (if newer), refreshes FX, computes risk + allocation, surfaces concentration flags. No recommendations.
allowed-tools: Bash, Read, Task
---

# /daily-scan

A read-only morning dashboard. **Never produces BUY/SELL/HOLD.** By design — the daily view is to catch drift and surprise, not to trade.

## Steps

1. **Check freshness.** Compare mtime of `ActivePortfolio.csv.xlsx` vs `portfolio/positions.json`. If the broker file is newer (or `positions.json` doesn't exist), run `portfolio-parse` first. Emit a one-line note either way.

2. **Run the P1 analytics in parallel** (single message, three Bash calls):
   ```
   echo '{}' | python3 .claude/skills/currency-conversion/scripts/fx.py
   echo '{}' | python3 .claude/skills/sector-allocation/scripts/sectors.py
   echo '{"period":"2y"}' | python3 .claude/skills/risk-metrics/scripts/sharpe_dd.py
   ```

3. **Format the briefing** as exactly this template:

   ```
   ## Daily Scan — <YYYY-MM-DD>

   **NAV:** ₪<ils> (~$<usd>, FX <date>)  ·  **positions:** <n>

   **Allocation (top 5 weights):**
   | # | Ticker | Weight | Region | Sector | Flag |
   |---|--------|--------|--------|--------|------|

   **Diversification:** HHI <x> (<label>)
   **Region / Currency:** <US%> US · <IL%> IL  ·  <USD%> USD · <ILS%> ILS

   **Risk (2y, current weights applied historically):**
   Ann. return <x>%  ·  Ann. vol <x>%  ·  Sharpe <x>  ·  Max DD <x>%  ·  β vs SPY <x>  ·  β vs TA-35 <x>

   **Flags:**
   - <any position > 10% weight>
   - <any crypto exposure note — IBIT holding>
   - <any stale-data warning from a skill>

   **Change vs last snapshot (<date>):**
   - added: <symbols> · removed: <symbols> · qty changed: <symbols>
   ```

4. **Render the opportunity list** if `research/daily/<date>/opportunities.json` exists and `_status == "done"`. Read the file and append this section to the daily report:

   ```
   ## Opportunity Scanner — <YYYY-MM-DD>
   > <phase: full/phase1_only> · <N> unique tickers · cash available: ₪<X> (~$<Y>, FX <date>)

   ### List 1 — Portfolio Fit
   | # | Ticker | Signal | Fit | 30d Rally | vs 200DMA | Entry Note | When to Buy | Max ₪ |
   |---|--------|--------|-----|-----------|-----------|------------|-------------|-------|

   ### List 2 — Profile Fit
   | # | Ticker | Signal | Fit | 30d Rally | vs 200DMA | Entry Note | When to Buy | Max ₪ |
   |---|--------|--------|-----|-----------|-----------|------------|-------------|-------|

   ### List 3 — Market Picks
   | # | Ticker | Signal | Fit | 30d Rally | vs 200DMA | Entry Note | When to Buy | Max ₪ |
   |---|--------|--------|-----|-----------|-----------|------------|-------------|-------|
   ```

   - Populate `30d Rally` from `rally_pct_30d`, `vs 200DMA` from `price_vs_200dma`, `Entry Note` from `entry_note`, `When to Buy` from `when_to_buy`.
   - If any field is missing or `"n/a"`, show `—`.
   - After the three tables, add a **"Ripe decisions"** paragraph: name 2-3 tickers across all lists that are the most immediately actionable (STRONG_BUY with clean entry, or WATCH with an imminent trigger date). One sentence each.
   - If `opportunities.json` doesn't exist or `_status != "done"`, write: `> Opportunity scanner not yet run for today — trigger with \`echo '{}' | python3 .claude/skills/opportunity-scanner/scripts/scan.py\``

5. **End with the Learning note** (CLAUDE.md convention — one concept). Append to `learning/concepts-seen.md`.

## Hard rules for this command

- Never output BUY / SELL / HOLD in a daily scan. If the user asks for a recommendation during the scan, say: "The daily scan is read-only. Ask me `/new-idea TICKER` or a direct question to get the full desk."
- Always surface `data_as_of` and any warnings from the skill outputs.
- If any skill returns null metrics, show the raw warning — don't fill with plausible numbers.
