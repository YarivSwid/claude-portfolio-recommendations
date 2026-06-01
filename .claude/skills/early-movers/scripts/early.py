#!/usr/bin/env python3
"""early.py — surface 150DMA-setup candidates, gated by SPY-vs-200DMA.

Two buckets per scan:
  breakout: today close > 150DMA, prior `breakout_lookback_days` (default 30) all below,
            and not extended (<= +10% above 150DMA).
  pullback: 200DMA rising over last 30d AND |close − 150DMA|/150DMA <= 2%.

Macro gate: SPY > 200DMA → BUY tokens. SPY <= 200DMA → all signals downgrade to WATCH
with macro_veto populated. Per user-spec: hard veto scoped to this skill only.

Input (stdin JSON, all optional):
  {"tickers": ["..."], "breakout_lookback_days": 30, "extended_pct": 0.10,
   "pullback_band_pct": 0.02, "limit": null}

Output (stdout JSON): see SKILL.md.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
# Reuse the screener's S&P 500 loader so we don't fork the universe-fetch logic.
sys.path.insert(0, str(ROOT / ".claude" / "skills" / "screener" / "scripts"))

from scripts.lib.data import fetch_history  # noqa: E402
import screen as scr  # noqa: E402

BREAKOUT_LOOKBACK_DEFAULT = 30  # days the stock must have been below 150DMA before
EXTENDED_PCT_DEFAULT = 0.10     # > +10% above 150DMA = chased, excluded
PULLBACK_BAND_DEFAULT = 0.02    # ±2% around 150DMA = "at the line"
MA_FAST = 150
MA_SLOW = 200
TREND_LOOKBACK = 30             # days to measure 200DMA slope
MIN_HISTORY = MA_SLOW + TREND_LOOKBACK  # need enough room for 200DMA + slope check


def _ma(closes: pd.Series, period: int) -> float | None:
    if len(closes) < period:
        return None
    return float(closes.tail(period).mean())


def _check_macro_gate() -> dict:
    """Return SPY context and gate state. Open iff SPY > its 200DMA."""
    df = fetch_history("SPY", period="2y")
    if df is None or df.empty or len(df) < MA_SLOW:
        return {"gate": "unknown", "error": "SPY history unavailable"}
    closes = df["Close"]
    spy_close = float(closes.iloc[-1])
    spy_200 = float(closes.tail(MA_SLOW).mean())
    pct = (spy_close - spy_200) / spy_200 if spy_200 > 0 else None
    return {
        "spy_close": round(spy_close, 2),
        "spy_200dma": round(spy_200, 2),
        "spy_pct_vs_200dma": round(pct, 4) if pct is not None else None,
        "gate": "open" if pct is not None and pct > 0 else "closed",
    }


def _classify_one(ticker: str, name: str, sector: str,
                  breakout_lookback: int, extended_pct: float,
                  pullback_band: float) -> dict | None:
    """Return a result dict if the ticker matches a bucket; else None."""
    df = fetch_history(ticker, period="2y")
    if df is None or df.empty or len(df) < MIN_HISTORY:
        return None
    closes = df["Close"]
    today = float(closes.iloc[-1])
    ma_150 = _ma(closes, MA_FAST)
    ma_200_today = _ma(closes, MA_SLOW)
    if ma_150 is None or ma_200_today is None:
        return None

    # 200DMA slope: compare today's 200DMA to 200DMA computed 30 days ago
    if len(closes) < MA_SLOW + TREND_LOOKBACK:
        ma_200_then = None
    else:
        prev_window = closes.iloc[-(MA_SLOW + TREND_LOOKBACK):-TREND_LOOKBACK]
        ma_200_then = float(prev_window.mean()) if len(prev_window) >= MA_SLOW else None
    slope_pos = ma_200_then is not None and ma_200_today > ma_200_then

    pct_vs_150 = (today - ma_150) / ma_150
    abs_dist_150 = abs(pct_vs_150)

    base = {
        "ticker": ticker, "name": name, "sector": sector,
        "close": round(today, 2),
        "ma_150": round(ma_150, 2),
        "ma_200": round(ma_200_today, 2),
        "pct_vs_150dma": round(pct_vs_150, 4),
        "ma_200_slope_positive": slope_pos,
    }

    # Pullback check FIRST (tighter, structural).
    # Require uptrend (200DMA rising) AND price hugging 150DMA AND above 200DMA
    # (a "pullback" below 200DMA is a downtrend, not a buy-the-dip).
    if slope_pos and abs_dist_150 <= pullback_band and today > ma_200_today:
        return {**base, "bucket": "pullback"}

    # Breakout check.
    # Need today's close above 150DMA, prior N days' closes ALL below 150DMA,
    # and not extended.
    if today > ma_150 and pct_vs_150 <= extended_pct:
        if len(closes) < breakout_lookback + 1:
            return None
        prior = closes.iloc[-(breakout_lookback + 1):-1]
        # All N prior closes were below their respective 150DMAs? Computing a
        # rolling 150DMA for that window is expensive; use today's 150DMA as a
        # close-enough proxy (the 150DMA moves slowly so the prior window's MA
        # is within ~1-2% of today's — fine for a discovery filter).
        all_below = (prior < ma_150).all()
        if all_below:
            # How many consecutive days were below before today?
            below = (closes.iloc[:-1] < ma_150)
            count = 0
            for v in reversed(below.tolist()):
                if v:
                    count += 1
                else:
                    break
            return {
                **base,
                "bucket": "breakout",
                "days_below_before_breakout": count,
            }
    return None


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid JSON on stdin"}))
        sys.exit(1)

    breakout_lookback = int(payload.get("breakout_lookback_days") or BREAKOUT_LOOKBACK_DEFAULT)
    extended_pct = float(payload.get("extended_pct") or EXTENDED_PCT_DEFAULT)
    pullback_band = float(payload.get("pullback_band_pct") or PULLBACK_BAND_DEFAULT)
    requested = {t.upper() for t in (payload.get("tickers") or [])}
    limit = payload.get("limit")

    macro = _check_macro_gate()
    gate_open = macro.get("gate") == "open"

    if requested:
        universe = [{"ticker": t, "name": "", "sector": ""} for t in requested]
        warnings: list[str] = []
    else:
        universe, warnings = scr._load_sp500()

    if limit:
        universe = universe[: int(limit)]

    breakouts: list[dict] = []
    pullbacks: list[dict] = []
    data_errors = 0

    for entry in universe:
        ticker = entry["ticker"]
        try:
            result = _classify_one(
                ticker, entry.get("name", ""), entry.get("sector", ""),
                breakout_lookback, extended_pct, pullback_band,
            )
        except Exception:
            data_errors += 1
            continue
        if result is None:
            continue

        if gate_open:
            result["signal"] = "BUY"
            result["macro_veto"] = None
        else:
            result["signal"] = "WATCH"
            result["macro_veto"] = (
                "SPY <=200DMA — broader trend off; revisit when SPY recovers above 200DMA"
            )

        if result["bucket"] == "breakout":
            breakouts.append(result)
        else:
            pullbacks.append(result)

    # Stable sort: closest setup to the line first within each bucket
    breakouts.sort(key=lambda r: r["pct_vs_150dma"])
    pullbacks.sort(key=lambda r: abs(r["pct_vs_150dma"]))

    out = {
        "data_as_of": dt.date.today().isoformat(),
        "macro": macro,
        "params": {
            "breakout_lookback_days": breakout_lookback,
            "extended_pct": extended_pct,
            "pullback_band_pct": pullback_band,
        },
        "breakouts": breakouts,
        "pullbacks": pullbacks,
        "stats": {
            "universe_scanned": len(universe),
            "data_errors": data_errors,
            "breakouts_found": len(breakouts),
            "pullbacks_found": len(pullbacks),
        },
        "warnings": warnings,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
