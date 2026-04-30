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
    dma_200 = _dma(closes, 200)
    pct_vs_50dma = (last_close / dma_50 - 1) if dma_50 else None
    pct_vs_200dma = (last_close / dma_200 - 1) if dma_200 else None

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
        "pct_vs_200dma": round(pct_vs_200dma, 4) if pct_vs_200dma is not None else None,
        "dist_from_52w_high_pct": round(dist_from_52w_high, 4)
        if dist_from_52w_high is not None
        else None,
        "rsi_14": round(rsi, 2) if rsi is not None else None,
        "streak_flag": streak_flag,
        "flags": flags,
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
        print(json.dumps({"error": "must provide 'ticker' or 'tickers'"}))
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
