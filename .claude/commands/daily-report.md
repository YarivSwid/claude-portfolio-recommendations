---
description: Full broad-review daily report — macro context, per-holding analysis with signals (STRONG BUY / ADD / KEEP / HOLD / REDUCE / SELL / EXIT) derived from fundamentals + momentum + thesis breaks, opportunity bridge, and an informational Trend & Stops Reference block at the bottom. Stops are never used as a sell signal. Writes research/daily/<today>/report.md.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Task, WebSearch, WebFetch
---

# /daily-report

The full broad-review desk report. Unlike `/daily-scan` (read-only dashboard, never emits signals), this command produces grounded per-holding advice and writes the rendered output to `research/daily/<today>/report.md` so the dashboard `/report` tab can render it.

This is the **broad-review flow** per CLAUDE.md — no bull/bear/manager subagent invocation. Per-holding prose grounded in skill outputs, fundamentals, and primary-source research.

## CRITICAL — stops are informational only

The `stops` skill computes price-level reference data (Chandelier trader stop + 200DMA investor stop for stocks, 200DMA for ETFs, 5×ATR for crypto ETFs). It does NOT emit action tokens. The `momentum-check` `trend_flags` (golden_150_cross, death_200_cross, etc.) are descriptive context, not actions.

**Per-holding signals (STRONG BUY / ADD / KEEP / HOLD / REDUCE / SELL / EXIT) are derived from:**
1. Fundamentals + cited primary sources
2. `momentum-check` `flags` (RSI≥75 + far_above_200dma + extended streak) — gates ADD into already-extended names
3. Earnings within 14 days — converts ADD candidates to HOLD-until-after-print
4. ETF 200DMA break **with macro context** (i.e. not a sell signal in a Greed/ATH regime; can be a sell signal in a Neutral or Fear regime if the ETF is a thematic call you're losing confidence in)
5. Explicit thesis-break from `research/theses/<TICKER>.md` when it exists
6. Profit-taking analyst call when a position has run materially from cost basis (analyst must explicitly justify, not auto-derived)

**Signals are NOT derived from:**
- Stock `trader_status: below` or `near` (Chandelier trigger) — informational only
- Stock `investor_status: below` (200DMA break on a single stock) — informational; in a Greed/ATH regime usually means "lagging the rally," not "selling signal"
- `trend_flags` alone — `death_150_cross` on a quality compounder in a strong tape is context, not a sell trigger

If you find yourself writing "stop triggered → REDUCE" the rule is being violated. Re-justify from fundamentals or remove the negative signal.

## Steps

1. **Determine today's date** and the output path:
   - `TODAY = $(date +%Y-%m-%d)`
   - `OUT = research/daily/$TODAY/report.md`
   - Create parent directory if missing.

2. **Refresh portfolio data.** If broker export mtime is newer than `portfolio/positions.json`, run `portfolio-parse` first. Emit a one-line note either way.

3. **Run the analytics in parallel** (single message, parallel Bash calls):
   ```
   echo '{}' | python3 .claude/skills/currency-conversion/scripts/fx.py
   echo '{}' | python3 .claude/skills/sector-allocation/scripts/sectors.py
   echo '{"period":"2y"}' | python3 .claude/skills/risk-metrics/scripts/sharpe_dd.py
   echo '{}' | python3 .claude/skills/market-regime/scripts/regime.py
   echo '{}' | python3 .claude/skills/earnings-calendar/scripts/earnings.py
   echo '{}' | python3 .claude/skills/stops/scripts/stops.py
   echo '{}' | python3 .claude/skills/early-movers/scripts/early.py
   echo '{}' | python3 .claude/skills/momentum-check/scripts/momentum.py
   ```

4. **Gather macro context.** WebFetch / WebSearch for current Fed funds rate, 10-yr yield, DXY, oil (WTI), and any recent FOMC commentary or recession indicators. Cite every number with an inline `(Source — YYYY-MM-DD)`.

5. **Read `research/user-views.md`** to frame recommendations against the user's stated views. If the file does not exist, note that the workbench is unconfigured and stop with a clear error.

6. **Render the report and write it to `$OUT`.** Exact template:

   ```markdown
   # Daily Report — <YYYY-MM-DD>

   > Broad review. Educational analysis, not investment advice.

   ## Macro Context
   - Fed funds: <x>% (Source — YYYY-MM-DD)
   - 10-yr: <x>% · DXY: <x> · WTI: $<x>
   - Regime: <label from market-regime skill> · F&G <x> · VIX <x> · SPY vs 200DMA: <x>%
   - One-paragraph read on what this means for risk-on vs risk-off today.

   ## Portfolio Snapshot
   **NAV:** <amount in user's home currency, FX <date> if multi-currency> · **positions:** <n>
   **HHI:** <x> (<label>) · **Sharpe (2y):** <x> · **Max DD:** <x>%
   _(risk-metrics, sector-allocation — <YYYY-MM-DD>)_

   ## Per-holding Analysis
   For EACH holding (no "informational only" cop-outs — analyze every position regardless of exchange):
   - **<TICKER>** (<Full Name>) — **<STRONG BUY|ADD|KEEP|HOLD|REDUCE|SELL|EXIT>** · weight <x>%
   - One sentence on the thesis as-of today.
   - One sentence on recent price/fundamentals (cite numbers from skill outputs or yfinance cache).
   - One sentence stating the current bear case (what would I be wrong about?).
   - **Thesis breaks at:** <FUNDAMENTAL threshold — revenue/guide/margin/growth/competitive/regulatory metric>. Example: "ad-rev growth < 10% YoY for 2 consecutive quarters" or "Azure CC growth < 25% for 2 consecutive prints" or "EPS guide cut > 15% intra-quarter". **Never** a stop price alone — a bare "$13.80" is not a thesis-break, it's a chart level.
   - **Technical reference:** <stop / DMA / RSI level from the bottom Trend & Stops block — FYI only, never the cause of a signal>. Example: "Chandelier 3×ATR at $13.80; 200DMA at $15.10; status: below 200DMA, above Chandelier."

   Formatting rules for per-holding entries:
   - Signal token comes FIRST in the header line (before weight), so it's immediately visible on scan.
   - For very small positions where TRIM is not size-appropriate (threshold defined in `research/user-views.md`), write in plain English: "Position below minimum size for TRIM — sized as HOLD instead." Do NOT write parenthetical workbench rule citations.
   - Omit any workbench-internal parentheticals (rule citations, hook names, skill names) from per-holding prose. Keep the substance, not the machinery.

   Why two separate lines: the analyst (LLM) decides the signal from fundamentals + momentum + thesis breaks. The Technical reference is situational awareness, NOT input to the decision. Putting a stop price in "Thesis breaks at:" is the most common drift pattern — keeping the slots separate prevents it. The `enforce_stop_signal_separation.py` Stop hook will warn (and, when promoted to BLOCK mode, block) any REDUCE/SELL/EXIT cited on the same line as Chandelier/ATR/200DMA without a fundamental cause within 2 lines.

   **Honesty rule (not a quota):** the number of REDUCE/SELL/EXIT signals reflects reality.
   In a Greed/ATH regime with no fundamental breaks, 0–1 negatives is honest. In a Neutral
   or Fear regime with real fundamental concerns, more negatives are warranted. NEVER
   manufacture a negative to satisfy a count, and NEVER cite a stop trigger as the cause —
   if the only reason for REDUCE is "the Chandelier fired," remove the negative signal.

   ## Early Movers
   Render the `early-movers` skill output. Header text depends on `gate`:
   - `gate=open` → "Early Movers — 150DMA Setup" with `signal: BUY` rows.
   - `gate=closed` → "Early Movers — WATCH ONLY (macro gate)" with every row showing `signal: WATCH (macro)` and `macro_veto`.

   If `gate=closed`, add a caution badge at the top of every BUY surface in the report:
   > ⚠ Macro caution — SPY <200DMA. The 150DMA early-movers list is currently WATCH-only; broader BUY signals still emit but should be sized conservatively.

   ## Opportunity Bridge
   **Read available cash first.** Look up `cash_ready_usd` at the top level of `portfolio/positions.json`. If present, use that as the cash base for sizing (convert to the user's home currency via `currency-conversion` if needed). If absent, fall back to "$40,000" and call this out explicitly: *"Cash ready not set — using $40,000 default. Set it in the Portfolio tab to size proposals against your real cash."*

   **Read `research/daily/<today>/opportunities.json` and branch on `_status`:**

   - **`_status == "done"`** (Phase 2 complete — full 3-agent debate per ticker)
     - Label the section: *"Opportunities — Phase 2 deep analysis (bull-officer + risk-officer + portfolio-manager debate complete per ticker)."*
     - Name 2-3 ripe tickers (across lists 1/2/3) with `BUY` or `STRONG_BUY` signals and clean entry, sized against actual cash. Per-ticker sizing capped at `max_invest_usd` from the scanner output.

   - **`_status == "done:phase1_only"`** (Phase 1 only — fast screener + one-line bull/risk per ticker)
     - Label the section: *"Opportunities — Phase 1 screening (one-line bull / risk per ticker via fast LLM prompt; full 3-agent debate not run). To get manager-level vetting before deploying, run: `echo '{\"cash_ils\": <X>, \"date\": \"<TODAY>\", \"phase1_only\": false}' | python3 .claude/skills/opportunity-scanner/scripts/scan.py` (~$30-50, ~25 min)."*
     - Phase 1 already includes per-candidate `signal`, `max_invest_ils`, `bull_thesis`, `bear_threshold`. Sizing IS allowed (the scanner caps per-ticker at `cash_ils/5` and total at `cash_ils`), but the prose must clearly state the analysis depth so the user can decide whether to act now or wait for Phase 2.
     - Name 2-3 ripe tickers with `BUY` signals; for each, quote the `bull_thesis` and `bear_threshold` verbatim from the JSON (these are the LLM's one-liners, not your editorial).

   - **`_status` starts with `"phase1_failed:"`** (one or more lists failed)
     - Write: `> Opportunity scanner Phase 1 failed for lists: <names>. See research/daily/<TODAY>/scan-debug.log. Proposing deploys only from existing holdings until the scanner is re-run.`
     - Skip the new-name sizing; proceed with existing-holdings ADD candidates only.

   - **`_status == "running"`** (in progress, partial file written)
     - Write: `> Scanner vetting is still running — ADD candidates below are from existing holdings only; full scanner output will update the next report.`

   - **`opportunities.json` missing entirely**
     - Write: `> Opportunity scanner has not run today. Trigger it via the dashboard ▶ button, or ask Claude to run the scanner.`
     - Propose deploys only from existing holdings.

   - **`_status` present but not one of the values above** (corrupt/unknown)
     - Write: `> Opportunities file present but _status="<value>" is unknown — refusing to size from possibly partial output. Re-run the scanner.`
     - Skip new-name sizing.

   ## Earnings on Deck
   From `earnings-calendar` output: list holdings with earnings within 14 days, with date and consensus EPS estimate.

   ## Ripe Decisions
   2-3 concrete actions the user could take this week, citing per-holding signals above.

   If no REDUCE/SELL/EXIT signals were warranted today, the final Ripe Decisions item MUST be the dismissal paragraph (this is the required "No negative signals" honesty floor — do not place it inside the Per-holding section):
   > **No negative signals warranted today.** Here's what I looked at and dismissed: <specific list — stop triggers on X/Y/Z were noise; thesis intact; extension gates ADD but not a sell cause; etc.>

   Do NOT duplicate this paragraph inside the Per-holding Analysis section — it belongs here only.

   ---

   ## Trend & Stops Reference (informational — does not drive signals above)

   _Informational only. Stop levels and trend context are reference data; per-holding signals above are driven by fundamentals, not these tables. A `below` status is never a REDUCE/SELL/EXIT cause. Prices as of <YYYY-MM-DD>._

   ### Stocks — dual stop reference

   | Ticker | Current | Trader stop (3×ATR) | Δ% | Status | Investor stop (200DMA) | Δ% | Status |
   |---|---|---|---|---|---|---|---|
   | NVDA | $212.60 | $213.58 | -0.5% | below | $187.34 | +13.5% | above |
   | ... | | | | | | | |

   ### ETFs / mutual funds — 200DMA reference

   | Ticker | Current | 200DMA | Δ% | Status |
   |---|---|---|---|---|
   | IGV | $93.05 | $98.94 | -6.0% | below |
   | ... | | | | |

   ### Crypto ETFs — 5×ATR Chandelier reference

   | Ticker | Current | 5×ATR stop | Δ% | Status |
   |---|---|---|---|---|
   | IBIT | $42.45 | $40.78 | +4.1% | near |

   ### Trend context (`momentum-check.trend_flags` + days-above MAs)

   For every holding, render `pct_vs_150dma`, `pct_vs_200dma`, `days_above_150dma`,
   `days_above_200dma`, and any `trend_flags` from momentum-check. Highlight:
   - `golden_150_cross` / `golden_200_cross` — bullish stage-2 setup (informational tilt for ADD candidates if fundamentals support)
   - `death_150_cross` / `death_200_cross` — bearish trend warning (informational; analyst weighs against fundamentals before any signal change)
   - `golden_cross_50_200` / `death_cross_50_200` — classic 50/200 crosses

   If `summary.stocks_below_investor_stop` is non-empty AND the macro regime is Greed/ATH,
   add a one-line note: *"<TICKERS> are below their 200DMA in a strong tape — they're
   lagging the rally, not breaking down. Watch for divergences if the broader tape weakens."*

   ---
   Learning note
   Concept: <one named concept>
   Why it mattered here: <one sentence>
   Go deeper: <optional pointer>
   ```

7. **Append the Learning note's concept to `learning/concepts-seen.md`** in the form ``- YYYY-MM-DD — <concept>``.

8. **Persist any BUY/SELL/HOLD signals** — the `persist_signals.py` Stop hook handles this automatically; just emit the signals in the report and the hook writes `research/signals/<TICKER>_<date>.json`.

## Hard rules for this command

- **Write the full report to `research/daily/<today>/report.md` before ending the turn.** The dashboard `/report` tab reads this file — if it isn't written, the button appears to have failed.
- All daily-report quality rules from CLAUDE.md apply (macro section, all holdings analyzed, "Thesis breaks at" for every holding, opportunity bridge, full signal vocabulary, Trend & Stops Reference at the bottom, early-movers section).
- **Stops are informational only.** Per-holding signals (REDUCE/SELL/EXIT) must NOT be derived from `trader_status: below`, `investor_status: below` on a single stock, or any `trend_flags` alone. Re-justify any negative signal from fundamentals or remove it.
- **No quota on negative signals.** The honesty floor is per-holding critical thinking (bear case + thesis-break line), not a count of REDUCEs.
- Show monetary figures in the user's home currency as defined in `research/user-views.md`. If multi-currency portfolio, show both with FX date.
- Every numeric claim cites a skill output, the yfinance cache, or a primary-source URL with date.
- No bull/bear/manager subagent calls — this is the broad-review flow, not the specific-decision flow.
- If yfinance or any skill returns stale data (>24h), flag it explicitly in the report; do not paper over with plausible numbers.
- End with the personal-advice disclaimer: *"Educational analysis, not investment advice. Decisions are yours."*

## Pre-write verification checklist

Walk this checklist mentally before invoking `Write` on the report file. Every box must pass. If any fails, fix the draft in memory and re-check before writing.

```
[ ] Macro Context section present (Fed funds, 10y, DXY, WTI, regime label, one-paragraph read)
[ ] Portfolio Snapshot present (NAV with FX date if applicable, HHI, Sharpe, Max DD)
[ ] Per-Holding Analysis covers EVERY ticker in positions.json (no holdings dropped, no "informational only")
[ ] Every per-holding entry has ALL FIVE elements:
    [ ] signal token (STRONG BUY|ADD|KEEP|HOLD|REDUCE|SELL|EXIT)
    [ ] one-sentence current bear case
    [ ] "Thesis breaks at:" line with a FUNDAMENTAL metric threshold (revenue/guide/margin/growth/competitive/regulatory)
    [ ] "Technical reference:" line with stop/DMA/RSI level
    [ ] no $-only value in "Thesis breaks at:" (e.g., "Thesis breaks at: $13.80" alone is INVALID — must be metric-anchored)
[ ] No REDUCE/SELL/EXIT signal on the same line as Chandelier/ATR/200DMA without a fundamental cause within 2 lines above (this is what enforce_stop_signal_separation.py enforces)
[ ] If signals are all KEEP/HOLD/ADD, explicit "No negative signals warranted today — here's what I looked at and dismissed: ..." paragraph is present as the FINAL Ripe Decisions item (honesty floor, not a quota — must be in Ripe Decisions, NOT inside Per-holding Analysis)
[ ] Early Movers section present (header text matches macro gate: open → "150DMA Setup" with BUY; closed → "WATCH ONLY (macro gate)" with WATCH (macro))
[ ] If macro gate is closed, caution badge present at top of every BUY surface in the report
[ ] Opportunity Bridge: cash_ready_usd looked up from positions.json (or default disclosure); opportunities.json read AND its `_status` value explicitly checked; section labeled with the analysis depth ("Phase 2 deep analysis" / "Phase 1 screening" / one of the fallback messages); sizing tied to actual cash; per-ticker sizing ≤ max_invest_ils from the scanner
[ ] Earnings on Deck present (or explicit "no earnings within 14 days")
[ ] Ripe Decisions: 2-3 concrete actions citing per-holding signals
[ ] Trend & Stops Reference block is at the BOTTOM of the report (after Ripe Decisions), explicitly labeled informational
[ ] Learning note present, concept novel relative to learning/concepts-seen.md
[ ] Personal-advice disclaimer present
```

## Post-write verification

After writing the report, run these in a single message (parallel):

```bash
test -s "research/daily/$(date +%Y-%m-%d)/report.md" && echo "report written" || echo "ERROR: report.md missing or empty"

# Backstop the firewall hook — any line that names a REDUCE/SELL/EXIT signal AND
# cites the stop/Chandelier/200DMA as its cause without a fundamental cause nearby
# is a bug. The enforce_stop_signal_separation.py hook is the canonical enforcer;
# this grep surfaces obvious cases for human review.
grep -nE '(REDUCE|SELL|EXIT).*(stop|chandelier|trigger|200DMA|200dma|150DMA|150dma)' "research/daily/$(date +%Y-%m-%d)/report.md" || echo "no stop-derived negatives — good"

# Confirm Trend & Stops Reference comes AFTER Ripe Decisions (i.e., bottom-of-report,
# not mid-analysis). The structural check is robust to report length.
# (`|| true` swallows grep exit 1 under pipefail when section is missing.)
REPORT_PATH="research/daily/$(date +%Y-%m-%d)/report.md"
ripe_line=$(grep -n '^## Ripe Decisions' "$REPORT_PATH" 2>/dev/null | head -1 | cut -d: -f1 || true)
trend_line=$(grep -n '^## Trend & Stops Reference' "$REPORT_PATH" 2>/dev/null | head -1 | cut -d: -f1 || true)
if [ -z "$trend_line" ]; then echo "ERROR: Trend & Stops Reference section missing"
elif [ -z "$ripe_line" ]; then echo "ERROR: Ripe Decisions section missing"
elif [ "$trend_line" -lt "$ripe_line" ]; then echo "ERROR: Trend & Stops Reference at line $trend_line appears BEFORE Ripe Decisions at line $ripe_line — must come AFTER the main analysis"
else echo "Trend & Stops Reference correctly positioned after Ripe Decisions (L$trend_line > L$ripe_line) — good"; fi

# Confirm every per-holding entry has both "Thesis breaks at:" and "Technical reference:".
held=$(python3 -c "import json; d=json.load(open('portfolio/positions.json')); print(len(d if isinstance(d, list) else (d.get('positions') or d.get('holdings') or [])))")
breaks=$(grep -c '^- \*\*Thesis breaks at:' "$REPORT_PATH" 2>/dev/null || echo 0)
techrefs=$(grep -c '^- \*\*Technical reference:' "$REPORT_PATH" 2>/dev/null || echo 0)
if [ "$breaks" -ge "$held" ] && [ "$techrefs" -ge "$held" ]; then
  echo "per-holding template coverage: ${breaks} thesis-breaks, ${techrefs} technical-refs for ${held} holdings — good"
else
  echo "ERROR: incomplete per-holding template — ${breaks} thesis-breaks, ${techrefs} technical-refs for ${held} holdings"
fi
```

If any post-write check fails, do not claim the report is generated — fix the file and re-verify.
