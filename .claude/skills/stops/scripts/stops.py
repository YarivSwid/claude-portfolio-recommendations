#!/usr/bin/env python3
"""stops.py — per-holding stop-loss LEVELS (informational reference, not signals).

POLICY (enforced by CLAUDE.md "Daily report quality rules"):
This skill computes price levels and distances. It does NOT emit
REDUCE / SELL / EXIT / WATCH action tokens. The daily-report agent,
opportunity-scanner, and bull/risk officer subagents must NOT derive
negative signals from this output — stops are reference data only.

The 22-day Chandelier on stocks fires on routine pullbacks in a strong
trend (e.g. NVDA -0.5% through stop while up +13% vs 200DMA), and the
old skill emitted REDUCE on every such trigger. That produced
mechanical "trim winners" behavior in uptrends. We removed the action
emission entirely; the analyst now uses fundamentals + momentum
extension + thesis breaks to decide signals, and consults the
stops output as one of several context inputs.

What's computed (math unchanged from the prior versions):
  - Single stocks: BOTH levels, side by side:
      trader_stop   = max(highest_close_22d, last_close) - 3 * ATR(14)   [Chandelier]
      investor_stop = SMA(close, 200)                                    [200DMA]
  - Crypto ETFs (IBIT/FBTC/...): crypto_stop = anchor - 5 * ATR(14)
  - Equity ETFs / mutual funds: investor_stop = SMA(close, 200)

Status labels are descriptive only (no action prescribed):
  - "below"  : current price <= stop
  - "near"   : current price within 5% above stop
  - "above"  : current price > 5% above stop

Input (stdin JSON, all optional):
  {"tickers": ["MU","NVDA"]}    # subset; default = all holdings

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
from scripts.lib import data as pdata  # noqa: E402

POSITIONS_PATH = ROOT / "portfolio" / "positions.json"

# Crypto-ETF allowlist — these get the wider 5x ATR Chandelier instead of the
# equity-ETF 200DMA reference, because they track a single very-volatile asset.
CRYPTO_ETFS = {"IBIT", "FBTC", "GBTC", "BITB", "ARKB", "BTCO", "EZBC", "HODL", "BRRR"}

CHANDELIER_LOOKBACK = 22       # trading days — standard Chuck LeBeau parameterisation
ATR_PERIOD = 14
STOCK_ATR_MULT = 3.0
CRYPTO_ATR_MULT = 5.0
DMA_PERIOD = 200
NEAR_BAND_PCT = 0.05            # within 5% above stop = "near"

# Explicit disclaimer surfaced at the top of every output. Mirrored in
# CLAUDE.md "Daily report quality rules" so the agent reading this output
# knows it must not derive REDUCE/SELL/EXIT from any field below.
INFORMATIONAL_NOTE = (
    "Stops are informational reference levels only. Do NOT derive REDUCE/SELL/EXIT "
    "signals from stop status — the daily-report rule forbids it. Signals come from "
    "fundamentals, momentum extension into a real risk, ETF trend collapse with macro "
    "context, or an explicit thesis break — never from a stop trigger alone."
)


def _wilder_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float | None:
    """Welles Wilder ATR — Investopedia ATR definition.

    True range = max(H-L, |H - prev_close|, |L - prev_close|), then Wilder
    smoothing (equivalent to an EMA with alpha = 1/period).
    """
    if df is None or len(df) < period + 1:
        return None
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr = tr.dropna()
    if len(tr) < period:
        return None
    atr = tr.iloc[:period].mean()
    for x in tr.iloc[period:]:
        atr = (atr * (period - 1) + x) / period
    return float(atr)


def _chandelier_stop(df: pd.DataFrame, atr_mult: float) -> dict | None:
    """Chandelier exit anchored to max(highest_close_22d, last_close)."""
    if df is None or len(df) < CHANDELIER_LOOKBACK + 1:
        return None
    atr = _wilder_atr(df)
    if atr is None:
        return None
    recent = df.tail(CHANDELIER_LOOKBACK)
    last_close = float(df["Close"].iloc[-1])
    highest = float(recent["Close"].max())
    anchor = max(highest, last_close)  # don't anchor below today
    stop = anchor - atr_mult * atr
    return {
        "stop_price": round(stop, 2),
        "atr_14": round(atr, 4),
        "highest_close_22d": round(highest, 2),
        "anchor": round(anchor, 2),
        "atr_mult": atr_mult,
    }


def _sma_stop(df: pd.DataFrame, period: int = DMA_PERIOD) -> dict | None:
    """SMA(period) close. Used as the investor-horizon reference for stocks
    and the long-trend reference for ETFs / mutual funds."""
    if df is None or len(df) < period:
        return None
    sma = float(df["Close"].tail(period).mean())
    return {"stop_price": round(sma, 2), "sma_period": period}


def _classify_asset(pos: dict) -> str:
    """Return 'stock', 'crypto_etf', 'etf', or 'mutual_fund'."""
    sym = pos.get("symbol", "").upper()
    yf_sym = (pos.get("yf_symbol") or sym).upper()
    cls = (pos.get("asset_class") or "").lower()
    if sym in CRYPTO_ETFS or yf_sym in CRYPTO_ETFS:
        return "crypto_etf"
    if cls == "stock":
        return "stock"
    if cls == "mutual_fund":
        return "mutual_fund"
    return "etf"


def _status_label(current: float, stop: float) -> str:
    """Descriptive label only. Not an action. See INFORMATIONAL_NOTE."""
    if current <= stop:
        return "below"
    if (current - stop) / stop <= NEAR_BAND_PCT:
        return "near"
    return "above"


def _distance_pct(current: float, stop: float | None) -> float | None:
    if stop is None or stop <= 0:
        return None
    return round((current - stop) / stop, 4)


def _process_one(pos: dict) -> dict:
    sym = pos.get("symbol", "?")
    yf_sym = pos.get("yf_symbol") or sym
    asset_type = _classify_asset(pos)
    # Infer currency: .TA suffix = ILS (TASE), everything else USD.
    # Configurable via pos["currency"] if your broker export carries it.
    currency = pos.get("currency") or ("ILS" if yf_sym.endswith(".TA") else "USD")

    df = pdata.fetch_history(yf_sym, period="2y")
    if df is None or df.empty:
        return {
            "symbol": sym,
            "yf_symbol": yf_sym,
            "asset_type": asset_type,
            "currency": currency,
            "error": "no price history available",
        }

    last_close = float(df["Close"].iloc[-1])
    result: dict = {
        "symbol": sym,
        "yf_symbol": yf_sym,
        "asset_type": asset_type,
        "current_price": round(last_close, 2),
        "currency": currency,
    }

    if asset_type == "stock":
        chan = _chandelier_stop(df, STOCK_ATR_MULT)
        sma = _sma_stop(df, DMA_PERIOD)
        if chan:
            result["trader_stop"] = chan["stop_price"]
            result["trader_distance_pct"] = _distance_pct(last_close, chan["stop_price"])
            result["trader_status"] = _status_label(last_close, chan["stop_price"])
            result["atr_14"] = chan["atr_14"]
            result["highest_close_22d"] = chan["highest_close_22d"]
        if sma:
            result["investor_stop"] = sma["stop_price"]
            result["investor_distance_pct"] = _distance_pct(last_close, sma["stop_price"])
            result["investor_status"] = _status_label(last_close, sma["stop_price"])
        if not chan and not sma:
            result["error"] = "insufficient history to compute either stop"

    elif asset_type == "crypto_etf":
        chan = _chandelier_stop(df, CRYPTO_ATR_MULT)
        if chan:
            result["crypto_stop"] = chan["stop_price"]
            result["crypto_distance_pct"] = _distance_pct(last_close, chan["stop_price"])
            result["crypto_status"] = _status_label(last_close, chan["stop_price"])
            result["atr_14"] = chan["atr_14"]
        else:
            result["error"] = "insufficient history for crypto chandelier"

    else:  # etf / mutual_fund
        sma = _sma_stop(df, DMA_PERIOD)
        if sma:
            result["investor_stop"] = sma["stop_price"]
            result["investor_distance_pct"] = _distance_pct(last_close, sma["stop_price"])
            result["investor_status"] = _status_label(last_close, sma["stop_price"])
        else:
            result["error"] = "insufficient history for 200DMA"

    return result


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid JSON on stdin"}))
        sys.exit(1)

    requested = {t.upper() for t in (payload.get("tickers") or [])}

    if not POSITIONS_PATH.exists():
        print(json.dumps({"error": f"positions file not found: {POSITIONS_PATH}"}))
        sys.exit(1)
    data = json.loads(POSITIONS_PATH.read_text())
    positions = data if isinstance(data, list) else (
        data.get("positions") or data.get("holdings") or []
    )

    if requested:
        positions = [p for p in positions if p.get("symbol", "").upper() in requested]

    results = [_process_one(p) for p in positions]

    # Descriptive summary groupings — these are NOT action lists. The names use
    # neutral language ("below trader stop") rather than action-prescriptive
    # language ("triggered") to make the informational nature explicit.
    stocks_below_trader = [r["symbol"] for r in results
                           if r.get("asset_type") == "stock"
                           and r.get("trader_status") == "below"]
    stocks_near_trader = [r["symbol"] for r in results
                          if r.get("asset_type") == "stock"
                          and r.get("trader_status") == "near"]
    stocks_below_investor = [r["symbol"] for r in results
                             if r.get("asset_type") == "stock"
                             and r.get("investor_status") == "below"]
    etfs_below_200dma = [r["symbol"] for r in results
                         if r.get("asset_type") in ("etf", "mutual_fund")
                         and r.get("investor_status") == "below"]
    crypto_below_5atr = [r["symbol"] for r in results
                         if r.get("asset_type") == "crypto_etf"
                         and r.get("crypto_status") == "below"]
    errors = [r["symbol"] for r in results if "error" in r]

    out = {
        "data_as_of": dt.date.today().isoformat(),
        "scope": "informational reference levels only — see 'note' below",
        "note": INFORMATIONAL_NOTE,
        "method_summary": {
            "stock_trader": (
                f"Chandelier {STOCK_ATR_MULT:.0f}xATR({ATR_PERIOD}) anchored to "
                f"max(close_high_{CHANDELIER_LOOKBACK}d, last_close) — short-horizon"
            ),
            "stock_investor": f"SMA({DMA_PERIOD}) — 200DMA, long-horizon trend reference",
            "crypto_etf": f"Chandelier {CRYPTO_ATR_MULT:.0f}xATR({ATR_PERIOD}) (wider — BTC vol)",
            "etf": f"SMA({DMA_PERIOD}) — diversified-basket trend reference",
            "mutual_fund": f"SMA({DMA_PERIOD})",
        },
        "results": results,
        "summary": {
            "stocks_below_trader_stop": stocks_below_trader,
            "stocks_near_trader_stop": stocks_near_trader,
            "stocks_below_investor_stop": stocks_below_investor,
            "etfs_below_200dma": etfs_below_200dma,
            "crypto_below_5atr": crypto_below_5atr,
            "errors": errors,
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
