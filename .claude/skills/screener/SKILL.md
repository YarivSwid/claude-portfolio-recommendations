---
name: screener
description: Generates a ranked candidate list of US stocks (S&P 500 universe by default) for the three-agent pipeline. Filters by market cap, liquidity, and basic quality; ranks by a composite of growth + momentum + valuation-sanity. Outputs JSON the bull/bear/manager agents consume. Use when the user asks "find stocks," "pick N stocks from the market," or before any multi-name three-agent analysis.
---

# screener

## When to use

- Before any "pick N stocks" request. The three agents (bull-officer, risk-officer, portfolio-manager) are **decision-stage** — they evaluate a given ticker. They do not screen. This skill produces the candidate universe they run against.
- When the user asks for thematic picks ("AI infrastructure", "beaten-down quality"), you may run this skill with a sector filter and hand the top candidates to the three-agent pipeline.
- **Not a recommendation.** Output is a ranked candidate list — bull/bear/manager still decide.

## Input

```bash
echo '{"universe": "sp500", "top_n": 20, "sector": null, "min_market_cap": 10e9, "min_adv_usd": 50e6}' \
  | python3 .claude/skills/screener/scripts/screen.py
```

Keys (all optional):
- `universe`: `"sp500"` (default). Future: `"nasdaq100"`, `"custom"`.
- `top_n`: candidates to return (default 20).
- `sector`: filter to one sector (e.g. `"Technology"`, `"Communication Services"`). Default: no filter.
- `min_market_cap`: USD minimum (default 10e9 — large-cap). Set lower for mid/small-cap screens.
- `min_adv_usd`: avg-daily-volume × price minimum (default 50e6).
- `include_etfs`: append common sector/broad ETFs (SMH, XLV, EFA, TLT, etc) to the universe so the three-agent pipeline can consider them alongside single names (default true; set false to exclude). Ignored when `sector` filter is set.
- `force_refresh`: rebuild the universe cache (default false).

## Output

```json
{
  "data_as_of": "2026-04-20",
  "universe": "sp500",
  "filters_applied": {
    "min_market_cap": 10000000000,
    "min_adv_usd": 50000000,
    "sector": null
  },
  "candidates": [
    {
      "ticker": "NVDA",
      "name": "NVIDIA Corporation",
      "sector": "Technology",
      "market_cap_usd": 3150000000000,
      "fwd_pe": 32.1,
      "rev_growth_yoy": 0.72,
      "gross_margin": 0.75,
      "pct_above_200dma": 0.18,
      "adv_usd": 4200000000,
      "score": 0.87,
      "score_breakdown": {
        "growth": 0.35,
        "momentum": 0.27,
        "quality": 0.25
      },
      "priority_flags": []
    }
  ],
  "skipped": {
    "missing_fundamentals": ["XYZ"],
    "failed_market_cap": 142,
    "failed_adv": 18
  },
  "warnings": []
}
```

## Ranking formula

Composite score ∈ [0, 1], weighted sum:
- **Growth** (0.40): revenue YoY growth, capped at 50% (anything above gets 0.40).
- **Momentum** (0.30): % above 200DMA, capped at ±30%.
- **Quality** (0.30): gross margin (capped at 70%) + positive-earnings indicator.

Names missing a component get the component weight × 0.3 (penalty, not zero — preserves optionality).

## Priority flags (informational, non-blocking)

- `below_micro_cap_floor` — US market cap < $300M
- `valuation_stretch` — fwd P/E > 60 without rev growth > 30%
- `low_liquidity` — ADV < $20M
- `negative_momentum` — price < 200DMA by >10%

Flags are surfaced to the bull-officer so it knows which priority concerns to address.

## Caching

- Universe (S&P 500 constituents): cached to `.cache/screener/universe_sp500.json`, refreshed weekly.
- Per-ticker fundamentals: piggybacks on `scripts/lib/data.py` daily cache.
- Full screener output: `research/daily/<date>/screener_<universe>_<top_n>.json`.

## Limits and honesty

- Uses yfinance `.info` dict for fundamentals — coverage is imperfect. Names with missing key fields go to `skipped.missing_fundamentals`, not silently given zero.
- S&P 500 constituent list: fetched from Wikipedia (widely-used source); cached weekly. If fetch fails, uses last cached copy and warns.
- **This is a starting-point filter**, not a factor model. The three agents do the actual evaluation.
