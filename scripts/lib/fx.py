"""FX helpers. USD <-> ILS via yfinance USDILS=X, cached daily."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import data as _data

ROOT = Path(__file__).resolve().parents[2]
FX_CACHE = ROOT / ".cache" / "fx"
FX_CACHE.mkdir(parents=True, exist_ok=True)


def usd_ils_rate(force_refresh: bool = False) -> tuple[float | None, str | None]:
    """Return (ILS per USD, date_iso). None on failure.

    Cache is keyed by the yfinance `as_of` date (not the local clock date), so the
    filename always matches the rate inside. A separate `latest.json` pointer is
    rewritten on every successful refresh so callers can find the newest rate
    without scanning the directory.

    Call with force_refresh=True at render time when displaying NAV — do not
    trust an FX value embedded in a snapshot that was written days earlier.
    """
    latest_p = FX_CACHE / "latest.json"

    if not force_refresh and latest_p.exists():
        try:
            mtime_date = dt.date.fromtimestamp(latest_p.stat().st_mtime)
            if mtime_date == dt.date.today():
                d = json.loads(latest_p.read_text())
                return float(d["rate"]), d["as_of"]
        except Exception:
            latest_p.unlink(missing_ok=True)

    rate, as_of = _data.latest_close("USDILS=X")
    if rate is None:
        if latest_p.exists():
            try:
                d = json.loads(latest_p.read_text())
                return float(d["rate"]), d["as_of"]
            except Exception:
                pass
        return None, None

    payload = {"rate": rate, "as_of": as_of}
    (FX_CACHE / f"usdils_{as_of}.json").write_text(json.dumps(payload))
    latest_p.write_text(json.dumps(payload))
    return rate, as_of


def to_usd(amount_ils: float, force_refresh: bool = False) -> float | None:
    rate, _ = usd_ils_rate(force_refresh=force_refresh)
    if rate is None or rate == 0:
        return None
    return amount_ils / rate


def to_ils(amount_usd: float, force_refresh: bool = False) -> float | None:
    rate, _ = usd_ils_rate(force_refresh=force_refresh)
    if rate is None:
        return None
    return amount_usd * rate
