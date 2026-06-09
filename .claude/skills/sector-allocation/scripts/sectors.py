#!/usr/bin/env python3
"""Portfolio allocation by region/currency/asset class/sector + HHI."""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.lib import data as pdata  # noqa: E402
from scripts.lib import fx as pfx  # noqa: E402
from scripts.lib import io as pio  # noqa: E402


def _weight_in_ils(p: dict, fx_rate: float | None) -> float:
    mv_ils = p.get("market_value_ils")
    if isinstance(mv_ils, (int, float)):
        return float(mv_ils)
    mv_local = p.get("market_value_local") or 0.0
    if p.get("currency") == "USD" and fx_rate:
        return float(mv_local) * float(fx_rate)
    return float(mv_local)


def _sector_for(p: dict) -> str:
    # Israeli mutual funds often don't resolve on yfinance; skip yf lookup for them.
    if p.get("asset_class") in ("mutual_fund", "etf", "bond"):
        return p.get("asset_class", "unknown").title()
    yf_sym = p.get("yf_symbol") or p.get("symbol")
    if not yf_sym:
        return "Unknown"
    info = pdata.fetch_info(yf_sym)
    s = info.get("sector") if info else None
    return s or "Unknown"


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    top_n = int(params.get("top_n_positions", 5))

    doc = pio.load_positions()
    positions = doc.get("positions", [])
    meta = doc.get("meta", {}) or {}
    snapshot_fx_rate = meta.get("fx_rate_ils_per_usd")
    snapshot_fx_as_of = meta.get("fx_as_of")

    live_fx_rate, live_fx_as_of = pfx.usd_ils_rate(force_refresh=True)
    fx_rate = live_fx_rate if live_fx_rate is not None else snapshot_fx_rate
    fx_as_of = live_fx_as_of if live_fx_rate is not None else snapshot_fx_as_of

    fx_drift_pct = None
    if live_fx_rate and snapshot_fx_rate and snapshot_fx_rate != 0:
        fx_drift_pct = round((live_fx_rate - snapshot_fx_rate) / snapshot_fx_rate * 100, 2)

    if not positions:
        print(json.dumps({
            "data_as_of": dt.date.today().isoformat(),
            "warnings": ["no positions found — run portfolio-parse first"],
        }, indent=2))
        return

    enriched = []
    for p in positions:
        w_ils = _weight_in_ils(p, fx_rate)
        enriched.append((p, w_ils))

    nav_ils = sum(w for _, w in enriched) or 1.0
    nav_usd = nav_ils / fx_rate if fx_rate else None

    by_region: dict[str, float] = defaultdict(float)
    by_currency: dict[str, float] = defaultdict(float)
    by_asset_class: dict[str, float] = defaultdict(float)
    by_sector: dict[str, float] = defaultdict(float)

    position_weights = []
    warnings: list[str] = []

    for p, w_ils in enriched:
        w = w_ils / nav_ils
        by_region[p.get("region") or "Unknown"] += w
        by_currency[p.get("currency") or "Unknown"] += w
        by_asset_class[p.get("asset_class") or "Unknown"] += w
        sector = _sector_for(p)
        by_sector[sector] += w
        if sector == "Unknown":
            warnings.append(f"unknown sector for {p.get('symbol')}")
        position_weights.append({
            "symbol": p.get("symbol"),
            "name": p.get("name"),
            "weight": round(w, 4),
            "region": p.get("region"),
            "sector": sector,
        })

    position_weights.sort(key=lambda x: x["weight"], reverse=True)
    top = position_weights[:top_n]
    for row in top:
        if row["weight"] > 0.10:
            row["flag"] = "over 10% cap"

    hhi = sum(w["weight"] ** 2 for w in position_weights)
    if hhi < 0.10:
        hhi_label = "diversified"
    elif hhi < 0.18:
        hhi_label = "moderate concentration"
    else:
        hhi_label = "concentrated"

    def _round_dict(d: dict) -> dict:
        return {k: round(v, 4) for k, v in sorted(d.items(), key=lambda kv: -kv[1])}

    if fx_drift_pct is not None and abs(fx_drift_pct) >= 1.0:
        warnings.append(
            f"FX drift {fx_drift_pct:+.2f}% — snapshot FX {snapshot_fx_rate:.4f} "
            f"({snapshot_fx_as_of}) vs live {live_fx_rate:.4f} ({live_fx_as_of}). "
            "NAV uses live FX; re-parse positions to refresh the snapshot."
        )

    result = {
        "data_as_of": dt.date.today().isoformat(),
        "fx_as_of": fx_as_of,
        "fx_rate_ils_per_usd": fx_rate,
        "snapshot_fx_as_of": snapshot_fx_as_of,
        "snapshot_fx_rate_ils_per_usd": snapshot_fx_rate,
        "fx_drift_pct_vs_snapshot": fx_drift_pct,
        "nav_ils": round(nav_ils, 2),
        "nav_usd": round(nav_usd, 2) if nav_usd is not None else None,
        "by_region": _round_dict(by_region),
        "by_currency": _round_dict(by_currency),
        "by_asset_class": _round_dict(by_asset_class),
        "by_sector": _round_dict(by_sector),
        "hhi": round(hhi, 4),
        "hhi_interpretation": hhi_label,
        "top_positions": top,
        "warnings": sorted(set(warnings)),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
