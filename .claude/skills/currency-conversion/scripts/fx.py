#!/usr/bin/env python3
"""USDILS rate + conversion helper (cached daily via scripts/lib/fx.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.lib import fx as pfx  # noqa: E402


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    rate, as_of = pfx.usd_ils_rate()
    out: dict = {"rate_ils_per_usd": rate, "as_of": as_of}

    amount = params.get("amount")
    src = (params.get("from") or "").upper()
    if isinstance(amount, (int, float)) and src in ("USD", "ILS") and rate is not None:
        if src == "USD":
            out["converted"] = {"amount": round(amount * rate, 2), "currency": "ILS"}
        else:
            out["converted"] = {"amount": round(amount / rate, 2), "currency": "USD"}

    if rate is None:
        out["warning"] = "USDILS=X unavailable from yfinance; no conversion performed"

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
