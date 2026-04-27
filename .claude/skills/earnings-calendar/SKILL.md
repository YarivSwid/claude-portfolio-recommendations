---
name: earnings-calendar
description: Fetches next earnings dates for all portfolio holdings from yfinance and flags tickers reporting within a configurable window (default 14 days). Use when the user asks about upcoming earnings, or before any broad review or specific-decision flow to surface earnings-season clustering risk.
---

# earnings-calendar

## When to use

- Automatically as part of the broad review flow (step 1 data-gathering).
- Before a specific-decision flow — if the ticker reports within 14 days, momentum and Sharpe signals are unreliable.
- When the user asks "when does X report?" or "any earnings coming up?"

## Input

```bash
echo '{}' | python3 .claude/skills/earnings-calendar/scripts/earnings.py
```

Optional keys:
- `window_days` (int, default 14) — how far ahead to look for upcoming earnings.
- `force_refresh` (bool, default false) — bypass daily cache.

## Output

```json
{
  "data_as_of": "2026-04-20",
  "window_days": 14,
  "upcoming": [
    {
      "symbol": "MSFT",
      "earnings_date": "2026-04-30",
      "days_until": 9,
      "warning": "earnings in 9 days — momentum and Sharpe signals may be unreliable; consider waiting for print"
    }
  ],
  "no_date_available": ["IL5131234"],
  "warnings": []
}
```

## Notes

- Uses yfinance `.info["earningsTimestamp"]` or `.info["earningsDate"]` — not all tickers have this field. Israeli mutual funds (IL-numeric) are skipped automatically.
- Past earnings dates are filtered out; only future dates within the window are returned.
- Caches daily to `research/daily/<date>/earnings-calendar.json`. Same-day re-runs return the cached version unless `force_refresh` is set.
- When a ticker reports within the window, the orchestrator should surface the warning prominently — pre-earnings signals are noisy.
