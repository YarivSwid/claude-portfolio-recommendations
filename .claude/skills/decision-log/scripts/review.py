#!/usr/bin/env python3
"""decision-log/review.py — read research/decisions.jsonl and compute per-signal P&L.

Joins each logged signal against current yfinance-cached price, computes return
since entry, aggregates by signal type, and benchmarks vs SPY.

Input (stdin JSON, all optional):
  {"window_days": 30, "filter_signal": "BUY"}

Output: structured monthly-review JSON.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

DECISIONS_PATH = ROOT / "research" / "decisions.jsonl"


def _read_log() -> list[dict]:
    if not DECISIONS_PATH.exists():
        return []
    rows = []
    for line in DECISIONS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _current_price(ticker: str) -> float | None:
    try:
        from scripts.lib import data as pdata
        info = pdata.fetch_info(ticker)
        return float(info.get("currentPrice") or info.get("regularMarketPrice") or 0) or None
    except Exception:
        return None


def _spy_return(start_date: str) -> float | None:
    """SPY return from `start_date` to today."""
    try:
        from scripts.lib import data as pdata
        hist = pdata.fetch_history("SPY", period="2y")
        if hist is None or hist.empty:
            return None
        start = dt.date.fromisoformat(start_date)
        # find the closest trading day
        idx = hist.index
        candidates = idx[idx.date >= start] if hasattr(idx, "date") else \
            [d for d in idx if d.date() >= start]
        if len(candidates) == 0:
            return None
        start_close = float(hist.loc[candidates[0], "Close"])
        end_close = float(hist["Close"].iloc[-1])
        return (end_close / start_close) - 1
    except Exception:
        return None


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}

    window_days = payload.get("window_days")
    filter_signal = (payload.get("filter_signal") or "").upper() or None

    rows = _read_log()
    today = dt.date.today()

    # Apply window filter
    if window_days:
        cutoff = today - dt.timedelta(days=int(window_days))
        rows = [r for r in rows if dt.date.fromisoformat(r.get("date", today.isoformat())) >= cutoff]
    if filter_signal:
        rows = [r for r in rows if (r.get("signal") or "").upper() == filter_signal]

    # Compute per-row return
    enriched = []
    for r in rows:
        ticker = r.get("ticker")
        entry = r.get("entry_price")
        if not ticker or not entry:
            continue
        cur = _current_price(ticker)
        if cur is None:
            r2 = dict(r); r2["current_price"] = None; r2["return_pct"] = None
            enriched.append(r2)
            continue
        ret = (cur / entry) - 1
        r2 = dict(r)
        r2["current_price"] = round(cur, 4)
        r2["return_pct"] = round(ret * 100, 2)
        # Days held
        try:
            entry_date = dt.date.fromisoformat(r.get("date"))
            r2["days_held"] = (today - entry_date).days
        except Exception:
            r2["days_held"] = None
        enriched.append(r2)

    # Aggregate by signal type
    by_signal: dict[str, dict] = {}
    for r in enriched:
        sig = r.get("signal") or "UNKNOWN"
        if r.get("return_pct") is None:
            continue
        by_signal.setdefault(sig, []).append(r)

    summary = {}
    for sig, items in by_signal.items():
        returns = [i["return_pct"] for i in items if i.get("return_pct") is not None]
        if not returns:
            continue
        positives = [r for r in returns if r > 0]
        best = max(items, key=lambda x: x.get("return_pct") or -999)
        worst = min(items, key=lambda x: x.get("return_pct") or 999)
        summary[sig] = {
            "count": len(items),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
            "median_return_pct": round(sorted(returns)[len(returns) // 2], 2),
            "hit_rate": round(len(positives) / len(returns), 2) if sig in ("BUY", "ADD", "STRONG_BUY") else None,
            "best": f"{best['ticker']} {best['return_pct']:+.1f}%",
            "worst": f"{worst['ticker']} {worst['return_pct']:+.1f}%",
        }

    # Benchmark vs SPY — use the earliest date in the window
    spy_alpha = None
    spy_ret = None
    if enriched:
        earliest = min(r.get("date") for r in enriched if r.get("date"))
        spy_ret = _spy_return(earliest)
        if spy_ret is not None and summary.get("BUY"):
            spy_alpha = round(summary["BUY"]["avg_return_pct"] - (spy_ret * 100), 2)

    out = {
        "data_as_of": today.isoformat(),
        "window_days": window_days,
        "filter_signal": filter_signal,
        "total_signals": len(enriched),
        "by_signal": summary,
        "vs_spy": {
            "buy_avg_return_pct": summary.get("BUY", {}).get("avg_return_pct"),
            "spy_return_pct": round(spy_ret * 100, 2) if spy_ret is not None else None,
            "alpha_pct": spy_alpha,
        },
        "rows": enriched,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
