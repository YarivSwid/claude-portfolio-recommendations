#!/usr/bin/env python3
"""Look up the last-saved bull/bear/manager signal record for a ticker.

Usage (from the orchestrator or a direct question):
  python3 scripts/signal_lookup.py DELL
  python3 scripts/signal_lookup.py DELL --role bull
  python3 scripts/signal_lookup.py DELL --date 2026-04-20

Reads research/daily/<date>/signals/<TICKER>__<role>.json. Falls back to the
most recent date if --date is not given and today has no file.

Answering "what did the bull say about DELL" becomes a single read, not a
re-run of the bull-officer.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "research" / "daily"


def _find_file(ticker, role, target_date=None):
    safe = ticker.replace("/", "_")
    if target_date:
        p = BASE / target_date / "signals" / f"{safe}__{role}.json"
        return p if p.exists() else None

    if not BASE.exists():
        return None
    dates = sorted((d.name for d in BASE.iterdir() if d.is_dir()), reverse=True)
    for d in dates:
        p = BASE / d / "signals" / f"{safe}__{role}.json"
        if p.exists():
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker")
    ap.add_argument("--role", choices=["bull", "bear", "manager", "all"], default="all")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD — defaults to most recent available")
    args = ap.parse_args()

    roles = ["bull", "bear", "manager"] if args.role == "all" else [args.role]
    result = {"ticker": args.ticker, "records": {}}

    for r in roles:
        p = _find_file(args.ticker, r, args.date)
        if p is None:
            result["records"][r] = {"error": "no record found", "searched_date": args.date or "most-recent"}
        else:
            try:
                result["records"][r] = {
                    "file": str(p.relative_to(ROOT)),
                    "data_as_of": json.loads(p.read_text()).get("data_as_of"),
                    "content": json.loads(p.read_text()),
                }
            except Exception as e:
                result["records"][r] = {"error": f"parse failed: {e}", "file": str(p)}

    print(json.dumps(result, indent=2))
    return 0 if any("content" in v for v in result["records"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
