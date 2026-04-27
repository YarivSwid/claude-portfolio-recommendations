---
name: charts
description: Renders interactive HTML dashboards (browser) or PNG charts (inline). Use dashboard.py for any ticker or portfolio visual — it opens automatically in the browser. Use chart.py only for the legacy allocation-donut PNG.
---

# charts

## When to use

- User asks "show me" / "draw" / "visualize" / "chart" anything about a stock or the portfolio.
- A specific-decision flow wants a price+200DMA+drawdown visual for the ticker.
- A broad review wants portfolio-vs-SPY and sector allocation side by side.

## dashboard.py — interactive HTML (preferred)

```bash
# Single ticker (price + 200DMA + drawdown, opens in browser automatically)
echo '{"kind":"ticker","symbol":"NVDA"}' | python3 .claude/skills/charts/scripts/dashboard.py

# Full portfolio dashboard (portfolio vs SPY + sector donut)
echo '{"kind":"portfolio"}' | python3 .claude/skills/charts/scripts/dashboard.py

# Generate without auto-opening browser
echo '{"kind":"ticker","symbol":"MSFT","open_browser":false}' | python3 .claude/skills/charts/scripts/dashboard.py
```

Output:
```json
{
  "dashboard_path": "research/daily/2026-04-21/charts/NVDA-dashboard.html",
  "absolute_path": "/Users/.../NVDA-dashboard.html",
  "data_as_of": "2026-04-21",
  "warnings": []
}
```

The HTML opens automatically in the default browser. Charts are interactive — zoom, hover for exact values, toggle series on/off.

## chart.py — PNG (inline in Claude Code chat)

```bash
echo '{"kind":"allocation-donut"}' | python3 .claude/skills/charts/scripts/chart.py
```

Use the `Read` tool on `chart_path` to display the PNG inline in chat.

## What each dashboard shows

**kind=ticker:**
- Price history (2 years) + 200-day moving average + cost basis line (if held)
- Volume bars
- Drawdown from peak chart

**kind=portfolio:**
- Portfolio cumulative return vs SPY (1 year, current weights applied historically)
- Sector allocation donut

## Notes

- dashboard.py uses Plotly.js via CDN — requires internet the first time the browser loads the file.
- All data comes from yfinance cache and positions.json — never computed inline.
- Portfolio return is an approximation (current weights applied historically, not true TWR).
