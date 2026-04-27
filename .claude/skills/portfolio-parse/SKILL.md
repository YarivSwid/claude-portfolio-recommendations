---
name: portfolio-parse
description: Parses the Hebrew broker exports (ActivePortfolio.csv.xlsx + AllStockTransactions.csv.xls) into canonical positions.json and transactions.json. Also writes a dated snapshot and diffs against the previous snapshot. Run whenever the user drops fresh broker exports in the project root.
---

# portfolio-parse

## When to use

- User drops new broker exports at the project root (overwriting the old files).
- The root broker files are newer (mtime) than `portfolio/positions.json`.
- The user asks "re-parse my portfolio" or "update positions."

## Input

```bash
echo '{}' | python3 .claude/skills/portfolio-parse/scripts/parse.py
```

Optional input JSON keys (all optional):

```json
{
  "active_xlsx": "ActivePortfolio.csv.xlsx",
  "transactions_xls": "AllStockTransactions.csv.xls"
}
```

## Output

Writes:
- `portfolio/positions.json` — canonical current holdings.
- `portfolio/transactions.json` — full trade history (buys, sells, dividends).
- `portfolio/snapshots/YYYY-MM-DD.json` — today's snapshot (point-in-time).

Returns JSON on stdout:

```json
{
  "data_as_of": "2026-04-20",
  "n_positions": 22,
  "n_transactions": 130,
  "nav_ils": 123456.78,
  "nav_usd": 33456.78,
  "fx_as_of": "2026-04-20",
  "diff_vs_last_snapshot": {
    "added": ["..."],
    "removed": ["..."],
    "qty_changed": ["..."]
  },
  "warnings": ["..."]
}
```

## Hebrew schema notes

The portfolio sheet uses these Hebrew column headers (left-to-right):
`נייר` (name), `מספר נייר` (security number), `סימבול` (symbol), `ISIN`, `כמות` (quantity), `שער אחרון` (last price), `מטבע` (currency), `שער עלות מותאם` (adjusted cost basis), `שווי אחזקה בשח` (NAV in ILS).

Section headers split the sheet: `ניע ישראלים` (Israeli securities) → `מניות` (stocks) / `קרנות נאמנות` (mutual funds) / `מחקי מדד` (index trackers) → `ניע זרים` (foreign) → same subdivisions. The parser uses the most recent section header to tag each row with `asset_class` and `region`.

Transaction actions include: `קניה`/`קניה בבורסה` (buy), `מכירה`/`מכירה בבורסה` (sell), `דיבידנד` (dividend), `קנ/מכ. מחוץ לבורסה` (OTC buy/sell), `פקיעה` (option expiry).

## Guardrails

- Never writes outside `portfolio/`.
- If the broker files are missing, returns a clear error — does not fabricate.
- Writes the snapshot even if diff is unchanged.
