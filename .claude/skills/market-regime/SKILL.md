---
name: market-regime
description: Reads current market regime signals — CNN Fear & Greed Index, VIX level, SPY vs 200-day moving average. Outputs a regime label (Extreme Fear / Fear / Neutral / Greed / Extreme Greed) for use by the portfolio-manager subagent to regime-adjust its bullish lean. Caches daily to research/daily/<date>/market-regime.json.
---

# market-regime

## When to use

- Invoked automatically by the `portfolio-manager` subagent before any BUY/SELL/HOLD synthesis.
- Can also be called directly when the user asks about market sentiment, volatility regime, or "how extreme is the market right now."
- Caches daily: if today's file exists, returns it instead of re-fetching.

## Input

```bash
echo '{}' | python3 .claude/skills/market-regime/scripts/regime.py
```

Optional keys:
- `force_refresh` (bool) — re-fetch even if today's cache exists.

## Output

```json
{
  "data_as_of": "2026-04-20",
  "fear_greed": {
    "value": 52,
    "label": "Neutral",
    "source_url": "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
  },
  "vix": {
    "value": 14.8,
    "label": "low",
    "source": "yfinance ^VIX"
  },
  "spy_vs_200dma": {
    "spy_price": 585.2,
    "ma_200": 540.1,
    "pct_above": 8.4,
    "label": "uptrend",
    "source": "yfinance SPY"
  },
  "regime_composite": "neutral-greed",
  "bull_lean_adjustment": 0.0,
  "warnings": []
}
```

### Regime-composite label logic

Based on a weighted combination of the three signals:
- `extreme-fear` (contrarian BUY): F&G < 25, OR VIX > 30, OR SPY < 200DMA by >5%
- `fear`: F&G 25-45 OR VIX 20-30
- `neutral`: default
- `greed`: F&G 55-75 AND VIX < 18
- `extreme-greed` (contrarian WAIT): F&G > 75 AND VIX < 15 AND SPY > 200DMA by >15%

### `bull_lean_adjustment` values (used by portfolio-manager)

- `extreme-fear`: +0.30 (heavily lean bullish — contrarian buy)
- `fear`: +0.15
- `neutral`: 0.00 (default profile lean still applies)
- `greed`: -0.10
- `extreme-greed`: -0.25 (lean bearish, don't chase)

## Notes

- Primary Fear & Greed data: CNN's public JSON endpoint (`production.dataviz.cnn.io/index/fearandgreed/graphdata`).
- Falls back to a static warning if CNN endpoint fails — never fabricates a value.
- VIX and SPY come from yfinance via the shared `scripts/lib/data.py` cache.
- Daily cache in `research/daily/<date>/market-regime.json` — same-day re-runs return instantly.
