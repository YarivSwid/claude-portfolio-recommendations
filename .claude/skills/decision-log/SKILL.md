---
name: decision-log
description: Maintains an append-only JSONL log of every BUY/SELL/HOLD/ADD/TRIM signal the workbench emits, with entry price, R/R, confidence, and "thesis breaks at" levels. Provides a monthly-review script that joins logged signals against current prices and computes per-signal P&L since entry. Use when the user asks "how have my signals performed?", "monthly review", "did our calls work?", or "show me the decision log". Auto-appends via the persist_signals.py Stop hook — no user action needed for logging.
---

# decision-log

## When to use

- Automatically: every Stop-hook run appends to `research/decisions.jsonl` (no user action needed).
- Manually for review: when the user asks "how did our calls do this month?" or wants
  to see aggregate signal performance.

## Why this exists

The workbench was already persisting per-ticker signals to `research/signals/<TICKER>_<date>.json` —
good for reversal detection, useless for aggregate review. The decision-log is the *flat*
projection of those signals into a single append-only JSONL, joined later against current
prices to answer "did we add value?" Foundation for the backtesting skill once
enough data accumulates.

## Output format

`research/decisions.jsonl` — one signal record per line:

```json
{"date": "2026-05-27", "ticker": "APP", "signal": "BUY", "confidence": "moderate",
 "entry_price": 533.87, "currency": "USD", "rr_base": 1.9, "rr_tail": 1.1,
 "thesis_breaks_at": "SEC Wells notice OR Q2 < $1.90B OR price < $400",
 "tier": "normal", "data_as_of": "2026-05-27",
 "source_signal_file": "research/signals/APP_2026-05-27.json"}
```

## Scripts

### `append.py`

Programmatic append — called by the `persist_signals.py` Stop hook after a signal is
persisted to `research/signals/`. Reads the per-ticker signal file, extracts the key
fields, and appends one line to `research/decisions.jsonl`.

```bash
# Called automatically from the Stop hook — do not run manually unless backfilling.
echo '{"signal_file": "research/signals/APP_2026-05-27.json"}' \
  | python3 .claude/skills/decision-log/scripts/append.py
```

### `review.py`

Read-side. Reads `research/decisions.jsonl`, joins each row against current
yfinance-cached prices via `scripts/lib/data.py`, computes per-signal P&L since entry,
and returns a structured monthly-review JSON.

```bash
# Last 30 days
echo '{"window_days": 30}' | python3 .claude/skills/decision-log/scripts/review.py

# All time
echo '{"window_days": null}' | python3 .claude/skills/decision-log/scripts/review.py

# By signal type
echo '{"window_days": 90, "filter_signal": "BUY"}' | python3 .claude/skills/decision-log/scripts/review.py
```

Output:
```json
{
  "data_as_of": "2026-05-27",
  "window_days": 30,
  "total_signals": 8,
  "by_signal": {
    "BUY": {"count": 5, "avg_return_pct": 4.2, "hit_rate": 0.6, "best": "APP +12.3%", "worst": "ANET -3.1%"},
    "HOLD": {"count": 2, "avg_return_pct": 1.1, "hit_rate": null},
    "SELL": {"count": 1, "avg_return_pct": -8.4, "hit_rate": null}
  },
  "vs_spy": {"signal_return": 4.2, "spy_return": 2.1, "alpha": 2.1},
  "rows": [ ...detailed per-signal P&L... ]
}
```

## Backfilling

If `research/decisions.jsonl` is empty or missing entries for past signals already in
`research/signals/`, run a one-time backfill:

```bash
for f in research/signals/*.json; do
  echo "{\"signal_file\": \"$f\"}" | python3 .claude/skills/decision-log/scripts/append.py
done
```

The append script is idempotent — re-running on the same signal file is a no-op
(detected via the `source_signal_file` field in existing JSONL rows).

## Quality rules

- **Append-only.** Never edit a row in-place. If a signal is later reversed, write a
  NEW row with the new signal — the old one stays as historical record.
- **Currency consistency.** Every row records the entry-price currency (USD for US
  tickers, ILS for TASE tickers with `.TA` suffix). The review script handles FX
  conversion for dual-currency P&L reporting.
- **Source-traceable.** Every row carries `source_signal_file` so the full bull/bear/manager
  context is reconstructable for any logged decision.
- **NOT a backtest.** Decision-log is *prospective* — records signals as they were emitted
  with the price at that moment. Backtest analysis is the *aggregate* analysis built on
  top of this data once 30+ entries exist.
