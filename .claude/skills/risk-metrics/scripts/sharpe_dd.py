#!/usr/bin/env python3
"""Risk metrics — Sharpe, Sortino, max drawdown, vol, beta."""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.lib import data as pdata  # noqa: E402
from scripts.lib import fx as pfx  # noqa: E402
from scripts.lib import io as pio  # noqa: E402

TRADING_DAYS = 252
DEFAULT_RF = 0.045  # ~3M T-bill, adjust for ILS as needed


def _returns(df: pd.DataFrame) -> pd.Series:
    s = df["Close"].pct_change().dropna() if "Close" in df.columns else pd.Series(dtype=float)
    return s


def _portfolio_series(positions: list[dict], period: str, fx_rate: float | None) -> pd.DataFrame:
    """Build a synthetic portfolio price series by weighting current holdings
    through historical returns. Returns a DataFrame with a 'Close' column (NAV index)."""
    frames = []
    weights: dict[str, float] = {}
    total_ils = 0.0
    for p in positions:
        mv_ils = p.get("market_value_ils")
        if not isinstance(mv_ils, (int, float)):
            mv_local = p.get("market_value_local") or 0
            if p.get("currency") == "USD" and fx_rate:
                mv_ils = mv_local * fx_rate
            else:
                mv_ils = mv_local
        total_ils += mv_ils
        yf_sym = p.get("yf_symbol") or p.get("symbol")
        if not yf_sym:
            continue
        df = pdata.fetch_history(yf_sym, period=period)
        if df.empty or "Close" not in df.columns:
            continue
        frames.append((yf_sym, mv_ils, df["Close"]))

    if not frames or total_ils <= 0:
        return pd.DataFrame()

    # Align on common dates
    prices = pd.concat({sym: s for sym, _, s in frames}, axis=1, sort=True).ffill().dropna(how="all")
    if prices.empty:
        return pd.DataFrame()
    rets = prices.pct_change().fillna(0.0)

    w = pd.Series({sym: mv / total_ils for sym, mv, _ in frames})
    w = w.reindex(rets.columns).fillna(0)
    port_rets = rets.mul(w, axis=1).sum(axis=1)
    nav = (1 + port_rets).cumprod()
    return pd.DataFrame({"Close": nav})


def _metrics(rets: pd.Series, rf_annual: float) -> dict:
    if rets.empty or len(rets) < 30:
        return {"annualized_return": None, "annualized_vol": None,
                "sharpe": None, "sortino": None,
                "max_drawdown": None,
                "max_drawdown_start": None, "max_drawdown_end": None,
                "observations": int(len(rets))}

    mean_d = rets.mean()
    std_d = rets.std(ddof=1)
    ann_ret = (1 + mean_d) ** TRADING_DAYS - 1
    ann_vol = std_d * math.sqrt(TRADING_DAYS)

    excess_mean = mean_d - rf_annual / TRADING_DAYS
    sharpe = (excess_mean / std_d) * math.sqrt(TRADING_DAYS) if std_d else None

    downside = rets[rets < 0]
    dd_std = downside.std(ddof=1) if len(downside) > 1 else None
    sortino = (excess_mean / dd_std) * math.sqrt(TRADING_DAYS) if dd_std else None

    # Max drawdown
    nav = (1 + rets).cumprod()
    running_max = nav.cummax()
    drawdown = nav / running_max - 1
    mdd = float(drawdown.min())
    mdd_end = drawdown.idxmin()
    mdd_start = nav.loc[:mdd_end].idxmax() if pd.notna(mdd_end) else None

    return {
        "annualized_return": round(float(ann_ret), 4),
        "annualized_vol": round(float(ann_vol), 4),
        "sharpe": round(float(sharpe), 4) if sharpe is not None else None,
        "sortino": round(float(sortino), 4) if sortino is not None else None,
        "max_drawdown": round(mdd, 4),
        "max_drawdown_start": str(mdd_start.date()) if mdd_start is not None else None,
        "max_drawdown_end": str(mdd_end.date()) if mdd_end is not None else None,
        "observations": int(len(rets)),
    }


def _beta(rets: pd.Series, bench_symbol: str, period: str) -> float | None:
    bench = pdata.fetch_history(bench_symbol, period=period)
    if bench.empty:
        return None
    br = bench["Close"].pct_change().dropna()
    aligned = pd.concat([rets, br], axis=1, join="inner").dropna()
    if len(aligned) < 30:
        return None
    var = aligned.iloc[:, 1].var()
    if var == 0 or pd.isna(var):
        return None
    cov = aligned.iloc[:, 0].cov(aligned.iloc[:, 1])
    return round(float(cov / var), 4)


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    period = params.get("period", "2y")
    rf_annual = float(params.get("rf_annual", DEFAULT_RF))
    benchmarks = params.get("benchmarks", ["SPY", "TA35.TA"])
    ticker = params.get("ticker")

    warnings: list[str] = []

    if ticker:
        df = pdata.fetch_history(ticker, period=period)
        if df.empty:
            print(json.dumps({
                "data_as_of": dt.date.today().isoformat(),
                "scope": ticker, "period": period,
                "warnings": ["no yfinance data — check ticker symbol"],
            }, indent=2))
            return
        rets = _returns(df)
        m = _metrics(rets, rf_annual)
        betas = {f"beta_vs_{b}": _beta(rets, b, period) for b in benchmarks}
        out = {"data_as_of": dt.date.today().isoformat(),
               "scope": ticker, "period": period,
               **m, **betas, "warnings": warnings}
        print(json.dumps(out, indent=2))
        return

    # Portfolio mode
    doc = pio.load_positions()
    positions = doc.get("positions", [])
    meta = doc.get("meta", {}) or {}
    snapshot_fx_rate = meta.get("fx_rate_ils_per_usd")
    live_fx_rate, _ = pfx.usd_ils_rate(force_refresh=True)
    fx_rate = live_fx_rate if live_fx_rate is not None else snapshot_fx_rate

    if not positions:
        print(json.dumps({
            "data_as_of": dt.date.today().isoformat(),
            "scope": "portfolio", "period": period,
            "warnings": ["no positions — run portfolio-parse first"],
        }, indent=2))
        return

    port_df = _portfolio_series(positions, period, fx_rate)
    if port_df.empty:
        print(json.dumps({
            "data_as_of": dt.date.today().isoformat(),
            "scope": "portfolio", "period": period,
            "warnings": ["could not build portfolio series — yfinance missing for holdings"],
        }, indent=2))
        return

    rets = _returns(port_df)
    m = _metrics(rets, rf_annual)
    betas = {f"beta_vs_{b}": _beta(rets, b, period) for b in benchmarks}

    n_missing = sum(1 for p in positions if not (p.get("yf_symbol") or p.get("symbol")))
    if n_missing:
        warnings.append(f"{n_missing} positions missing symbols — excluded from series")

    out = {
        "data_as_of": dt.date.today().isoformat(),
        "scope": "portfolio (approximation: current weights applied historically)",
        "period": period, "rf_annual": rf_annual,
        **m, **betas,
        "warnings": warnings,
    }
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
