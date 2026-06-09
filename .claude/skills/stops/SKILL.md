---
name: stops
description: Computes per-holding stop-loss reference LEVELS (informational only — never emits REDUCE/SELL/EXIT/WATCH action tokens). For single stocks, returns BOTH a short-horizon Chandelier 3×ATR(14) trader_stop and a long-horizon SMA(200) investor_stop. Crypto ETFs (IBIT etc.) get a wider Chandelier 5×ATR(14). Equity ETFs and mutual funds get SMA(200). Status labels are descriptive (below / near / above), not actions. The daily-report rule forbids deriving signals from this output — fundamentals, momentum extension, ETF trend collapse with macro context, or explicit thesis breaks drive decisions, not stops. Use as situational awareness during daily-report generation or ad-hoc "where is the line" questions.
---

# Stops skill

Reads `portfolio/positions.json`, fetches recent OHLC from the yfinance cache (`scripts/lib/data.py`), and computes one or two reference levels per holding using a method that depends on asset type.

## Policy (CRITICAL — read this before consuming the output)

**Stops are informational reference data only. They DO NOT emit action tokens.**

The skill returns price levels, distances, and descriptive status labels (`below` / `near` / `above`). It does **not** return `REDUCE`, `SELL`, `EXIT`, `WATCH`, or any other action signal. The daily-report agent, the opportunity-scanner, and the bull/risk officer subagents are forbidden by CLAUDE.md from deriving negative signals from this output.

**Why:** the 22-day Chandelier on stocks fires on routine pullbacks in a strong trend (e.g. NVDA −0.5% through stop while +13% vs 200DMA), and the previous version of this skill emitted `REDUCE` on every such trigger. That produced mechanical "trim winners" recommendations in uptrending markets and forced the analyst to write paragraphs reconciling its own contradictory signals. The fix was to remove the action emission from the skill entirely and let the analyst decide — using fundamentals, momentum-check extension flags, ETF trend with macro context, and thesis-break levels — what to act on.

## Methods

| Asset type | Detection | Levels returned |
|---|---|---|
| Single stock | `asset_class == "stock"` | **Both**: `trader_stop` = Chandelier 3×ATR(14) anchored to `max(highest_close_22d, last_close)`, AND `investor_stop` = SMA(200) |
| Crypto ETF | symbol in `{"IBIT","FBTC","GBTC",...}` | `crypto_stop` = Chandelier 5×ATR(14) (BTC vol ≫ stocks) |
| Equity ETF / mutual fund | `asset_class in ("etf","mutual_fund")` and not crypto | `investor_stop` = SMA(200) (diversified-basket trend) |

**Why these:**
- Chandelier is anchored to the recent high, not entry — it trails up with winners and tracks unrealized gains. Defined by Chuck LeBeau ([StockCharts](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/chandelier-exit)).
- ATR(14) uses Wilder's true-range smoothing — standard definition ([Investopedia](https://www.investopedia.com/terms/a/atr.asp)).
- For stocks we emit BOTH the trader and investor levels so you can see them side-by-side: a Chandelier trigger in a strong uptrend is almost always noise, while a 200DMA break is a meaningful long-horizon event. Having both visible makes the difference obvious.
- Diversified ETFs whipsaw on ATR stops because internal positions hedge each other; trend-following on the 200DMA is the textbook diversified-basket rule.
- IBIT runs at ~3× the vol of SPY; a 3×ATR stop on it fires on routine moves. 5×ATR is the user-specified compromise.

## Status labels (descriptive, not actions)

- `below`  — current price ≤ stop
- `near`   — current price within 5% above stop
- `above`  — current price > 5% above stop

These are **descriptive**. The skill does not say "trigger" or "approach" — those words sound like actions. The agent reading this output does not derive `REDUCE` or `WATCH` from these labels.

## Usage

```bash
# Default — every holding from portfolio/positions.json
echo '{}' | python3 .claude/skills/stops/scripts/stops.py

# Specific tickers
echo '{"tickers": ["MU","NVDA","JPM"]}' | python3 .claude/skills/stops/scripts/stops.py
```

## Output shape

```json
{
  "data_as_of": "2026-05-28",
  "scope": "informational reference levels only — see 'note' below",
  "note": "Stops are informational reference levels only. Do NOT derive REDUCE/SELL/EXIT signals from stop status — the daily-report rule forbids it. Signals come from fundamentals, momentum extension into a real risk, ETF trend collapse with macro context, or an explicit thesis break — never from a stop trigger alone.",
  "method_summary": {
    "stock_trader": "Chandelier 3xATR(14) anchored to max(close_high_22d, last_close) — short-horizon",
    "stock_investor": "SMA(200) — 200DMA, long-horizon trend reference",
    "crypto_etf": "Chandelier 5xATR(14) (wider — BTC vol)",
    "etf": "SMA(200) — diversified-basket trend reference",
    "mutual_fund": "SMA(200)"
  },
  "results": [
    {
      "symbol": "NVDA",
      "yf_symbol": "NVDA",
      "asset_type": "stock",
      "current_price": 212.60,
      "currency": "USD",
      "trader_stop": 213.58,
      "trader_distance_pct": -0.0046,
      "trader_status": "below",
      "atr_14": 7.13,
      "highest_close_22d": 235.10,
      "investor_stop": 187.45,
      "investor_distance_pct": 0.1342,
      "investor_status": "above"
    },
    {
      "symbol": "IGV",
      "asset_type": "etf",
      "current_price": 98.94,
      "currency": "USD",
      "investor_stop": 105.30,
      "investor_distance_pct": -0.0604,
      "investor_status": "below"
    },
    {
      "symbol": "IBIT",
      "asset_type": "crypto_etf",
      "current_price": 42.45,
      "currency": "USD",
      "crypto_stop": 40.78,
      "crypto_distance_pct": 0.0410,
      "crypto_status": "near",
      "atr_14": 2.31
    }
  ],
  "summary": {
    "stocks_below_trader_stop": ["TEVA","NVDA","NET","VRT"],
    "stocks_near_trader_stop":  ["GOOG","BRK-B","MSFT","META"],
    "stocks_below_investor_stop": ["META","MSFT","BRK-B"],
    "etfs_below_200dma": ["IGV"],
    "crypto_below_5atr": [],
    "errors": ["IL5138409","IL5133731","HRL.F16"]
  }
}
```

## Daily-report integration

Render the `results[]` and `summary` in the **Trend & Stops Reference** block at the **bottom** of the report, clearly labelled informational. The per-holding analysis section at the top of the report must NOT cite this output as a reason for REDUCE/SELL/EXIT. The agent may reference stop levels descriptively ("NVDA is sitting just under its 3×ATR trader stop while +13% above its 200DMA — that's noise in a trend"), but the signal token (KEEP / ADD / REDUCE etc.) comes from fundamentals + momentum-check extension flags + thesis breaks, not from stops.

## What this is NOT

- **Not a trade execution signal.** Workbench is read-only.
- **Not an entry-price stop.** Chandelier is anchored to the *recent high*, so on a name purchased at entry $E that has rallied 50%, the stop is calibrated to the rally, not to $E.
- **Not a thesis-break level.** The qualitative "thesis breaks at $X" lines in `research/theses/` live elsewhere and supersede this when available.
- **Not an action token source.** The action field that previous versions emitted has been removed. If a future consumer needs an action signal, it must compose one from this output PLUS other inputs (fundamentals, regime, thesis, momentum-check), and CLAUDE.md forbids using stops as the *primary* cause.
