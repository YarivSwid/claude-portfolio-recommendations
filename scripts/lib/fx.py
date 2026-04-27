"""FX helpers. USD <-> ILS via yfinance USDILS=X, cached daily."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import data as _data

ROOT = Path(__file__).resolve().parents[2]
FX_CACHE = ROOT / ".cache" / "fx"
FX_CACHE.mkdir(parents=True, exist_ok=True)


def usd_ils_rate() -> tuple[float | None, str | None]:
    """Return (ILS per USD, date_iso). None on failure."""
    today = dt.date.today().isoformat()
    p = FX_CACHE / f"usdils_{today}.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            return float(d["rate"]), d["as_of"]
        except Exception:
            p.unlink(missing_ok=True)

    rate, as_of = _data.latest_close("USDILS=X")
    if rate is None:
        return None, None
    p.write_text(json.dumps({"rate": rate, "as_of": as_of}))
    return rate, as_of


def to_usd(amount_ils: float) -> float | None:
    rate, _ = usd_ils_rate()
    if rate is None or rate == 0:
        return None
    return amount_ils / rate


def to_ils(amount_usd: float) -> float | None:
    rate, _ = usd_ils_rate()
    if rate is None:
        return None
    return amount_usd * rate
