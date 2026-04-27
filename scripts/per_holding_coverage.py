#!/usr/bin/env python3
"""Report coverage of today's signal records against current holdings.

Answers: for each ticker in positions.json, do we have a bull+bear+manager
record in research/daily/<today>/signals/?

Usage:
  python3 scripts/per_holding_coverage.py
  python3 scripts/per_holding_coverage.py --date 2026-04-20
"""
import argparse
import json
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    pos_path = ROOT / "portfolio" / "positions.json"
    if not pos_path.exists():
        print(json.dumps({"error": "positions.json missing", "path": str(pos_path)}))
        return 1
    data = json.loads(pos_path.read_text())
    pos = data if isinstance(data, list) else data.get("positions", [])
    symbols = sorted({x.get("symbol") for x in pos if x.get("symbol")})

    signals_dir = ROOT / "research" / "daily" / args.date / "signals"
    coverage = {}
    missing_any = []
    for sym in symbols:
        safe = sym.replace("/", "_")
        entry = {}
        for role in ("bull", "bear", "manager"):
            f = signals_dir / f"{safe}__{role}.json"
            entry[role] = f.exists()
        coverage[sym] = entry
        if not all(entry.values()):
            missing_any.append(sym)

    out = {
        "date": args.date,
        "holdings_count": len(symbols),
        "fully_covered": sum(1 for v in coverage.values() if all(v.values())),
        "missing_any_role": missing_any,
        "coverage_by_ticker": coverage,
    }
    print(json.dumps(out, indent=2))
    return 0 if not missing_any else 2


if __name__ == "__main__":
    raise SystemExit(main())
