#!/bin/bash
# daily_run.sh — example automation: triggered by launchd / cron on a daily schedule.
# Runs the daily portfolio report via Claude Code, then sends the result.
#
# To use: set PROJECT to the absolute path of your clone, adjust the cash_ils
# value below to your available cash, and customize the report sections to taste.

set -euo pipefail

# REQUIRED: set this to your absolute clone path, or export it in your shell rc.
PROJECT="${PROJECT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="$PROJECT/research/logs"
TODAY=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/daily_run_$TODAY.log"

mkdir -p "$LOG_DIR"
exec >> "$LOG_FILE" 2>&1

echo "=== Daily run started at $(date) ==="

cd "$PROJECT"

# Run Claude Code with the daily-report command
# --dangerously-skip-permissions allows unattended run
claude --dangerously-skip-permissions -p "
You are the investing workbench orchestrator. Run the full daily report for today ($TODAY).

Steps to follow IN ORDER:

1. Run portfolio-parse to get fresh positions (check if broker file is newer than last snapshot first).

2. Run these skills IN PARALLEL:
   - sector-allocation
   - risk-metrics (benchmarks: SPY and TA35.TA)
   - earnings-calendar (window_days: 14)
   - market-regime

3. Run WebSearch for: 'stock market news today $TODAY major movers earnings' to get today's market context.

4. Per-holding deep-dive — TRIGGERED ONLY, NOT BLANKET (cost control 2026-04-27).
   Running bull/risk/manager on every US holding every day costs ~$30-45/run and gives near-zero
   marginal value on names with no fresh signal. Instead, deep-dive only the holdings that meet
   AT LEAST ONE of these triggers:
     a) Earnings within the next 7 days (from earnings-calendar output)
     b) Price moved >5% in either direction since the previous snapshot
     c) New 52-week high or 52-week low touched
     d) The holding appeared in today's opportunity-scanner list with a BUY or STRONG_BUY signal
        (means the candidate-list LLM flagged it as actionable — worth a deeper look)
   For triggered tickers ONLY:
   - Run bull-officer and risk-officer IN PARALLEL
   - Then run portfolio-manager to synthesize
   - Collect the signal and confidence
   For all OTHER holdings, write a one-line status row in the report from existing skill data
   (price, weight, vs-200DMA, last cached signal if any) without re-running the agents.

5. Run the opportunity-scanner skill to generate all 3 opportunity lists:
   echo '{"cash_ils": 120000, "date": "$TODAY"}' | python3 .claude/skills/opportunity-scanner/scripts/scan.py
   This runs 3 lists in parallel (portfolio-fit, profile-fit, market-picks). Wait for it to complete.
   The top 3 scores from list1 go into the ## Opportunities section of the report.

6. Write the full report to research/daily/$TODAY/report.md with these sections IN ORDER:

   ## TL;DR — Today's Priorities (THIS SECTION COMES FIRST — max 6 bullets)
   Write a short punchy summary covering:
   - Top 1-2 action signals (ADD/BUY/TRIM/SELL) — label which agent drove it, e.g. "(bull-officer: ...)"
   - Biggest risk today (earnings, macro, single-position) — label "(risk-officer: ...)"
   - Any position down >30% from cost that needs attention
   - Earnings tonight or tomorrow (binary events)
   Keep each bullet under 15 words. This is what the user reads on their phone first.

   ## Portfolio Snapshot
   ## Market Context (regime, VIX, Fear & Greed, today's news)

   IMPORTANT for news: Only include headlines you actually fetched and verified via WebSearch/WebFetch.
   Do NOT include any news item you cannot cite with a real URL from a real source.
   If a headline sounds plausible but you cannot find a primary source URL — omit it entirely.
   Write "No verified headlines found" rather than guessing.

   ## Raw Position Data Block
   ## Holdings Review (per-ticker)

   For EACH ticker write exactly this structure:

   ### TICKER — SIGNAL | Confidence: LEVEL
   **🐂 Bull-officer:** one sentence — the strongest bull point found (cite source if from web)
   **🛡 Risk-officer:** one sentence — the key risk identified
   **🧠 Portfolio-manager:** one sentence — final synthesis and rationale for signal
   **Data as of:** YYYY-MM-DD

   IMPORTANT — flag these explicitly in Holdings Review:
   - Any position with unrealized loss > 30% from cost basis — add a ⚠️ WARNING line
   - Any position below ₪10,000 that has been a loser for > 6 months — flag as 'review exit'
   - Israeli mutual funds: flag the annual fee drag in ILS explicitly

   ## Earnings Watch (holdings reporting within 14 days)
   ## Opportunities (top 5 screener candidates with brief bull rationale)
   ## Cash Deployment (if cash > 5% NAV, specific sizing suggestions)

   Every monetary figure must show both ILS and USD.
   End the report with: 'Educational analysis, not investment advice. Decisions are yours.'

7. Confirm report.md was written successfully.
" 2>&1

echo "=== Claude finished at $(date) ==="

# Send the Telegram report
python3 "$PROJECT/scripts/send_report.py"

echo "=== Telegram sent at $(date) ==="
echo "=== Daily run complete ==="
