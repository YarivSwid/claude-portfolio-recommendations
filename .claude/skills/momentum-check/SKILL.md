---
name: momentum-check
description: Computes deterministic momentum and overbought signals per ticker — consecutive up-day streak, % change over 5/10/30/90d, distance from 50DMA/200DMA/52w-high, RSI(14). Returns a streak_flag ("normal" | "extended") used to gate BUY recommendations. Reads yfinance cache via scripts.lib.data — no LLM, no WebSearch.
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
      "pct_vs_200dma": 0.156,
      "dist_from_52w_high_pct": -0.012,
      "rsi_14": 68.4,
      "streak_flag": "normal",
      "flags": []
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

## Notes

- Uses 1y of daily closes from `scripts.lib.data.fetch_history`. Adjusted closes (auto_adjust=True).
- If fewer than 200 trading days are available (new IPO, illiquid TASE name), 200DMA fields return `null` and a `"insufficient_history"` warning is added per ticker.
- TASE tickers must use the `.TA` suffix.
- Pure Python, no LLM, runs in <2s for 30 tickers from cache.
