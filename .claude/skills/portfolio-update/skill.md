---
name: portfolio-update
description: Records a buy, sell, or trim trade and updates ALL portfolio files atomically — positions.json, snapshot, ActivePortfolio.csv.xlsx, and today's research snapshot. Use whenever the user says they bought, sold, or trimmed a position.
---

# portfolio-update

## When to use

- User says "I sold X", "I bought Y shares of Z", "I trimmed ABC to N shares"
- Any manual trade that is NOT yet reflected in a fresh broker export

## Input

```bash
echo '{
  "action": "sell",
  "symbol": "IL5137336",
  "quantity": 3250
}' | python3 .claude/skills/portfolio-update/scripts/update.py
```

### Action types

| action | meaning | required fields |
|--------|---------|----------------|
| `sell` | full exit — remove position entirely | `symbol` |
| `trim` | reduce shares | `symbol`, `quantity` (new total OR use `reduce_by`) |
| `buy`  | add new position or increase existing | `symbol`, `quantity`, `price` (optional), `currency` (optional, default USD) |

### Extra optional fields

```json
{
  "action": "trim",
  "symbol": "META",
  "reduce_by": 10,
  "price": 650.00,
  "date": "2026-04-21",
  "note": "trimming concentration"
}
```

- `quantity` — new total quantity after the trade (for trim)
- `reduce_by` — how many shares to remove (alternative to `quantity` for trim)
- `price` — execution price (stored in transaction log, does not update cost basis)
- `date` — trade date (defaults to today)
- `note` — free text, stored in transaction log

## Output

Updates these files atomically:
- `portfolio/positions.json`
- `portfolio/snapshots/YYYY-MM-DD.json` (today's snapshot)
- `ActivePortfolio.csv.xlsx` (removes or updates the row)

Returns JSON on stdout:

```json
{
  "ok": true,
  "action": "sell",
  "symbol": "IL5137336",
  "prev_quantity": 3250,
  "new_quantity": 0,
  "files_updated": ["portfolio/positions.json", "portfolio/snapshots/2026-04-21.json", "ActivePortfolio.csv.xlsx"],
  "warnings": []
}
```

## Guardrails

- Never fabricates prices or quantities — only records what the user provides
- For `sell`: removes the position from all files
- For `trim`: errors if `reduce_by` would make quantity negative
- For `buy` of a new symbol: adds a minimal position entry — user should run portfolio-parse after next broker export to get full data
- Always writes a dated snapshot after any change
