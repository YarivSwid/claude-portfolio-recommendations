---
name: currency-conversion
description: Returns today's USDILS rate (cached daily) and exposes convenient USD/ILS conversions. Used by every skill that displays monetary figures. Invoke before reporting dual-currency totals if the cached rate is older than 1 day.
---

# currency-conversion

## When to use

- Any time the assistant needs to present a figure in both ILS and USD.
- At the start of the daily scan to warm the FX cache for the rest of the day.

## Input

```bash
echo '{"amount": 1000, "from": "USD"}' | python3 .claude/skills/currency-conversion/scripts/fx.py
```

- `amount` (number, optional) — value to convert; omit for rate only.
- `from` (string, optional) — `"USD"` or `"ILS"`.

## Output

```json
{
  "rate_ils_per_usd": 3.65,
  "as_of": "2026-04-20",
  "converted": { "amount": 3650.0, "currency": "ILS" }
}
```

If yfinance is down, returns `rate_ils_per_usd: null` and a warning — does not fabricate a rate.
