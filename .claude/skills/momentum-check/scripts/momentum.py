#!/usr/bin/env python3
"""momentum-check — deterministic momentum/overbought signals per ticker.

Reads cached yfinance daily closes via scripts.lib.data. No LLM, no WebSearch.
Returns streak_flag ("normal" | "extended") plus the underlying metrics, so
callers (opportunity-scanner, hooks) can gate BUY recommendations on names
that are already 18 days into a rally.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.lib import data as pdata  # noqa: E402

UP_DAY_STREAK_THRESHOLD = 7
RSI_OVERBOUGHT = 75.0
PCT_VS_200DMA_EXTENDED = 0.30
PCT_30D_RALLY_AT_ATH_THRESHOLD = 0.25
DIST_FROM_ATH_NEAR = -0.02

# Trend-crossover detection (Stan Weinstein stage analysis + classic
# golden/death cross). The cross must have happened in the last
# CROSSOVER_LOOKBACK trading days AFTER a prior run of at least
# CROSSOVER_PRIOR_RUN bars on the originating side — that filters out
# rapid whipsaws around the MA line and only flags real regime changes.
CROSSOVER_LOOKBACK = 5
CROSSOVER_PRIOR_RUN = 20

# Three-tier momentum labels (Option B — labelling only, no filter).
# Calibrated for an 8/10 risk-tolerance, 30-year-horizon user.
# Used by daily-scan + opportunity-scanner for INFORMATIONAL display only.
# Downstream consumers (portfolio-manager spec) apply position-size caps by tier;
# the momentum-check skill itself does NOT exclude or filter anything.
TIER_YELLOW_RSI = 70.0
TIER_RED_RSI = 80.0
TIER_YELLOW_PCT_200DMA = 0.20
TIER_RED_PCT_200DMA = 0.40
TIER_YELLOW_STREAK = 7
TIER_RED_STREAK = 11


def _classify_tier(rsi: float | None, pct_vs_200dma: float | None,
                   up_streak: int) -> str:
    """Pure momentum tier. No valuation gate (deliberately — Option B).

    Returns 'normal' | 'yellow' | 'red'. Any single criterion in the Red band
    promotes to Red; any single criterion in the Yellow band (and none Red)
    promotes to Yellow.
    """
    red_hit = (
        (rsi is not None and rsi >= TIER_RED_RSI)
        or (pct_vs_200dma is not None and pct_vs_200dma >= TIER_RED_PCT_200DMA)
        or up_streak >= TIER_RED_STREAK
    )
    if red_hit:
        return "red"
    yellow_hit = (
        (rsi is not None and rsi >= TIER_YELLOW_RSI)
        or (pct_vs_200dma is not None and pct_vs_200dma >= TIER_YELLOW_PCT_200DMA)
        or up_streak >= TIER_YELLOW_STREAK
    )
    return "yellow" if yellow_hit else "normal"


def _today() -> str:
    return dt.date.today().isoformat()


def _rsi_14(closes: pd.Series) -> float | None:
    if len(closes) < 15:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    last = rsi.iloc[-1]
    if pd.isna(last):
        return None
    return float(last)


def _consecutive_streak(closes: pd.Series, direction: str) -> int:
    if len(closes) < 2:
        return 0
    diffs = closes.diff().dropna()
    streak = 0
    for d in diffs.iloc[::-1]:
        if direction == "up" and d > 0:
            streak += 1
        elif direction == "down" and d < 0:
            streak += 1
        else:
            break
    return int(streak)


def _pct_change(closes: pd.Series, n: int) -> float | None:
    if len(closes) <= n:
        return None
    prev = closes.iloc[-n - 1]
    last = closes.iloc[-1]
    if prev <= 0 or pd.isna(prev) or pd.isna(last):
        return None
    return float(last / prev - 1)


def _dma(closes: pd.Series, n: int) -> float | None:
    if len(closes) < n:
        return None
    return float(closes.iloc[-n:].mean())


def _ma_series(closes: pd.Series, n: int) -> pd.Series:
    """Rolling SMA series — needed for crossover detection over time."""
    return closes.rolling(window=n, min_periods=n).mean()


def _consecutive_above(closes: pd.Series, ma: pd.Series) -> int:
    """How many of the most recent consecutive bars closed above ma?
    Returns 0 if the most recent bar is below."""
    diffs = (closes - ma).dropna()
    streak = 0
    for d in diffs.iloc[::-1]:
        if d > 0:
            streak += 1
        else:
            break
    return int(streak)


def _consecutive_below(closes: pd.Series, ma: pd.Series) -> int:
    diffs = (closes - ma).dropna()
    streak = 0
    for d in diffs.iloc[::-1]:
        if d < 0:
            streak += 1
        else:
            break
    return int(streak)


def _detect_price_ma_cross(closes: pd.Series, ma: pd.Series,
                           direction: str) -> bool:
    """direction in {'golden','death'}.
    Golden = close crossed from <= ma to > ma within the last
    CROSSOVER_LOOKBACK bars, AFTER a prior run of at least
    CROSSOVER_PRIOR_RUN bars on the below side.
    Death = symmetric (crossed from above to below after a prior run above).
    """
    diffs = (closes - ma).dropna()
    if len(diffs) < CROSSOVER_PRIOR_RUN + CROSSOVER_LOOKBACK + 1:
        return False
    recent = diffs.iloc[-CROSSOVER_LOOKBACK - 1:]
    for i in range(1, len(recent)):
        prev = recent.iloc[i - 1]
        curr = recent.iloc[i]
        crossed_up = prev <= 0 < curr
        crossed_down = prev >= 0 > curr
        if direction == "golden" and crossed_up:
            idx = len(diffs) - len(recent) + i
            prior = diffs.iloc[max(0, idx - CROSSOVER_PRIOR_RUN):idx]
            if len(prior) >= CROSSOVER_PRIOR_RUN and (prior < 0).all():
                return True
        elif direction == "death" and crossed_down:
            idx = len(diffs) - len(recent) + i
            prior = diffs.iloc[max(0, idx - CROSSOVER_PRIOR_RUN):idx]
            if len(prior) >= CROSSOVER_PRIOR_RUN and (prior > 0).all():
                return True
    return False


def _detect_ma_ma_cross(short_ma: pd.Series, long_ma: pd.Series,
                         direction: str) -> bool:
    """Classic 50DMA vs 200DMA golden/death cross within last
    CROSSOVER_LOOKBACK bars. No prior-run filter — MA-on-MA crosses
    are inherently slower than price-on-MA, so they don't whipsaw."""
    diffs = (short_ma - long_ma).dropna()
    if len(diffs) < CROSSOVER_LOOKBACK + 1:
        return False
    recent = diffs.iloc[-CROSSOVER_LOOKBACK - 1:]
    for i in range(1, len(recent)):
        prev = recent.iloc[i - 1]
        curr = recent.iloc[i]
        if direction == "golden" and prev <= 0 < curr:
            return True
        if direction == "death" and prev >= 0 > curr:
            return True
    return False


