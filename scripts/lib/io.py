"""Schemas and IO helpers for the canonical portfolio JSON files."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PORTFOLIO_DIR = ROOT / "portfolio"
SNAPSHOTS_DIR = PORTFOLIO_DIR / "snapshots"

POSITIONS_SCHEMA_VERSION = 1
TRANSACTIONS_SCHEMA_VERSION = 1

# TASE tickers in yfinance end with .TA. Common Israeli symbols observed in
# the user's portfolio export. Missing entries fall back to plain symbol.
KNOWN_TASE_SYMBOLS = {
    "TEVA",  # Teva Pharmaceutical — dual-listed, but the Israeli leg uses TA
}


def yf_symbol(symbol: str, currency: str) -> str:
    """Best-effort yfinance symbol. Strip B/A share suffixes, add .TA for ILS."""
    if not symbol:
        return ""
    s = symbol.strip().upper().replace("/", "-")  # BRK/B -> BRK-B
    if currency in ("ILS", "₪", 'ש"ח'):
        return f"{s}.TA"
    return s


def normalize_currency(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if raw in ("$", "USD"):
        return "USD"
    if raw in ("₪", 'ש"ח', "ILS"):
        return "ILS"
    return raw.upper()


def validate_positions(positions: list[dict[str, Any]]) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors: list[str] = []
    required = {"symbol", "quantity", "currency", "last_price", "market_value_local"}
    for i, p in enumerate(positions):
        missing = required - set(p.keys())
        if missing:
            errors.append(f"position[{i}] missing {sorted(missing)}")
        if p.get("quantity", 0) < 0:
            errors.append(f"position[{i}] {p.get('symbol')} negative qty")
    return errors


def write_positions(positions: list[dict[str, Any]], meta: dict[str, Any]) -> Path:
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)
    path = PORTFOLIO_DIR / "positions.json"
    payload = {
        "schema_version": POSITIONS_SCHEMA_VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "positions": positions,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def write_transactions(txns: list[dict[str, Any]], meta: dict[str, Any]) -> Path:
    PORTFOLIO_DIR.mkdir(parents=True, exist_ok=True)
    path = PORTFOLIO_DIR / "transactions.json"
    payload = {
        "schema_version": TRANSACTIONS_SCHEMA_VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "transactions": txns,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def snapshot_positions(positions: list[dict[str, Any]], meta: dict[str, Any]) -> Path:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    path = SNAPSHOTS_DIR / f"{today}.json"
    payload = {
        "schema_version": POSITIONS_SCHEMA_VERSION,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "positions": positions,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def load_positions() -> dict[str, Any]:
    path = PORTFOLIO_DIR / "positions.json"
    if not path.exists():
        return {"positions": [], "meta": {}, "generated_at": None}
    return json.loads(path.read_text())


def load_transactions() -> dict[str, Any]:
    path = PORTFOLIO_DIR / "transactions.json"
    if not path.exists():
        return {"transactions": [], "meta": {}}
    return json.loads(path.read_text())
