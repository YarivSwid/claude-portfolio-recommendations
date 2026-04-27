---
name: sector-allocation
description: Computes the current portfolio's weights by sector, region, currency, and asset class, plus an HHI concentration score. Reads portfolio/positions.json. Use when the user asks about exposure, diversification, or concentration risk.
---

# sector-allocation

## When to use

- "How much tech do I have?" / "What's my exposure to X?"
- Concentration checks before recommending adding to a position.
- The daily-scan briefing.

## Input

```bash
echo '{}' | python3 .claude/skills/sector-allocation/scripts/sectors.py
```

Optional:

```json
{ "top_n_positions": 5 }
```

## Output

```json
{
  "data_as_of": "2026-04-20",
  "fx_as_of": "2026-04-20",
  "nav_ils": 123456.78,
  "nav_usd": 33456.78,
  "by_region":   { "US": 0.62, "IL": 0.38 },
  "by_currency": { "USD": 0.62, "ILS": 0.38 },
  "by_asset_class": { "stock": 0.55, "etf": 0.30, "mutual_fund": 0.15 },
  "by_sector": { "Technology": 0.40, "Financials": 0.12, ... },
  "hhi": 0.185,
  "hhi_interpretation": "moderate concentration",
  "top_positions": [
    { "symbol": "NVDA", "weight": 0.11, "flag": "over 10% cap" },
    ...
  ],
  "warnings": []
}
```

## Notes

- HHI = sum of squared position weights. < 0.10 = diversified; 0.10–0.18 = moderate; > 0.18 = concentrated.
- Sector lookup uses `yfinance .info["sector"]`. If yfinance is missing for a ticker (common for Israeli mutual funds), the sector is `"Unknown"` and surfaces in warnings.
- `top_positions` flags any weight > 10% of NAV (the user's floor rule).
