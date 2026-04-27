"""Freshness + floor-rule helpers shared by skills.
These DO NOT enforce rules — they report so the assistant / hooks can.
"""
from __future__ import annotations

import datetime as dt

MICROCAP_USD = 300_000_000
MICROCAP_ILS = 500_000_000


def age_days(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    try:
        d = dt.date.fromisoformat(iso_date[:10])
    except Exception:
        return None
    return (dt.date.today() - d).days


def stale(iso_date: str | None, max_days: int = 1) -> bool:
    a = age_days(iso_date)
    if a is None:
        return True
    return a > max_days


def is_microcap(market_cap: float | None, currency: str) -> bool:
    if market_cap is None:
        return False
    if currency == "USD":
        return market_cap < MICROCAP_USD
    if currency == "ILS":
        return market_cap < MICROCAP_ILS
    return False


def is_crypto_ticker(symbol: str) -> bool:
    s = (symbol or "").upper()
    return s in {"IBIT", "GBTC", "ETHE", "FBTC", "ARKB", "BITO", "BTCO"} or "BTC" in s or "ETH" in s
