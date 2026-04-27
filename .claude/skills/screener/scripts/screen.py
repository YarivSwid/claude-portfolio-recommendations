#!/usr/bin/env python3
"""US stock screener.

Pulls a universe (default: S&P 500 via Wikipedia), filters by market cap + liquidity,
ranks by growth + momentum + quality, outputs JSON for the three-agent pipeline.

Uses the shared yfinance cache in scripts/lib/data.py so we don't hammer Yahoo.
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

from scripts.lib.data import fetch_history, fetch_info  # noqa: E402

CACHE_DIR = ROOT / ".cache" / "screener"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR = ROOT / "research" / "daily" / dt.date.today().isoformat()
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _load_sp500(force_refresh: bool = False) -> tuple[list[dict[str, str]], list[str]]:
    """Return (constituents, warnings). Each constituent: {ticker, name, sector}."""
    cache = CACHE_DIR / "universe_sp500.json"
    warnings: list[str] = []

    if not force_refresh and cache.exists():
        age_days = (dt.date.today() - dt.date.fromtimestamp(cache.stat().st_mtime)).days
        if age_days < 7:
            return json.loads(cache.read_text()), warnings

    try:
        # Wikipedia blocks requests without a UA. Fetch via urllib with a browser UA,
        # then hand HTML to pandas.read_html.
        import urllib.request
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 (investing-workbench; educational)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        import io as _io
        tables = pd.read_html(_io.StringIO(html))
        df = tables[0]
        # Normalize column names across Wikipedia edits
        cols = {c.lower(): c for c in df.columns}
        tic = cols.get("symbol") or cols.get("ticker symbol") or list(df.columns)[0]
        name = cols.get("security") or cols.get("company")
        sector = cols.get("gics sector") or cols.get("sector")
        rows = []
        for _, r in df.iterrows():
            t = str(r[tic]).replace(".", "-").strip()  # BRK.B -> BRK-B (yfinance form)
            rows.append({
                "ticker": t,
                "name": str(r[name]) if name else "",
                "sector": str(r[sector]) if sector else "",
            })
        cache.write_text(json.dumps(rows))
        return rows, warnings
    except Exception as e:
        warnings.append(f"S&P 500 fetch failed: {e}")
        if cache.exists():
            warnings.append("using stale cached universe")
            return json.loads(cache.read_text()), warnings
        # Last-resort static seed: the 30 largest US names by market cap.
        # Good enough for a smoke-test run; real screens need the full universe.
        warnings.append("using static 30-name fallback seed — run with force_refresh later")
        seed = [
            ("AAPL", "Apple Inc.", "Information Technology"),
            ("MSFT", "Microsoft Corp.", "Information Technology"),
            ("NVDA", "NVIDIA Corp.", "Information Technology"),
            ("GOOG", "Alphabet Inc. C", "Communication Services"),
            ("GOOGL", "Alphabet Inc. A", "Communication Services"),
            ("AMZN", "Amazon.com Inc.", "Consumer Discretionary"),
            ("META", "Meta Platforms Inc.", "Communication Services"),
            ("AVGO", "Broadcom Inc.", "Information Technology"),
            ("TSLA", "Tesla Inc.", "Consumer Discretionary"),
            ("BRK-B", "Berkshire Hathaway B", "Financials"),
            ("LLY", "Eli Lilly and Co.", "Health Care"),
            ("JPM", "JPMorgan Chase", "Financials"),
            ("V", "Visa Inc.", "Financials"),
            ("XOM", "ExxonMobil", "Energy"),
            ("UNH", "UnitedHealth Group", "Health Care"),
            ("MA", "Mastercard Inc.", "Financials"),
            ("COST", "Costco", "Consumer Staples"),
            ("HD", "Home Depot", "Consumer Discretionary"),
            ("PG", "Procter & Gamble", "Consumer Staples"),
            ("JNJ", "Johnson & Johnson", "Health Care"),
            ("WMT", "Walmart Inc.", "Consumer Staples"),
            ("NFLX", "Netflix Inc.", "Communication Services"),
            ("ORCL", "Oracle Corp.", "Information Technology"),
            ("AMD", "Advanced Micro Devices", "Information Technology"),
            ("CRM", "Salesforce Inc.", "Information Technology"),
            ("BAC", "Bank of America", "Financials"),
            ("ABBV", "AbbVie Inc.", "Health Care"),
            ("CVX", "Chevron Corp.", "Energy"),
            ("ADBE", "Adobe Inc.", "Information Technology"),
            ("KO", "Coca-Cola Co.", "Consumer Staples"),
        ]
        rows = [{"ticker": t, "name": n, "sector": s} for (t, n, s) in seed]
        return rows, warnings


def _pct_above_200dma(symbol: str) -> float | None:
    df = fetch_history(symbol, period="1y")
    if df.empty or len(df) < 200:
        return None
    close = float(df["Close"].iloc[-1])
    ma = float(df["Close"].tail(200).mean())
    if ma <= 0:
        return None
    return (close - ma) / ma


def _adv_usd(symbol: str) -> float | None:
    df = fetch_history(symbol, period="3mo")
    if df.empty:
        return None
    # Average daily dollar volume over the window
    try:
        return float((df["Close"] * df["Volume"]).tail(60).mean())
    except Exception:
        return None


def _score(fund: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """Weighted composite in [0, 1]."""
    # Growth (0.40)
    g = fund.get("rev_growth_yoy")
    if g is None:
        growth = 0.40 * 0.3
    else:
        growth = 0.40 * min(max(g, 0.0), 0.50) / 0.50

    # Momentum (0.30) — symmetric around 0, capped at ±30%
    m = fund.get("pct_above_200dma")
    if m is None:
        momentum = 0.30 * 0.3
    else:
        # Map [-0.30, +0.30] to [0, 1]
        mapped = (max(min(m, 0.30), -0.30) + 0.30) / 0.60
        momentum = 0.30 * mapped

    # Quality (0.30): gross margin + positive earnings
    gm = fund.get("gross_margin")
    pe = fund.get("fwd_pe")
    q_gm = 0.0 if gm is None else min(gm, 0.70) / 0.70
    q_pe = 0.0 if pe is None or pe <= 0 else 1.0  # positive fwd EPS implied
    quality = 0.30 * (0.6 * q_gm + 0.4 * q_pe) if (gm is not None or pe is not None) else 0.30 * 0.3

    total = growth + momentum + quality
    return round(total, 3), {
        "growth": round(growth, 3),
        "momentum": round(momentum, 3),
        "quality": round(quality, 3),
    }


def _priority_flags(fund: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    mc = fund.get("market_cap_usd")
    pe = fund.get("fwd_pe")
    g = fund.get("rev_growth_yoy")
    adv = fund.get("adv_usd")
    m = fund.get("pct_above_200dma")
    if mc is not None and mc < 300e6:
        flags.append("below_micro_cap_floor")
    if pe is not None and pe > 60 and (g is None or g < 0.30):
        flags.append("valuation_stretch")
    if adv is not None and adv < 20e6:
        flags.append("low_liquidity")
    if m is not None and m < -0.10:
        flags.append("negative_momentum")
    return flags


def main() -> None:
    try:
        payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except Exception:
        payload = {}

    universe = payload.get("universe", "sp500")
    top_n = int(payload.get("top_n", 20))
    sector_filter = payload.get("sector")
    min_mc = float(payload.get("min_market_cap", 10e9))
    min_adv = float(payload.get("min_adv_usd", 50e6))
    force_refresh = bool(payload.get("force_refresh", False))

    warnings: list[str] = []
    if universe != "sp500":
        warnings.append(f"universe '{universe}' not yet supported — defaulting to sp500")

    constituents, w = _load_sp500(force_refresh=force_refresh)
    warnings.extend(w)

    if not constituents:
        print(json.dumps({
            "data_as_of": dt.date.today().isoformat(),
            "universe": "sp500",
            "candidates": [],
            "warnings": warnings + ["no universe available"],
        }, indent=2))
        return

    include_etfs = bool(payload.get("include_etfs", True))
    if include_etfs and not sector_filter:
        etf_seed = [
            ("SPY", "SPDR S&P 500 ETF", "ETF - Broad US"),
            ("QQQ", "Invesco QQQ Trust", "ETF - US Large-cap Tech"),
            ("VTI", "Vanguard Total Stock Market", "ETF - Broad US"),
            ("IWM", "iShares Russell 2000", "ETF - US Small-cap"),
            ("SMH", "VanEck Semiconductor ETF", "ETF - Semiconductors"),
            ("SOXX", "iShares Semiconductor ETF", "ETF - Semiconductors"),
            ("XLV", "Health Care Select Sector SPDR", "ETF - Health Care"),
            ("XLF", "Financial Select Sector SPDR", "ETF - Financials"),
            ("XLE", "Energy Select Sector SPDR", "ETF - Energy"),
            ("XLY", "Consumer Discretionary SPDR", "ETF - Consumer Disc"),
            ("XLP", "Consumer Staples SPDR", "ETF - Consumer Staples"),
            ("XLU", "Utilities Select Sector SPDR", "ETF - Utilities"),
            ("XLRE", "Real Estate Select Sector SPDR", "ETF - Real Estate"),
            ("XLI", "Industrials Select Sector SPDR", "ETF - Industrials"),
            ("XLB", "Materials Select Sector SPDR", "ETF - Materials"),
            ("XLC", "Communication Services SPDR", "ETF - Communication"),
            ("IGV", "iShares Expanded Tech Software", "ETF - Software"),
            ("EFA", "iShares MSCI EAFE", "ETF - International Dev"),
            ("VEA", "Vanguard FTSE Developed", "ETF - International Dev"),
            ("EEM", "iShares MSCI Emerging Markets", "ETF - Emerging Markets"),
            ("TLT", "iShares 20+ Year Treasury", "ETF - Long Treasuries"),
            ("IEF", "iShares 7-10 Year Treasury", "ETF - Mid Treasuries"),
            ("GLD", "SPDR Gold Shares", "ETF - Gold"),
            ("MAGS", "Roundhill Magnificent Seven", "ETF - Mega-cap Tech"),
        ]
        existing = {c["ticker"] for c in constituents}
        for t, n, s in etf_seed:
            if t not in existing:
                constituents.append({"ticker": t, "name": n, "sector": s})

    if sector_filter:
        constituents = [c for c in constituents if c.get("sector") == sector_filter]

    skipped_missing = []
    skipped_mc = 0
    skipped_adv = 0
    scored: list[dict[str, Any]] = []

    for c in constituents:
        ticker = c["ticker"]
        is_etf = "ETF" in (c.get("sector") or "")
        info = fetch_info(ticker)
        if not info and not is_etf:
            skipped_missing.append(ticker)
            continue

        mc = info.get("marketCap") if info else None
        if not is_etf:
            if mc is None or mc < min_mc:
                skipped_mc += 1
                continue
        else:
            mc = info.get("totalAssets") if info else None

        adv = _adv_usd(ticker)
        adv_threshold = 20e6 if is_etf else min_adv
        if adv is None or adv < adv_threshold:
            skipped_adv += 1
            continue

        fund = {
            "ticker": ticker,
            "name": c.get("name") or (info.get("shortName") if info else ""),
            "sector": c.get("sector") or (info.get("sector") if info else "") or "",
            "market_cap_usd": mc,
            "fwd_pe": info.get("forwardPE") if info else None,
            "rev_growth_yoy": info.get("revenueGrowth") if info else None,
            "gross_margin": info.get("grossMargins") if info else None,
            "pct_above_200dma": _pct_above_200dma(ticker),
            "adv_usd": adv,
            "is_etf": is_etf,
        }
        score, breakdown = _score(fund)
        fund["score"] = score
        fund["score_breakdown"] = breakdown
        fund["priority_flags"] = _priority_flags(fund)
        scored.append(fund)

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_n]

    out = {
        "data_as_of": dt.date.today().isoformat(),
        "universe": "sp500",
        "filters_applied": {
            "min_market_cap": min_mc,
            "min_adv_usd": min_adv,
            "sector": sector_filter,
        },
        "candidates": top,
        "skipped": {
            "missing_fundamentals": skipped_missing[:20],
            "missing_fundamentals_count": len(skipped_missing),
            "failed_market_cap": skipped_mc,
            "failed_adv": skipped_adv,
        },
        "warnings": warnings,
    }

    # Persist a copy
    tag = f"sp500_{sector_filter or 'all'}_{top_n}"
    (OUT_DIR / f"screener_{tag}.json").write_text(json.dumps(out, indent=2))

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
