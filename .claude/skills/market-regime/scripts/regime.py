#!/usr/bin/env python3
"""market-regime — composite market sentiment signal.

Pulls three regime inputs and returns a structured JSON:
  1. CNN Fear & Greed Index (value 0-100)
  2. VIX level via yfinance
  3. SPY vs 200-day moving average via yfinance

Outputs a composite label (extreme-fear / fear / neutral / greed / extreme-greed)
and a bull_lean_adjustment for the portfolio-manager subagent.

Caches daily under research/daily/<date>/market-regime.json to avoid repeat
fetches. Pass {"force_refresh": true} to bypass the cache.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

try:
    from scripts.lib import data as pdata  # noqa: E402
except Exception:
    pdata = None

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def fetch_fear_greed() -> dict:
    """Fetch CNN's Fear & Greed Index JSON endpoint. Falls back to AAII + put/call if CNN fails."""
    result = _fetch_cnn_fg()
    if result.get("value") is not None:
        return result
    # CNN failed — try fallback sentiment indicators
    fallback = _fetch_put_call_ratio()
    if fallback.get("value") is not None:
        return fallback
    # Both failed — return CNN error with fallback note
    result["fallback_attempted"] = "put_call_ratio"
    result["fallback_error"] = fallback.get("error", "unknown")
    return result


def _fetch_cnn_fg() -> dict:
    """Primary: CNN Fear & Greed Index."""
    try:
        req = urllib.request.Request(
            CNN_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (investing-workbench) Python-urllib",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
        doc = json.loads(raw)
        fg = doc.get("fear_and_greed", {}) or {}
        value = fg.get("score")
        if value is None:
            return {"value": None, "label": None, "error": "no score in response"}
        value = float(value)
        label = fg.get("rating") or _fg_label(value)
        return {
            "value": round(value, 1),
            "label": label,
            "source_url": CNN_URL,
        }
    except Exception as e:
        return {"value": None, "label": None, "error": str(e)[:120]}


def _fetch_put_call_ratio() -> dict:
    """Fallback sentiment: CBOE equity put/call ratio via VIX history shape.
    Uses VIX percentile as a proxy for fear/greed when CNN is unavailable.
    VIX > 80th percentile (1y) → fear-like; VIX < 20th percentile → greed-like.
    """
    if pdata is None:
        return {"value": None, "label": None, "error": "data.py unavailable for fallback"}
    try:
        hist = pdata.fetch_history("^VIX", period="1y")
        if hist.empty or "Close" not in hist.columns or len(hist) < 50:
            return {"value": None, "label": None, "error": "insufficient VIX history for percentile"}
        current = float(hist["Close"].iloc[-1])
        percentile = float((hist["Close"] < current).mean() * 100)
        # Map VIX percentile to a 0-100 fear/greed proxy (inverted: high VIX = low score = fear)
        fg_proxy = max(0, min(100, 100 - percentile))
        label = _fg_label(fg_proxy)
        return {
            "value": round(fg_proxy, 1),
            "label": label,
            "source": "VIX-percentile-proxy (fallback — CNN unavailable)",
            "vix_current": round(current, 2),
            "vix_percentile_1y": round(percentile, 1),
            "is_fallback": True,
        }
    except Exception as e:
        return {"value": None, "label": None, "error": f"fallback failed: {str(e)[:100]}"}


def _fg_label(v: float) -> str:
    if v < 25:
        return "Extreme Fear"
    if v < 45:
        return "Fear"
    if v < 55:
        return "Neutral"
    if v < 75:
        return "Greed"
    return "Extreme Greed"


def fetch_vix() -> dict:
    if pdata is None:
        return {"value": None, "label": None, "error": "data.py unavailable"}
    try:
        hist = pdata.fetch_history("^VIX", period="1y")
        if hist.empty or "Close" not in hist.columns:
            return {"value": None, "label": None, "error": "no VIX history"}
        v = float(hist["Close"].iloc[-1])
        if v < 15:
            label = "low"
        elif v < 20:
            label = "normal"
        elif v < 30:
            label = "elevated"
        else:
            label = "high"
        return {"value": round(v, 2), "label": label, "source": "yfinance ^VIX"}
    except Exception as e:
        return {"value": None, "label": None, "error": str(e)[:120]}


def fetch_spy_vs_200dma() -> dict:
    if pdata is None:
        return {"spy_price": None, "ma_200": None, "error": "data.py unavailable"}
    try:
        hist = pdata.fetch_history("SPY", period="2y")
        if hist.empty or len(hist) < 200:
            return {"spy_price": None, "ma_200": None, "error": "insufficient SPY history"}
        spy_price = float(hist["Close"].iloc[-1])
        ma_200 = float(hist["Close"].iloc[-200:].mean())
        pct = (spy_price - ma_200) / ma_200 * 100.0
        if pct > 5:
            label = "uptrend"
        elif pct > -5:
            label = "range"
        else:
            label = "downtrend"
        return {
            "spy_price": round(spy_price, 2),
            "ma_200": round(ma_200, 2),
            "pct_above": round(pct, 2),
            "label": label,
            "source": "yfinance SPY",
        }
    except Exception as e:
        return {"spy_price": None, "ma_200": None, "error": str(e)[:120]}


def composite_regime(fg: dict, vix: dict, spy: dict) -> tuple[str, float, list[str]]:
    """Return (composite_label, bull_lean_adjustment, warnings)."""
    warnings = []
    fg_v = fg.get("value")
    vix_v = vix.get("value")
    spy_pct = spy.get("pct_above")

    if fg_v is None:
        warnings.append("fear_greed unavailable — regime is VIX/SPY-only")
    if vix_v is None:
        warnings.append("VIX unavailable")
    if spy_pct is None:
        warnings.append("SPY 200DMA unavailable")

    # Extreme-fear triggers
    if (fg_v is not None and fg_v < 25) or (vix_v is not None and vix_v > 30) or (spy_pct is not None and spy_pct < -5):
        return ("extreme-fear", 0.30, warnings)
    # Extreme-greed triggers (require ALL three to agree — strict)
    if (
        fg_v is not None and fg_v > 75
        and vix_v is not None and vix_v < 15
        and spy_pct is not None and spy_pct > 15
    ):
        return ("extreme-greed", -0.25, warnings)
    # Fear
    if (fg_v is not None and fg_v < 45) or (vix_v is not None and 20 <= vix_v <= 30):
        return ("fear", 0.15, warnings)
    # Greed
    if (fg_v is not None and 55 <= fg_v <= 75) and (vix_v is not None and vix_v < 18):
        return ("greed", -0.10, warnings)
    # Default
    return ("neutral", 0.0, warnings)


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}
    force = bool(params.get("force_refresh", False))

    today = dt.date.today().isoformat()
    cache_dir = ROOT / "research" / "daily" / today
    cache_path = cache_dir / "market-regime.json"

    if cache_path.exists() and not force:
        try:
            with open(cache_path, "r") as f:
                print(f.read())
            return
        except Exception:
            pass  # fall through to re-fetch

    fg = fetch_fear_greed()
    vix = fetch_vix()
    spy = fetch_spy_vs_200dma()
    label, adj, warnings = composite_regime(fg, vix, spy)

    out = {
        "data_as_of": today,
        "fear_greed": fg,
        "vix": vix,
        "spy_vs_200dma": spy,
        "regime_composite": label,
        "bull_lean_adjustment": adj,
        "warnings": warnings,
    }

    # Write cache
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(out, f, indent=2)
    except Exception:
        pass

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
