#!/usr/bin/env python3
"""earnings-calendar — upcoming earnings dates for portfolio holdings.

For each held ticker, fetches the next earnings date from yfinance and flags
tickers reporting within a configurable window (default: 14 days).

Input (stdin JSON):
  {
    "window_days": 14,          # optional, default 14
    "force_refresh": false      # optional, bypass daily cache
  }

Output (stdout JSON):
  {
    "data_as_of": "YYYY-MM-DD",
    "window_days": 14,
    "upcoming": [
      {
        "symbol": "MSFT",
        "earnings_date": "2026-04-30",
        "days_until": 9,
        "warning": "earnings in 9 days — Sharpe/momentum signals may be unreliable"
      }
    ],
    "no_date_available": ["GOOG", "IL5131234"],
    "warnings": []
  }

Caches daily under research/daily/<date>/earnings-calendar.json.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

try:
    from scripts.lib import data as pdata
except Exception:
    pdata = None

POSITIONS_PATH = ROOT / "portfolio" / "positions.json"
CACHE_DIR = ROOT / "research" / "daily"


def _cache_path(today: dt.date) -> Path:
    d = CACHE_DIR / today.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d / "earnings-calendar.json"


def _load_positions() -> list[str]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(POSITIONS_PATH.read_text())
        positions = data if isinstance(data, list) else data.get("positions", [])
        symbols = []
        for p in positions:
            yf = p.get("yf_symbol") or p.get("symbol")
            if yf and not yf.startswith("IL"):
                symbols.append(yf)
        return list(dict.fromkeys(symbols))  # deduplicate, preserve order
    except Exception:
        return []


def _fetch_earnings_date(symbol: str) -> str | None:
    """Return next earnings date as ISO string, or None if unavailable."""
    if pdata is None:
        return None
    try:
        info = pdata.fetch_info(symbol)
        # yfinance stores next earnings as earningsTimestamp (unix) or earningsDate
        raw = info.get("earningsTimestamp") or info.get("earningsDate")
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            return dt.date.fromtimestamp(raw).isoformat()
        if isinstance(raw, str) and len(raw) >= 10:
            return raw[:10]
        # Sometimes it's a list of timestamps
        if isinstance(raw, list) and raw:
            first = raw[0]
            if isinstance(first, (int, float)):
                return dt.date.fromtimestamp(first).isoformat()
        return None
    except Exception:
        return None


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    window_days: int = int(params.get("window_days", 14))
    force_refresh: bool = bool(params.get("force_refresh", False))
    today = dt.date.today()

    cache_path = _cache_path(today)
    if cache_path.exists() and not force_refresh:
        try:
            cached = json.loads(cache_path.read_text())
            # Only use cache if window_days matches
            if cached.get("window_days") == window_days:
                print(json.dumps(cached, ensure_ascii=False, indent=2))
                return
        except Exception:
            pass

    symbols = _load_positions()
    if not symbols:
        result = {
            "data_as_of": today.isoformat(),
            "window_days": window_days,
            "upcoming": [],
            "no_date_available": [],
            "warnings": ["positions.json not found or empty — run portfolio-parse first"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    upcoming = []
    no_date = []
    warnings = []

    for symbol in symbols:
        earnings_date_str = _fetch_earnings_date(symbol)
        if earnings_date_str is None:
            no_date.append(symbol)
            continue
        try:
            earnings_date = dt.date.fromisoformat(earnings_date_str)
        except ValueError:
            no_date.append(symbol)
            continue

        days_until = (earnings_date - today).days
        if days_until < 0:
            # Past earnings — skip
            continue

        entry = {
            "symbol": symbol,
            "earnings_date": earnings_date_str,
            "days_until": days_until,
        }
        if days_until <= window_days:
            entry["warning"] = (
                f"earnings in {days_until} day{'s' if days_until != 1 else ''} — "
                "momentum and Sharpe signals may be unreliable; consider waiting for print"
            )
            upcoming.append(entry)

    upcoming.sort(key=lambda x: x["days_until"])

    result = {
        "data_as_of": today.isoformat(),
        "window_days": window_days,
        "upcoming": upcoming,
        "no_date_available": no_date,
        "warnings": warnings,
    }

    try:
        cache_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception:
        pass

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
