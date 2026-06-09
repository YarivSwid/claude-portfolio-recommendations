---
name: early-movers
description: Surfaces S&P 500 names showing an early 150DMA setup — either a fresh breakout above the 150DMA (was below ≥30 days, now above, not extended) or a pullback to the 150DMA in an established uptrend (200DMA rising). Applies a strict macro gate: if SPY is at or below its 200DMA, all candidates are downgraded to WATCH with `macro_veto` set — no BUY tokens emitted. Use when the user asks for "early breakouts", "stocks turning up", "fresh 150DMA names", or anywhere the daily report wants an offensive list that respects broader-market discipline.
---

# Early-movers skill

A discovery filter for trend setups, with a hard macro veto.

## Setup detection per ticker

For each S&P 500 name (or the user-supplied list):

- **`breakout`** — Today's close > 150DMA AND the prior 30 trading days were all below the 150DMA AND price is not extended (≤10% above 150DMA). Fresh trend, not chased.
- **`pullback`** — 200DMA slope positive over the last 30 trading days AND `|price − 150DMA| / 150DMA ≤ 2%`. Classic buy-the-dip-in-a-strong-name.
- **`neither`** — Excluded from output.

Stocks can match only one bucket; `pullback` is checked first because it's the tighter setup.

## Macro gate (the discipline)

Computes SPY's position vs its own 200DMA the same way `market-regime` does.

- **SPY > 200DMA** → bucket signals are `BUY`. Default action.
- **SPY ≤ 200DMA** → bucket signals are downgraded to `WATCH`, with `macro_veto: "SPY <200DMA — broader trend off; revisit when SPY recovers above 200DMA"`. No BUY tokens emitted.

The veto is hard. The user explicitly asked for "don't let me buy them unless the big 200 DMA also says the broader market is safe."

## Usage

```bash
# Default — full S&P 500 universe, default macro gate
echo '{}' | python3 .claude/skills/early-movers/scripts/early.py

# Subset by ticker
echo '{"tickers": ["NVDA","AVGO","TSM"]}' | python3 .claude/skills/early-movers/scripts/early.py

# Override the lookback for "fresh breakout" (default 30 days)
echo '{"breakout_lookback_days": 45}' | python3 .claude/skills/early-movers/scripts/early.py
```

## Output shape

```json
{
  "data_as_of": "2026-05-28",
  "macro": {
    "spy_close": 612.34,
    "spy_200dma": 580.21,
    "spy_pct_vs_200dma": 0.0554,
    "gate": "open"
  },
  "breakouts": [
    {
      "ticker": "X",
      "name": "...",
      "sector": "...",
      "close": 42.10,
      "ma_150": 41.55,
      "pct_above_150dma": 0.0132,
      "days_below_before_breakout": 47,
      "signal": "BUY",
      "macro_veto": null
    }
  ],
  "pullbacks": [...],
  "stats": {"universe_scanned": 503, "data_errors": 4, "breakouts_found": 7, "pullbacks_found": 12}
}
```

When `gate == "closed"` every candidate's `signal` is `WATCH` and `macro_veto` carries the reason.

## What this is NOT

- Not a buy ticket. The 150DMA setup is one signal; portfolio-fit, sizing, and fundamentals still live in the three-agent pipeline.
- Not a substitute for `momentum-check` or `opportunity-scanner`. Those gate on RSI / 200DMA / parabolic filters. This skill is an *earlier* surfacing mechanism.
- Not a macro-regime tool. It reads SPY-vs-200DMA only. For full regime (F&G, VIX, composite label) use `market-regime`.

## Daily-report integration

The daily report renders breakouts and pullbacks as two short tables under "Early Movers — 150DMA Setup". If `gate == "closed"`, the section header reads "Early Movers — WATCH ONLY (macro gate)" and every row's signal column shows `WATCH (macro)`.

Per CLAUDE.md rule: the *other* BUY surfaces (opportunity-scanner outputs, broad-review per-holding ADD signals) get a *caution badge* (not a hard veto) when SPY < 200DMA. The hard veto is scoped to early-movers only — that was the user's explicit choice.
