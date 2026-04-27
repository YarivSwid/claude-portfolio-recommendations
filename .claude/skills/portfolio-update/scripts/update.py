#!/usr/bin/env python3
"""portfolio-update — buy / sell / trim a position across all portfolio files.

Input (stdin JSON):
  {
    "action":    "sell" | "trim" | "buy",
    "symbol":    "META",          # ticker as stored in positions.json (symbol or yf_symbol)
    "quantity":  10,              # new total qty (trim) or shares to add (buy)
    "reduce_by": 5,               # alternative to quantity for trim
    "price":     650.0,           # execution price (optional, for log only)
    "currency":  "USD",           # for new buy only, default USD
    "date":      "2026-04-21",    # trade date, default today
    "note":      "free text"
  }
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import sys
from pathlib import Path

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

ROOT       = Path(__file__).resolve().parents[4]
POS_PATH   = ROOT / "portfolio" / "positions.json"
XLSX_PATH  = ROOT / "ActivePortfolio.csv.xlsx"
SNAP_DIR   = ROOT / "portfolio" / "snapshots"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_positions() -> tuple[dict | list, list[dict]]:
    data = json.loads(POS_PATH.read_text())
    lst  = data["positions"] if isinstance(data, dict) else data
    return data, lst


def _save_positions(data: dict | list, lst: list[dict]) -> None:
    if isinstance(data, dict):
        data["positions"]   = lst
        data["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    else:
        data = lst
    POS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _write_snapshot(lst: list[dict]) -> Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    snap  = SNAP_DIR / f"{today}.json"
    snap.write_text(json.dumps(lst, ensure_ascii=False, indent=2))
    return snap


def _match(p: dict, symbol: str) -> bool:
    s = symbol.upper()
    return (
        str(p.get("symbol", "")).upper()        == s or
        str(p.get("yf_symbol", "")).upper()     == s or
        str(p.get("security_number", "")).upper()== s or
        # strip .TA suffix for comparison
        str(p.get("yf_symbol", "")).upper().replace(".TA", "") == s.replace(".TA", "") or
        str(p.get("symbol", "")).upper().replace("IL", "") == s.replace("IL", "")
    )


def _xlsx_remove(symbol: str) -> bool:
    """Remove all rows matching symbol from xlsx. Returns True if any removed."""
    if not HAS_OPENPYXL or not XLSX_PATH.exists():
        return False
    wb = openpyxl.load_workbook(XLSX_PATH)
    ws = wb.active
    s  = symbol.upper().replace("IL", "").replace(".TA", "")
    to_delete = []
    for row in ws.iter_rows():
        # col D (index 3) = symbol, col C (index 2) = security_number
        sym_cell = str(row[3].value or "").upper().strip()
        sec_cell = str(row[2].value or "").upper().strip()
        if sym_cell == s or sec_cell == s or sec_cell == symbol.replace("IL", ""):
            to_delete.append(row[0].row)
    for r in sorted(to_delete, reverse=True):
        ws.delete_rows(r)
    if to_delete:
        wb.save(XLSX_PATH)
    return bool(to_delete)


def _xlsx_update_qty(symbol: str, new_qty: float) -> bool:
    """Update quantity (col F, index 5) for matching row."""
    if not HAS_OPENPYXL or not XLSX_PATH.exists():
        return False
    wb = openpyxl.load_workbook(XLSX_PATH)
    ws = wb.active
    s  = symbol.upper().replace("IL", "").replace(".TA", "")
    updated = False
    for row in ws.iter_rows():
        sym_cell = str(row[3].value or "").upper().strip()
        sec_cell = str(row[2].value or "").upper().strip()
        if sym_cell == s or sec_cell == s or sec_cell == symbol.replace("IL", ""):
            row[5].value = new_qty
            updated = True
    if updated:
        wb.save(XLSX_PATH)
    return updated


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def do_sell(symbol: str, params: dict) -> dict:
    data, lst = _load_positions()
    matches   = [p for p in lst if _match(p, symbol)]
    if not matches:
        return {"ok": False, "error": f"Symbol '{symbol}' not found in positions"}

    pos       = matches[0]
    prev_qty  = pos.get("quantity", 0)
    new_lst   = [p for p in lst if not _match(p, symbol)]

    _save_positions(data, new_lst)
    snap = _write_snapshot(new_lst)
    xlsx_ok = _xlsx_remove(symbol)

    files = ["portfolio/positions.json", str(snap.relative_to(ROOT))]
    if xlsx_ok:
        files.append("ActivePortfolio.csv.xlsx")

    return {
        "ok":           True,
        "action":       "sell",
        "symbol":       pos.get("symbol", symbol),
        "prev_quantity": prev_qty,
        "new_quantity": 0,
        "files_updated": files,
        "warnings":     [] if xlsx_ok else ["ActivePortfolio.csv.xlsx not updated (openpyxl missing or file absent)"],
    }


def do_trim(symbol: str, params: dict) -> dict:
    data, lst = _load_positions()
    idx       = next((i for i, p in enumerate(lst) if _match(p, symbol)), None)
    if idx is None:
        return {"ok": False, "error": f"Symbol '{symbol}' not found in positions"}

    pos      = lst[idx]
    prev_qty = float(pos.get("quantity") or 0)

    if "reduce_by" in params:
        new_qty = prev_qty - float(params["reduce_by"])
    elif "quantity" in params:
        new_qty = float(params["quantity"])
    else:
        return {"ok": False, "error": "trim requires 'quantity' (new total) or 'reduce_by'"}

    if new_qty < 0:
        return {"ok": False, "error": f"New quantity {new_qty} would be negative"}
    if new_qty == 0:
        return do_sell(symbol, params)

    lst[idx] = {**pos, "quantity": new_qty}
    _save_positions(data, lst)
    snap = _write_snapshot(lst)
    xlsx_ok = _xlsx_update_qty(symbol, new_qty)

    files = ["portfolio/positions.json", str(snap.relative_to(ROOT))]
    if xlsx_ok:
        files.append("ActivePortfolio.csv.xlsx")

    return {
        "ok":            True,
        "action":        "trim",
        "symbol":        pos.get("symbol", symbol),
        "prev_quantity": prev_qty,
        "new_quantity":  new_qty,
        "files_updated": files,
        "warnings":      [] if xlsx_ok else ["ActivePortfolio.csv.xlsx not updated"],
    }


def do_buy(symbol: str, params: dict) -> dict:
    data, lst = _load_positions()
    idx       = next((i for i, p in enumerate(lst) if _match(p, symbol)), None)

    add_qty  = float(params.get("quantity", 0))
    price    = params.get("price")
    currency = params.get("currency", "USD").upper()

    if idx is not None:
        pos      = lst[idx]
        prev_qty = float(pos.get("quantity") or 0)
        new_qty  = prev_qty + add_qty
        lst[idx] = {**pos, "quantity": new_qty}
        action_desc = "increased"
    else:
        # New position — minimal entry
        sym_upper = symbol.upper()
        new_pos = {
            "symbol":    sym_upper,
            "yf_symbol": sym_upper,
            "name":      sym_upper,
            "quantity":  add_qty,
            "currency":  currency,
            "asset_class": "stock",
            "region":    "IL" if sym_upper.startswith("IL") else "US",
            "last_price": price or 0,
            "cost_basis_adj_local": price or 0,
            "market_value_local": (price or 0) * add_qty,
        }
        lst.append(new_pos)
        prev_qty    = 0
        new_qty     = add_qty
        action_desc = "added"

    _save_positions(data, lst)
    snap = _write_snapshot(lst)

    warnings = []
    if action_desc == "added":
        warnings.append("New position added with minimal data. Run portfolio-parse after next broker export for full details.")

    return {
        "ok":            True,
        "action":        "buy",
        "symbol":        symbol.upper(),
        "prev_quantity": prev_qty,
        "new_quantity":  new_qty,
        "files_updated": ["portfolio/positions.json", str(snap.relative_to(ROOT))],
        "warnings":      warnings,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        raw    = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Invalid JSON input: {e}"}))
        sys.exit(1)

    action = params.get("action", "").lower().strip()
    symbol = (params.get("symbol") or "").strip()

    if not action:
        print(json.dumps({"ok": False, "error": "'action' is required: sell | trim | buy"}))
        sys.exit(1)
    if not symbol:
        print(json.dumps({"ok": False, "error": "'symbol' is required"}))
        sys.exit(1)

    if action == "sell":
        result = do_sell(symbol, params)
    elif action == "trim":
        result = do_trim(symbol, params)
    elif action == "buy":
        result = do_buy(symbol, params)
    else:
        result = {"ok": False, "error": f"Unknown action '{action}'. Use: sell | trim | buy"}

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
