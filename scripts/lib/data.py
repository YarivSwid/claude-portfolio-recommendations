"""yfinance wrapper with a per-day on-disk cache.

One file per ticker per date. Skills import fetch_history() and fetch_info().
If yfinance fails or returns empty, we surface that explicitly — callers must
not paper over missing data.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache" / "yfinance"
CACHE.mkdir(parents=True, exist_ok=True)


def _today() -> str:
    return dt.date.today().isoformat()


def _hist_path(symbol: str, period: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return CACHE / f"hist__{safe}__{period}__{_today()}.parquet"


def _info_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return CACHE / f"info__{safe}__{_today()}.json"


def fetch_history(symbol: str, period: str = "2y") -> pd.DataFrame:
    """Return daily OHLCV for symbol. Empty DataFrame on failure."""
    p = _hist_path(symbol, period)
    if p.exists():
        try:
            return pd.read_parquet(p)
        except Exception:
            p.unlink(missing_ok=True)

    if yf is None:
        return pd.DataFrame()

    # Silence yfinance's HTTP 404 stderr spam for the many Israeli mutual-fund
    # security numbers that aren't listed in Yahoo. Callers already handle empty DFs.
    import contextlib, io as _io, os
    try:
        with contextlib.redirect_stderr(_io.StringIO()):
            df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
    except Exception:
        return pd.DataFrame()

    if df is None or df.empty:
        return pd.DataFrame()

    try:
        df.to_parquet(p)
    except Exception:
        pass
    return df


def fetch_info(symbol: str) -> dict[str, Any]:
    """Return yfinance .info dict. {} on failure."""
    p = _info_path(symbol)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            p.unlink(missing_ok=True)

    if yf is None:
        return {}

    import contextlib, io as _io
    try:
        with contextlib.redirect_stderr(_io.StringIO()):
            info = yf.Ticker(symbol).info or {}
    except Exception:
        return {}

    try:
        p.write_text(json.dumps(_jsonable(info)))
    except Exception:
        pass
    return info


def latest_close(symbol: str) -> tuple[float | None, str | None]:
    """(price, date_iso) or (None, None)."""
    df = fetch_history(symbol, period="5d")
    if df.empty:
        return None, None
    row = df.iloc[-1]
    date = df.index[-1]
    date_iso = date.date().isoformat() if hasattr(date, "date") else str(date)
    return float(row["Close"]), date_iso


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    return str(obj)
