"""yfinance + maya.tase.co.il wrapper with a per-day on-disk cache.

One file per ticker per date. Skills import fetch_history() and fetch_info().
If yfinance fails or returns empty, we surface that explicitly — callers must
not paper over missing data.

Israeli securities (9-digit security numbers like IL5138409) are routed through
the TASE adapter — yfinance often has no coverage for Israeli mutual funds and
some IL-listed ETFs. The TASE path tries yfinance with the .TA suffix first
(sometimes works), then falls back to the maya.tase.co.il public endpoint.
"""
from __future__ import annotations

import datetime as dt
import json
import re
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

# Israeli security number patterns. Strip the .TA suffix if present.
_TASE_NUMBER_RE = re.compile(r"^(IL)?(\d{7,9})(\.TA)?$")


def _today() -> str:
    return dt.date.today().isoformat()


def _hist_path(symbol: str, period: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return CACHE / f"hist__{safe}__{period}__{_today()}.parquet"


def _info_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return CACHE / f"info__{safe}__{_today()}.json"


def _tase_security_number(symbol: str) -> str | None:
    """Return the bare 7-9 digit TASE security number if the symbol looks Israeli."""
    m = _TASE_NUMBER_RE.match(symbol)
    if not m:
        return None
    return m.group(2)


def fetch_tase_history(security_number: str, period: str = "2y") -> pd.DataFrame:
    """Fetch IL mutual fund / TASE-listed security history from maya.tase.co.il.

    TODO: implement the maya.tase.co.il HTTP fetch. The endpoint pattern is
        https://maya.tase.co.il/api/fund/<security_number>/historicalRates
    (response shape: list of {tradeDate, lastRate, ...}). Normalise to the
    same OHLCV schema as yfinance — at minimum a Close column indexed by
    DatetimeIndex — and write to .cache/yfinance/ using the same path
    pattern so downstream consumers do not branch.

    Until that lands, this function intentionally returns an empty DataFrame
    rather than raising — that preserves the existing "no price history
    available" behaviour for IL funds without breaking the broader pipeline.
    """
    return pd.DataFrame()


def fetch_history(symbol: str, period: str = "2y") -> pd.DataFrame:
    """Return daily OHLCV for symbol. Empty DataFrame on failure.

    Israeli securities (9-digit numbers, optionally with .TA suffix) are tried
    on yfinance first (sometimes coverage exists) and fall back to the TASE
    adapter if yfinance returns empty.
    """
    p = _hist_path(symbol, period)
    if p.exists():
        try:
            return pd.read_parquet(p)
        except Exception:
            p.unlink(missing_ok=True)

    df = pd.DataFrame()

    if yf is not None:
        # Silence yfinance's HTTP 404 stderr spam for Israeli securities
        # that aren't listed in Yahoo. Callers handle empty DFs.
        import contextlib, io as _io
        try:
            with contextlib.redirect_stderr(_io.StringIO()):
                df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
        except Exception:
            df = pd.DataFrame()
        if df is None:
            df = pd.DataFrame()

    if df.empty:
        tase_num = _tase_security_number(symbol)
        if tase_num is not None:
            df = fetch_tase_history(tase_num, period=period)

    if df.empty:
        return df

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
