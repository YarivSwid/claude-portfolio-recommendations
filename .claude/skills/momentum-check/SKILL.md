---
name: momentum-check
description: Computes deterministic momentum, overbought signals, and trend-context flags per ticker — consecutive up-day streak, % change over 5/10/30/90d, distance from 50DMA/150DMA/200DMA/52w-high, RSI(14), 150DMA and 200DMA price-MA crossovers (Weinstein stage-2 setups), and classic 50/200 golden/death crosses. Returns a streak_flag ("normal" | "extended") used to gate BUY recommendations, plus a trend_flags array that is descriptive context for the analyst (never auto-triggers BUY or SELL). Reads yfinance cache via scripts.lib.data — no LLM, no WebSearch.
---

# momentum-check

## When to use

- Before the opportunity-scanner LLM lists, to attach exact momentum metrics to each candidate ticker.
- Before any specific-decision BUY recommendation, to verify the name isn't momentum-extended.
- Manual lookup: "is NVDA overbought?" / "how many days has X gone up in a row?"

## Why it exists

Replaces the WebSearch-based `rally_pct_30d` and `price_vs_200dma` estimates the scanner used to make. Those numbers were LLM guesses from search snippets; these numbers come from cached yfinance closes.

## Input

```bash
# single ticker
echo '{"ticker":"NVDA"}' | python3 .claude/skills/momentum-check/scripts/momentum.py

# batch (recommended for scanner candidate bundles)
echo '{"tickers":["NVDA","AVGO","META","IWM","TEVA.TA"]}' | python3 .claude/skills/momentum-check/scripts/momentum.py
```

## Output

```json
{
  "data_as_of": "2026-04-29",
  "results": {
    "NVDA": {
      "last_close": 201.68,
      "as_of": "2026-04-28",
      "consecutive_up_days": 4,
      "consecutive_down_days": 0,
      "pct_change_5d": 0.038,
      "pct_change_10d": 0.071,
      "pct_change_30d": 0.272,
      "pct_change_90d": 0.41,
      "pct_vs_50dma": 0.084,
      "pct_vs_150dma": 0.121,
      "pct_vs_200dma": 0.156,
      "days_above_50dma": 18,
      "days_above_150dma": 42,
      "days_above_200dma": 87,
      "trend_flags": [],
      "dist_from_52w_high_pct": -0.012,
      "rsi_14": 68.4,
      "streak_flag": "normal",
      "flags": [],
      "tier": "normal"
    }
  },
  "warnings": []
}
```

## streak_flag rules

`streak_flag = "extended"` when ANY of:
- `consecutive_up_days >= 7`
- `rsi_14 >= 75`
- `pct_vs_200dma >= 0.30` (more than 30% above 200DMA)
- `pct_change_30d >= 0.25` AND `dist_from_52w_high_pct >= -0.02` (rallied hard AND at ATH)

When extended, the `flags` array names which condition tripped (e.g. `["rsi_overbought","far_above_200dma"]`).

## trend_flags (descriptive context — NOT actions)

The `trend_flags` array surfaces moving-average regime changes for the analyst's situational awareness. They are **descriptive**: a `golden_150_cross` is bullish context, not an auto-BUY; a `death_200_cross` is bearish context, not an auto-SELL. The daily-report rule (CLAUDE.md) forbids deriving signals from trend flags alone — the analyst weighs them alongside fundamentals, thesis files, and momentum extension.

Detection rules:

| Flag | Meaning |
|---|---|
| `golden_150_cross` | Close crossed from below to above the 150DMA within the last 5 trading days, after being below for ≥20 days. Classic Stan Weinstein **stage-2 breakout** setup. |
| `death_150_cross` | Symmetric — broke below 150DMA after a ≥20-day run above. Stage-4 breakdown setup. |
| `golden_200_cross` | Same logic vs the 200DMA — long-horizon trend change to up. |
| `death_200_cross` | Long-horizon trend change to down. |
| `golden_cross_50_200` | 50DMA crossed above 200DMA in the last 5 trading days. Classic golden cross. |
| `death_cross_50_200` | 50DMA crossed below 200DMA in the last 5 trading days. Classic death cross. |

The 20-day prior-run filter on the price-MA crosses prevents whipsaw flagging when price oscillates around the MA line. MA-on-MA crosses don't need that filter — they're inherently slower.

`days_above_150dma` and `days_above_200dma` are current consecutive streaks (counted backwards from the latest bar); `0` means the most recent bar closed below the MA.

## Notes

- Uses 1y of daily closes from `scripts.lib.data.fetch_history`. Adjusted closes (auto_adjust=True).
- If fewer than 200 trading days are available (new IPO, illiquid TASE name), 200DMA fields return `null` and a `"insufficient_history"` warning is added per ticker.
- TASE tickers must use the `.TA` suffix.
- Pure Python, no LLM, runs in <2s for 30 tickers from cache.
