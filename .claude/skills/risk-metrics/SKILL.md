---
name: risk-metrics
description: Computes Sharpe, Sortino, max drawdown, annualized vol, and beta versus SPY and TA-35 on either the whole portfolio or a single ticker. Uses yfinance history. Use when the user asks about risk, volatility, drawdown, or benchmark-relative performance.
---

# risk-metrics

## When to use

- "What's my Sharpe?" / "How risky am I?" / "What's my drawdown?"
- Per-position risk profile before a sizing decision.
- Weekly review.

## Input

```bash
# portfolio-level (default)
echo '{}' | python3 .claude/skills/risk-metrics/scripts/sharpe_dd.py

# single ticker
echo '{"ticker":"NVDA"}' | python3 .claude/skills/risk-metrics/scripts/sharpe_dd.py

# custom window + risk-free rate
echo '{"period":"3y","rf_annual":0.045}' | python3 .claude/skills/risk-metrics/scripts/sharpe_dd.py
```

## Output

```json
{
  "data_as_of": "2026-04-20",
  "scope": "portfolio",
  "period": "2y",
  "annualized_return": 0.18,
  "annualized_vol": 0.22,
  "sharpe": 0.64,
  "sortino": 0.81,
  "max_drawdown": -0.19,
  "max_drawdown_start": "2025-02-10",
  "max_drawdown_end":   "2025-04-08",
  "beta_vs_SPY": 1.12,
  "beta_vs_TA35.TA": 0.35,
  "observations": 502,
  "warnings": []
}
```

## Notes

- Portfolio mode uses **current** holdings reweighted through the historical window — a good first-order estimate, not a true time-weighted return. Stated as an approximation.
- Single-ticker mode uses yfinance adjusted closes directly.
- Default `rf_annual` = 0.045 (US 3-month T-bill proxy). Override for ILS-denominated Sharpe.
- If yfinance fails to return ≥ 60 observations, returns `warnings: ["insufficient data"]` and null metrics.