def _analyze_one(symbol: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    try:
        df = pdata.fetch_history(symbol, period="1y")
    except Exception as e:  # noqa: BLE001
        return {"error": f"fetch_failed: {e!s}"}, [f"{symbol}: fetch failed"]

    if df is None or df.empty or "Close" not in df.columns:
        return {"error": "no_data"}, [f"{symbol}: no data"]

    closes = df["Close"].dropna()
    if len(closes) < 20:
        return {"error": "insufficient_history", "observations": len(closes)}, [
            f"{symbol}: insufficient_history ({len(closes)} obs)"
        ]

    last_close = float(closes.iloc[-1])
    as_of = closes.index[-1].date().isoformat() if hasattr(closes.index[-1], "date") else None

    pct_5d = _pct_change(closes, 5)
    pct_10d = _pct_change(closes, 10)
    pct_30d = _pct_change(closes, 30)
    pct_90d = _pct_change(closes, 90)

    dma_50 = _dma(closes, 50)
    dma_150 = _dma(closes, 150)
    dma_200 = _dma(closes, 200)
    pct_vs_50dma = (last_close / dma_50 - 1) if dma_50 else None
    pct_vs_150dma = (last_close / dma_150 - 1) if dma_150 else None
    pct_vs_200dma = (last_close / dma_200 - 1) if dma_200 else None

    # Trend context — descriptive only, never auto-triggers BUY or SELL.
    # See CLAUDE.md: trend flags are context for the analyst, not actions.
    ma_50_series = _ma_series(closes, 50) if len(closes) >= 50 else None
    ma_150_series = _ma_series(closes, 150) if len(closes) >= 150 else None
    ma_200_series = _ma_series(closes, 200) if len(closes) >= 200 else None

    days_above_50dma = (
        _consecutive_above(closes, ma_50_series) if ma_50_series is not None else None
    )
    days_above_150dma = (
        _consecutive_above(closes, ma_150_series) if ma_150_series is not None else None
    )
    days_above_200dma = (
        _consecutive_above(closes, ma_200_series) if ma_200_series is not None else None
    )

    trend_flags: list[str] = []
    if ma_150_series is not None:
        if _detect_price_ma_cross(closes, ma_150_series, "golden"):
            trend_flags.append("golden_150_cross")
        if _detect_price_ma_cross(closes, ma_150_series, "death"):
            trend_flags.append("death_150_cross")
    if ma_200_series is not None:
        if _detect_price_ma_cross(closes, ma_200_series, "golden"):
            trend_flags.append("golden_200_cross")
        if _detect_price_ma_cross(closes, ma_200_series, "death"):
            trend_flags.append("death_200_cross")
    if ma_50_series is not None and ma_200_series is not None:
        if _detect_ma_ma_cross(ma_50_series, ma_200_series, "golden"):
            trend_flags.append("golden_cross_50_200")
        if _detect_ma_ma_cross(ma_50_series, ma_200_series, "death"):
            trend_flags.append("death_cross_50_200")

    high_52w = float(closes.iloc[-min(252, len(closes)):].max())
    dist_from_52w_high = (last_close / high_52w - 1) if high_52w > 0 else None

    rsi = _rsi_14(closes)
    up_streak = _consecutive_streak(closes, "up")
    down_streak = _consecutive_streak(closes, "down")

    flags: list[str] = []
    if up_streak >= UP_DAY_STREAK_THRESHOLD:
        flags.append(f"up_{up_streak}_days")
    if rsi is not None and rsi >= RSI_OVERBOUGHT:
        flags.append("rsi_overbought")
    if pct_vs_200dma is not None and pct_vs_200dma >= PCT_VS_200DMA_EXTENDED:
        flags.append("far_above_200dma")
    if (
        pct_30d is not None
        and pct_30d >= PCT_30D_RALLY_AT_ATH_THRESHOLD
        and dist_from_52w_high is not None
        and dist_from_52w_high >= DIST_FROM_ATH_NEAR
    ):
        flags.append("rallied_at_ath")

    streak_flag = "extended" if flags else "normal"
    tier = _classify_tier(rsi, pct_vs_200dma, up_streak)

    if dma_200 is None:
        warnings.append(f"{symbol}: 200DMA unavailable (only {len(closes)} obs)")

    result = {
        "last_close": round(last_close, 4),
        "as_of": as_of,
        "consecutive_up_days": up_streak,
        "consecutive_down_days": down_streak,
        "pct_change_5d": round(pct_5d, 4) if pct_5d is not None else None,
        "pct_change_10d": round(pct_10d, 4) if pct_10d is not None else None,
        "pct_change_30d": round(pct_30d, 4) if pct_30d is not None else None,
        "pct_change_90d": round(pct_90d, 4) if pct_90d is not None else None,
        "pct_vs_50dma": round(pct_vs_50dma, 4) if pct_vs_50dma is not None else None,
        "pct_vs_150dma": round(pct_vs_150dma, 4) if pct_vs_150dma is not None else None,
        "pct_vs_200dma": round(pct_vs_200dma, 4) if pct_vs_200dma is not None else None,
        "days_above_50dma": days_above_50dma,
        "days_above_150dma": days_above_150dma,
        "days_above_200dma": days_above_200dma,
        "trend_flags": trend_flags,
        "dist_from_52w_high_pct": round(dist_from_52w_high, 4)
        if dist_from_52w_high is not None
        else None,
        "rsi_14": round(rsi, 2) if rsi is not None else None,
        "streak_flag": streak_flag,
        "flags": flags,
        "tier": tier,
    }
    return result, warnings


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    payload = json.loads(raw)

    if "tickers" in payload:
        tickers = payload["tickers"]
    elif "ticker" in payload:
        tickers = [payload["ticker"]]
    else:
        positions_path = ROOT / "portfolio" / "positions.json"
        tickers = []
        if positions_path.exists():
            data = json.loads(positions_path.read_text())
            positions = data if isinstance(data, list) else (
                data.get("positions") or data.get("holdings") or []
            )
            tickers = [
                p.get("yf_symbol") or p.get("symbol")
                for p in positions
                if (p.get("yf_symbol") or p.get("symbol"))
            ]
        if not tickers:
            print(json.dumps({"error": "no tickers provided and positions.json had none"}))
            sys.exit(1)

    results: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for t in tickers:
        res, w = _analyze_one(t)
        results[t] = res
        warnings.extend(w)

    out = {
        "data_as_of": _today(),
        "results": results,
        "warnings": warnings,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
