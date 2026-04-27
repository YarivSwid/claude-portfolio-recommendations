#!/usr/bin/env python3
"""Parse the Hebrew broker exports into canonical JSON."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

# Make `scripts/lib/...` importable regardless of CWD.
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.lib import io as pio  # noqa: E402
from scripts.lib import fx as pfx  # noqa: E402

# Hebrew action labels → canonical English
ACTION_MAP = {
    "קניה": "buy",
    "קניה בבורסה": "buy",
    "קניה מחוץ לבורסה": "buy_otc",
    "קנ. מחוץ לבורסה-המ": "buy_otc",
    "מכירה": "sell",
    "מכירה בבורסה": "sell",
    "מכירה מחוץ לבורסה": "sell_otc",
    "מכ. מחוץ לבורסה-המ": "sell_otc",
    "דיבידנד": "dividend",
    "פקיעה מכ. מחוץ לבו": "expiry_sell",
    "פקיעה קנ. מחוץ לבו": "expiry_buy",
}

# Map Hebrew section headers to (asset_class, region)
SECTION_TAGS = {
    "ניע ישראלים": (None, "IL"),
    "ניע זרים": (None, "US"),
    "מניות": ("stock", None),
    "קרנות נאמנות": ("mutual_fund", None),
    "מחקי מדד": ("etf", None),
    "אג\"ח": ("bond", None),
    "אג''ח": ("bond", None),
}

EXCEL_EPOCH = dt.date(1899, 12, 30)


def excel_serial_to_date(n: float | int | None) -> str | None:
    if n in (None, "", 0, 0.0):
        return None
    try:
        return (EXCEL_EPOCH + dt.timedelta(days=int(n))).isoformat()
    except Exception:
        return None


def _is_section(name: str) -> str | None:
    if not name:
        return None
    n = name.strip()
    for key in SECTION_TAGS:
        if key in n:
            return key
    return None


def _is_total(name: str) -> bool:
    return bool(name) and ('סה"כ' in name or "סהכ" in name)


def parse_positions(path: Path) -> tuple[list[dict], list[str]]:
    import openpyxl
    warnings: list[str] = []
    if not path.exists():
        return [], [f"missing {path.name}"]
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    if ws is None:
        return [], ["portfolio sheet empty"]

    positions: list[dict] = []
    current_region: str | None = None
    current_asset_class: str | None = None

    for row in ws.iter_rows(values_only=True):
        if not row or not any(v not in (None, "", " ") for v in row):
            continue

        name = (row[1] or "").strip() if isinstance(row[1], str) else row[1]
        qty = row[5]
        has_qty = isinstance(qty, (int, float)) and qty != 0

        # Section headers are label-only rows with no numeric qty. Position
        # names can contain section words as substrings (e.g. the ETF
        # "מניות ארה\"ב ATF" contains "מניות"), so we only classify a row
        # as a section when qty is absent.
        if isinstance(name, str) and not has_qty:
            if _is_total(name):
                continue
            section = _is_section(name)
            if section:
                ac, reg = SECTION_TAGS[section]
                if ac is not None:
                    current_asset_class = ac
                if reg is not None:
                    current_region = reg
                continue

        if not has_qty:
            continue

        symbol = (row[3] or "").strip() if isinstance(row[3], str) else (row[3] or "")
        security_number = row[2]
        last_price = row[6]
        currency_raw = row[7]
        cost_adj = row[10]
        mv_local = row[17]
        mv_ils = row[16]
        pct_of_port = row[20]

        currency = pio.normalize_currency(currency_raw or "")
        if currency not in ("USD", "ILS"):
            currency = "ILS"  # TASE default

        # Fallback synthetic symbol if broker didn't give one (common for
        # Israeli mutual funds identified by security number only).
        if not symbol:
            symbol = f"IL{security_number}" if security_number else name

        positions.append({
            "symbol": str(symbol),
            "yf_symbol": pio.yf_symbol(str(symbol), currency),
            "name": name,
            "security_number": str(security_number) if security_number else None,
            "quantity": float(qty),
            "currency": currency,
            "region": current_region,
            "asset_class": current_asset_class,
            "last_price": float(last_price) if isinstance(last_price, (int, float)) else None,
            "cost_basis_adj_local": float(cost_adj) if isinstance(cost_adj, (int, float)) else None,
            "market_value_local": float(mv_local) if isinstance(mv_local, (int, float)) else None,
            "market_value_ils": float(mv_ils) if isinstance(mv_ils, (int, float)) else None,
            "broker_pct_of_portfolio": float(pct_of_port) if isinstance(pct_of_port, (int, float)) else None,
        })

    errors = pio.validate_positions(positions)
    warnings.extend(errors)
    return positions, warnings


def parse_transactions(path: Path) -> tuple[list[dict], list[str]]:
    import xlrd
    warnings: list[str] = []
    if not path.exists():
        return [], [f"missing {path.name}"]
    wb = xlrd.open_workbook(str(path))
    sheet = wb.sheets()[0]

    txns: list[dict] = []
    for i in range(sheet.nrows):
        row = sheet.row_values(i)
        if not row or len(row) < 17:
            continue
        # Header row has "שם נייר" in col 0.
        first = str(row[0] or "").strip()
        if not first or first in ("שם נייר",):
            continue
        if first.startswith("סניף:"):
            continue

        name = first
        security_number = row[1]
        symbol = str(row[2] or "").strip()
        value_date = excel_serial_to_date(row[3])
        action_he = str(row[4] or "").strip()
        action = ACTION_MAP.get(action_he, action_he or "unknown")
        qty = row[5] if isinstance(row[5], (int, float)) else None
        price = row[6] if isinstance(row[6], (int, float)) else None
        currency_raw = row[9]
        currency = pio.normalize_currency(currency_raw or "")
        gross = row[10] if isinstance(row[10], (int, float)) else None
        net = row[11] if isinstance(row[11], (int, float)) else None
        tax_il = row[12] if isinstance(row[12], (int, float)) else 0.0
        tax_foreign = row[13] if isinstance(row[13], (int, float)) else 0.0
        commission = row[14] if isinstance(row[14], (int, float)) else 0.0
        record_date = excel_serial_to_date(row[17] if len(row) > 17 else None)

        if qty is None and action == "dividend":
            qty = 0.0
        if qty is None:
            continue

        txns.append({
            "name": name,
            "security_number": str(security_number) if security_number else None,
            "symbol": symbol or None,
            "value_date": value_date,
            "record_date": record_date,
            "action_he": action_he,
            "action": action,
            "quantity": float(qty),
            "price": float(price) if price is not None else None,
            "currency": currency,
            "gross": float(gross) if gross is not None else None,
            "net": float(net) if net is not None else None,
            "tax_il": float(tax_il),
            "tax_foreign": float(tax_foreign),
            "commission": float(commission),
        })

    return txns, warnings


def diff_vs_last_snapshot(positions: list[dict]) -> dict:
    snapshots = sorted(pio.SNAPSHOTS_DIR.glob("*.json")) if pio.SNAPSHOTS_DIR.exists() else []
    # Exclude today's in-progress snapshot if it already exists.
    today = dt.date.today().isoformat()
    prior = [s for s in snapshots if s.stem != today]
    if not prior:
        return {"added": [], "removed": [], "qty_changed": [], "base": None}
    last = prior[-1]
    try:
        data = json.loads(last.read_text())
    except Exception:
        return {"added": [], "removed": [], "qty_changed": [], "base": None}
    rows = data if isinstance(data, list) else data.get("positions", [])
    old = {p["symbol"]: p["quantity"] for p in rows}
    new = {p["symbol"]: p["quantity"] for p in positions}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(s for s in set(new) & set(old) if abs(new[s] - old[s]) > 1e-9)
    return {"added": added, "removed": removed, "qty_changed": changed, "base": last.stem}


def compute_nav(positions: list[dict], fx_rate: float | None) -> tuple[float, float | None]:
    nav_ils = 0.0
    for p in positions:
        mv_ils = p.get("market_value_ils")
        if isinstance(mv_ils, (int, float)):
            nav_ils += mv_ils
            continue
        # Fallback: market_value_local × FX if local=USD
        mv_local = p.get("market_value_local")
        if isinstance(mv_local, (int, float)):
            if p["currency"] == "USD" and fx_rate:
                nav_ils += mv_local * fx_rate
            elif p["currency"] == "ILS":
                nav_ils += mv_local
    nav_usd = (nav_ils / fx_rate) if fx_rate else None
    return nav_ils, nav_usd


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    active_path = ROOT / params.get("active_xlsx", "ActivePortfolio.csv.xlsx")
    txn_path = ROOT / params.get("transactions_xls", "AllStockTransactions.csv.xls")

    positions, pos_warnings = parse_positions(active_path)
    txns, txn_warnings = parse_transactions(txn_path)

    fx_rate, fx_as_of = pfx.usd_ils_rate()
    nav_ils, nav_usd = compute_nav(positions, fx_rate)

    meta = {
        "active_source": active_path.name,
        "transactions_source": txn_path.name,
        "fx_rate_ils_per_usd": fx_rate,
        "fx_as_of": fx_as_of,
    }

    pio.write_positions(positions, meta)
    pio.write_transactions(txns, meta)
    pio.snapshot_positions(positions, meta)

    diff = diff_vs_last_snapshot(positions)

    warnings = pos_warnings + txn_warnings
    if fx_rate is None:
        warnings.append("FX rate unavailable — nav_usd omitted")

    # Use broker file mtime so data_as_of reflects when positions were actually exported,
    # not when this script ran. Stamps today() only if the file is fresh (same day).
    try:
        file_mtime = dt.date.fromtimestamp(active_path.stat().st_mtime)
    except Exception:
        file_mtime = dt.date.today()
    data_as_of = file_mtime.isoformat()
    if file_mtime < dt.date.today():
        warnings.append(
            f"Broker file '{active_path.name}' last modified {data_as_of} — "
            "positions may be stale. Re-export from your broker for fresh data."
        )

    result = {
        "data_as_of": data_as_of,
        "n_positions": len(positions),
        "n_transactions": len(txns),
        "nav_ils": round(nav_ils, 2),
        "nav_usd": round(nav_usd, 2) if nav_usd is not None else None,
        "fx_rate_ils_per_usd": fx_rate,
        "fx_as_of": fx_as_of,
        "diff_vs_last_snapshot": diff,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
