#!/usr/bin/env python3
"""dashboard — interactive stock/portfolio dashboard with live ticker switching.

Three tabs:
  1. Charts    — price, drawdown, analyst recs, price targets for any ticker
  2. Portfolio — editable holdings table + AI chat (runs local agent skills via claude CLI)
  3. Report    — latest daily report rendered as formatted HTML

Input (stdin JSON):
  { "kind": "ticker", "symbol": "NVDA", "port": 0 }

Output (stdout JSON — printed once server is ready):
  { "url": "http://localhost:5123", "data_as_of": "2026-04-21", "warnings": [] }
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.lib import data as pdata  # noqa: E402

PLOTLY_CDN     = "https://cdn.plot.ly/plotly-2.35.2.min.js"
POSITIONS_PATH = ROOT / "portfolio" / "positions.json"
REPORT_DIR     = ROOT / "research" / "daily"
SIGNALS_DIR    = ROOT / "research" / "signals"

# Bump this string whenever the agent prompts change — invalidates all cached signals.
PROMPT_VERSION = "v2"

DARK_BG       = "#0f1117"
GRID_COLOR    = "#2a2d3a"
TEXT_COLOR    = "#e0e0e0"
ACCENT_BLUE   = "#4f8ef7"
ACCENT_GREEN  = "#26a69a"
ACCENT_RED    = "#ef5350"
ACCENT_YELLOW = "#ffd54f"
ACCENT_PURPLE = "#ab47bc"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_json(obj):
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _safe_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_json(v) for v in obj]
    return obj


def _load_positions() -> list[dict]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(POSITIONS_PATH.read_text())
        return data if isinstance(data, list) else data.get("positions", [])
    except Exception:
        return []


def _save_positions(positions: list[dict]) -> None:
    existing = {}
    if POSITIONS_PATH.exists():
        try:
            existing = json.loads(POSITIONS_PATH.read_text())
        except Exception:
            existing = {}
    if isinstance(existing, dict):
        existing["positions"] = positions
        existing["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    else:
        existing = positions
    POSITIONS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def _cost_basis_for(symbol: str, positions: list[dict]) -> float | None:
    for p in positions:
        if p.get("symbol") == symbol or p.get("yf_symbol") == symbol:
            cb = p.get("cost_basis_adj_local")
            if cb and isinstance(cb, (int, float)) and cb > 0:
                return float(cb)
    return None


def _run_sector_allocation() -> dict:
    script = ROOT / ".claude/skills/sector-allocation/scripts/sectors.py"
    try:
        proc = subprocess.run(
            ["python3", str(script)], input=json.dumps({}),
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
    except Exception:
        pass
    return {}


def _latest_report_md() -> str:
    """Return the most recent daily report.md content, or empty string."""
    if not REPORT_DIR.exists():
        return ""
    dirs = sorted(REPORT_DIR.iterdir(), reverse=True)
    for d in dirs:
        p = d / "report.md"
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def _md_to_html(md: str) -> str:
    """Minimal markdown → HTML: headings, bold, tables, bullets, hr."""
    lines = md.split("\n")
    out = []
    in_table = False
    in_ul = False

    def flush_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def flush_table():
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    for raw in lines:
        line = raw.rstrip()

        # Horizontal rule
        if re.match(r"^-{3,}$", line):
            flush_ul(); flush_table()
            out.append("<hr>")
            continue

        # Headings
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush_ul(); flush_table()
            lvl = len(m.group(1)) + 1  # h2-h5
            text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", m.group(2))
            out.append(f"<h{lvl}>{text}</h{lvl}>")
            continue

        # Table row
        if line.startswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            # separator row
            if all(re.match(r"^-+$", c.replace(":", "")) for c in cells if c):
                if not in_table:
                    # wrap previous line as thead
                    if out and out[-1].startswith("<tr"):
                        out[-1] = "<thead>" + out[-1] + "</thead><tbody>"
                    in_table = True
                continue
            flush_ul()
            if not in_table:
                out.append("<table>")
                in_table = True
            row = "".join(f"<td>{re.sub(r'[*_`]', '', c)}</td>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue

        flush_table()

        # Bullet
        if re.match(r"^[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            text = line[2:].strip()
            text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
            text = re.sub(r"`(.*?)`", r"<code>\1</code>", text)
            out.append(f"<li>{text}</li>")
            continue

        flush_ul()

        # Blank line
        if not line:
            out.append("<br>")
            continue

        # Normal paragraph
        text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", line)
        text = re.sub(r"`(.*?)`", r"<code>\1</code>", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', text)
        out.append(f"<p>{text}</p>")

    flush_ul()
    flush_table()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Chart data builders
# ---------------------------------------------------------------------------

def _range_selector():
    return {
        "buttons": [
            {"count": 1,  "label": "1M", "step": "month", "stepmode": "backward"},
            {"count": 3,  "label": "3M", "step": "month", "stepmode": "backward"},
            {"count": 6,  "label": "6M", "step": "month", "stepmode": "backward"},
            {"count": 1,  "label": "1Y", "step": "year",  "stepmode": "backward"},
            {"count": 2,  "label": "2Y", "step": "year",  "stepmode": "backward"},
            {"count": 5,  "label": "5Y", "step": "year",  "stepmode": "backward"},
            {"step": "all", "label": "All"},
        ],
        "bgcolor": "#1a1d27", "activecolor": ACCENT_BLUE,
        "bordercolor": GRID_COLOR, "font": {"color": TEXT_COLOR},
    }


def _price_data(df: pd.DataFrame, symbol: str, cost_basis: float | None) -> dict:
    dates  = [str(d.date()) if hasattr(d, "date") else str(d) for d in df.index]
    closes = df["Close"].tolist()
    ma200  = df["Close"].rolling(200, min_periods=20).mean().tolist()
    ma150  = df["Close"].rolling(150, min_periods=15).mean().tolist()

    traces = [
        {"type": "scatter", "mode": "lines", "name": symbol,
         "x": dates, "y": closes, "line": {"color": ACCENT_BLUE, "width": 2}},
        {"type": "scatter", "mode": "lines", "name": "200-day MA",
         "x": dates, "y": ma200,
         "line": {"color": ACCENT_YELLOW, "width": 1.5, "dash": "dash"}},
        {"type": "scatter", "mode": "lines", "name": "150-day MA",
         "x": dates, "y": ma150,
         "line": {"color": ACCENT_PURPLE, "width": 1.5, "dash": "dot"}},
    ]
    if cost_basis is not None:
        traces.append({"type": "scatter", "mode": "lines", "name": "Cost basis",
                        "x": [dates[0], dates[-1]], "y": [cost_basis, cost_basis],
                        "line": {"color": ACCENT_GREEN, "width": 1.5, "dash": "dot"}})
    if "Volume" in df.columns:
        traces.append({"type": "bar", "name": "Volume", "yaxis": "y2",
                        "x": dates, "y": df["Volume"].tolist(),
                        "marker": {"color": "#37474f", "opacity": 0.5}})

    layout = {
        "title": {"text": f"{symbol} — Price & Moving Averages", "font": {"color": TEXT_COLOR}},
        "paper_bgcolor": DARK_BG, "plot_bgcolor": DARK_BG,
        "font": {"color": TEXT_COLOR},
        "xaxis": {
            "gridcolor": GRID_COLOR, "showgrid": True,
            "rangeselector": _range_selector(),
            "rangeslider": {"visible": True, "bgcolor": "#1a1d27", "bordercolor": GRID_COLOR},
            "type": "date",
        },
        "yaxis": {"gridcolor": GRID_COLOR, "title": "Price (USD)", "side": "left",
                  "autorange": True, "fixedrange": False},
        "yaxis2": {"overlaying": "y", "side": "right", "showgrid": False,
                   "title": "Volume", "titlefont": {"color": "#546e7a"}, "fixedrange": True},
        "legend": {"bgcolor": "rgba(0,0,0,0)"},
        "hovermode": "x unified",
        "margin": {"l": 60, "r": 60, "t": 50, "b": 60},
    }
    return {"traces": traces, "layout": layout}


def _drawdown_data(df: pd.DataFrame, symbol: str) -> dict:
    dates = [str(d.date()) if hasattr(d, "date") else str(d) for d in df.index]
    peak  = df["Close"].cummax()
    dd    = ((df["Close"] - peak) / peak * 100).tolist()

    traces = [{"type": "scatter", "mode": "lines", "name": "Drawdown",
               "x": dates, "y": dd, "fill": "tozeroy",
               "line": {"color": ACCENT_RED, "width": 1.5},
               "fillcolor": "rgba(239,83,80,0.15)"}]
    layout = {
        "title": {"text": f"{symbol} — Drawdown from Peak", "font": {"color": TEXT_COLOR}},
        "paper_bgcolor": DARK_BG, "plot_bgcolor": DARK_BG,
        "font": {"color": TEXT_COLOR},
        "xaxis": {
            "gridcolor": GRID_COLOR, "type": "date",
            "rangeselector": _range_selector(),
            "rangeslider": {"visible": True, "bgcolor": "#1a1d27", "bordercolor": GRID_COLOR},
        },
        "yaxis": {"gridcolor": GRID_COLOR, "title": "Drawdown %", "ticksuffix": "%",
                  "autorange": True, "fixedrange": False},
        "hovermode": "x unified",
        "margin": {"l": 60, "r": 40, "t": 50, "b": 60},
    }
    return {"traces": traces, "layout": layout}


def _analyst_recs_data(symbol: str) -> dict:
    try:
        import yfinance as yf
        summary = yf.Ticker(symbol).recommendations_summary
        if summary is not None and not summary.empty:
            row = summary.iloc[0]
            counts = {k: int(row.get(v, 0)) for k, v in [
                ("Strong Buy", "strongBuy"), ("Buy", "buy"),
                ("Hold", "hold"), ("Sell", "sell"), ("Strong Sell", "strongSell"),
            ] if int(row.get(v, 0)) > 0}
            if counts:
                colors = {"Strong Buy": "#26a69a", "Buy": "#66bb6a",
                          "Hold": "#ffd54f", "Sell": "#ef9a9a", "Strong Sell": "#ef5350"}
                traces = [{"type": "pie", "hole": 0.45,
                           "labels": list(counts.keys()), "values": list(counts.values()),
                           "marker": {"colors": [colors.get(k, "#888") for k in counts],
                                      "line": {"color": DARK_BG, "width": 2}},
                           "textinfo": "label+value",
                           "hovertemplate": "%{label}: %{value} analysts<extra></extra>"}]
                layout = {
                    "title": {"text": f"Analyst Recommendations ({sum(counts.values())} analysts)",
                              "font": {"color": TEXT_COLOR}},
                    "paper_bgcolor": DARK_BG, "font": {"color": TEXT_COLOR},
                    "showlegend": True, "legend": {"bgcolor": "rgba(0,0,0,0)"},
                    "margin": {"l": 20, "r": 20, "t": 50, "b": 20},
                }
                return {"traces": traces, "layout": layout}
    except Exception:
        pass
    return {}


def _price_target_data(info: dict, current_price: float | None) -> dict:
    low    = info.get("targetLowPrice")
    mean   = info.get("targetMeanPrice")
    median = info.get("targetMedianPrice")
    high   = info.get("targetHighPrice")
    current = current_price or info.get("currentPrice")
    if not any([low, mean, high]):
        return {}

    items = [("Low Target", low, "#ef9a9a"), ("Median Target", median, "#ffd54f"),
             ("Mean Target", mean, ACCENT_YELLOW), ("High Target", high, "#66bb6a"),
             ("Current Price", current, ACCENT_BLUE)]
    items = [(l, v, c) for l, v, c in items if v]
    labels = [l for l, _, _ in items]
    values = [v for _, v, _ in items]
    colors = [c for _, _, c in items]

    upside = ""
    if current and mean:
        upside = f"  |  Mean upside: {(mean - current) / current * 100:+.1f}%"

    traces = [{"type": "bar", "x": labels, "y": values,
               "marker": {"color": colors},
               "text": [f"${v:.2f}" for v in values],
               "textposition": "outside",
               "hovertemplate": "%{x}: $%{y:.2f}<extra></extra>"}]
    layout = {
        "title": {"text": f"Analyst Price Targets{upside}", "font": {"color": TEXT_COLOR}},
        "paper_bgcolor": DARK_BG, "plot_bgcolor": DARK_BG,
        "font": {"color": TEXT_COLOR},
        "xaxis": {"gridcolor": GRID_COLOR},
        "yaxis": {"gridcolor": GRID_COLOR, "tickprefix": "$"},
        "showlegend": False,
        "margin": {"l": 60, "r": 40, "t": 50, "b": 40},
    }
    return {"traces": traces, "layout": layout}


def _portfolio_vs_spy_data(positions: list[dict]) -> dict:
    spy_df = pdata.fetch_history("SPY", period="1y")
    if spy_df.empty:
        return {}
    dates   = [str(d.date()) if hasattr(d, "date") else str(d) for d in spy_df.index]
    spy_ret = (spy_df["Close"] / spy_df["Close"].iloc[0] - 1) * 100

    total    = sum(p.get("market_value_local", 0) or 0 for p in positions if p.get("currency") == "USD")
    port_ret = pd.Series(0.0, index=spy_df.index)
    for pos in positions:
        sym = pos.get("yf_symbol") or pos.get("symbol", "")
        if not sym or sym.startswith("IL"):
            continue
        mv = pos.get("market_value_local", 0) or 0
        if total <= 0 or mv <= 0:
            continue
        h = pdata.fetch_history(sym, period="1y")
        if h.empty:
            continue
        aligned = h["Close"].reindex(spy_df.index, method="ffill").dropna()
        if len(aligned) < 2:
            continue
        port_ret = port_ret.add(
            ((aligned / aligned.iloc[0] - 1) * (mv / total)).reindex(port_ret.index, fill_value=0)
        )

    traces = [
        {"type": "scatter", "mode": "lines", "name": "Your Portfolio (approx.)",
         "x": dates, "y": (port_ret * 100).tolist(),
         "line": {"color": ACCENT_BLUE, "width": 2}},
        {"type": "scatter", "mode": "lines", "name": "SPY",
         "x": dates, "y": spy_ret.tolist(),
         "line": {"color": ACCENT_YELLOW, "width": 2, "dash": "dash"}},
    ]
    layout = {
        "title": {"text": "Portfolio vs SPY — 1-year cumulative return", "font": {"color": TEXT_COLOR}},
        "paper_bgcolor": DARK_BG, "plot_bgcolor": DARK_BG,
        "font": {"color": TEXT_COLOR},
        "xaxis": {"gridcolor": GRID_COLOR},
        "yaxis": {"gridcolor": GRID_COLOR, "title": "Return %", "ticksuffix": "%"},
        "legend": {"bgcolor": "rgba(0,0,0,0)"}, "hovermode": "x unified",
        "margin": {"l": 60, "r": 40, "t": 50, "b": 40},
    }
    return {"traces": traces, "layout": layout}


def _sector_donut_data(alloc: dict) -> dict:
    by_sector = alloc.get("by_sector") or {}
    if not by_sector:
        return {}
    items = sorted(by_sector.items(), key=lambda kv: -kv[1])
    top = items[:8]
    if len(items) > 8:
        top.append(("Other", sum(w for _, w in items[8:])))
    traces = [{"type": "pie", "hole": 0.45,
               "labels": [k for k, _ in top],
               "values": [round(v * 100, 2) for _, v in top],
               "textinfo": "label+percent",
               "marker": {"line": {"color": DARK_BG, "width": 2}}}]
    layout = {
        "title": {"text": "Sector Allocation", "font": {"color": TEXT_COLOR}},
        "paper_bgcolor": DARK_BG, "font": {"color": TEXT_COLOR},
        "showlegend": True, "legend": {"bgcolor": "rgba(0,0,0,0)"},
        "margin": {"l": 20, "r": 20, "t": 50, "b": 20},
    }
    return {"traces": traces, "layout": layout}


def _valuation_cards(info: dict) -> list[dict]:
    current      = info.get("currentPrice") or info.get("previousClose") or info.get("navPrice")
    quote_type   = (info.get("quoteType") or "").upper()
    is_etf       = quote_type in ("ETF", "MUTUALFUND")

    if is_etf:
        total_assets  = info.get("totalAssets")
        expense_ratio = info.get("annualReportExpenseRatio") or info.get("expenseRatio")
        nav           = info.get("navPrice") or info.get("previousClose")
        category      = info.get("category") or info.get("fundFamily") or "N/A"
        three_yr      = info.get("threeYearAverageReturn")

        # yfinance is inconsistent: some fields are decimals (0.05 = 5%),
        # others are already percentages (1.71 = 1.71%). Normalize carefully.
        div_yield_raw = info.get("yield") or info.get("dividendYield")
        ytd_raw       = info.get("ytdReturn")

        def _aum(v):
            if v is None: return "N/A"
            if v >= 1e12: return f"${v / 1e12:.2f}T"
            if v >= 1e9:  return f"${v / 1e9:.1f}B"
            return f"${v / 1e6:.0f}M"

        def _pct_decimal(v):
            """Format a decimal-fraction value (0.05 → '5.00%')."""
            return f"{v * 100:.2f}%" if v is not None else "N/A"

        def _pct_smart(v):
            """Format a value that might already be in percent form.
            Heuristic: abs(v) > 1 means already percent; else decimal."""
            if v is None:
                return "N/A"
            if abs(v) > 1:
                return f"{v:.2f}%"
            return f"{v * 100:.2f}%"

        ytd_display = _pct_smart(ytd_raw)
        ytd_val     = ytd_raw if ytd_raw and abs(ytd_raw) > 1 else (ytd_raw * 100 if ytd_raw else 0)
        ytd_color   = (ACCENT_GREEN if ytd_val > 0 else
                       ACCENT_RED   if ytd_val < 0 else TEXT_COLOR)

        div_display = _pct_smart(div_yield_raw)

        return [
            {"label": "Price / NAV",     "value": f"${current:.2f}" if current else "N/A",    "color": TEXT_COLOR},
            {"label": "AUM",             "value": _aum(total_assets),                          "color": TEXT_COLOR},
            {"label": "Expense Ratio",   "value": _pct_decimal(expense_ratio),
             "color": ACCENT_RED if expense_ratio and expense_ratio > 0.005 else TEXT_COLOR},
            {"label": "Dividend Yield",  "value": div_display,                                 "color": TEXT_COLOR},
            {"label": "YTD Return",      "value": ytd_display,                                 "color": ytd_color},
            {"label": "3Y Avg Return",   "value": _pct_decimal(three_yr),                      "color": TEXT_COLOR},
            {"label": "Category",        "value": category,                                    "color": TEXT_COLOR},
        ]

    # Stock cards
    trailing_pe = info.get("trailingPE")
    forward_pe  = info.get("forwardPE")
    rec_key     = (info.get("recommendationKey") or "").replace("_", " ").title()
    rec_mean    = info.get("recommendationMean")
    n_analysts  = info.get("numberOfAnalystOpinions")
    market_cap  = info.get("marketCap")

    earnings_str = "N/A"
    earn_color   = TEXT_COLOR
    ts = info.get("earningsTimestamp")
    if ts:
        try:
            ed = dt.date.fromtimestamp(ts)
            days_until = (ed - dt.date.today()).days
            earnings_str = f"{ed.isoformat()} ({days_until}d)" if days_until >= 0 else f"{ed.isoformat()} (past)"
            if 0 <= days_until <= 14:
                earn_color = ACCENT_YELLOW
        except Exception:
            pass

    rec_color = (ACCENT_GREEN if rec_key in ("Strong Buy", "Buy") else
                 ACCENT_RED   if rec_key in ("Sell", "Strong Sell") else ACCENT_YELLOW)

    def _cap(v):
        if v is None: return "N/A"
        if v >= 1e12: return f"${v / 1e12:.2f}T"
        if v >= 1e9:  return f"${v / 1e9:.1f}B"
        return f"${v / 1e6:.0f}M"

    return [
        {"label": "Current Price",     "value": f"${current:.2f}" if current else "N/A",          "color": TEXT_COLOR},
        {"label": "Trailing P/E",      "value": f"{trailing_pe:.1f}x" if trailing_pe else "N/A",
         "color": ACCENT_RED if trailing_pe and trailing_pe > 50 else TEXT_COLOR},
        {"label": "Forward P/E",       "value": f"{forward_pe:.1f}x" if forward_pe else "N/A",
         "color": ACCENT_RED if forward_pe and forward_pe > 40 else TEXT_COLOR},
        {"label": "Market Cap",        "value": _cap(market_cap),                                  "color": TEXT_COLOR},
        {"label": "Next Earnings",     "value": earnings_str,                                       "color": earn_color},
        {"label": "Analyst Consensus", "value": f"{rec_key} ({rec_mean:.2f})" if rec_mean else rec_key or "N/A",
         "color": rec_color},
        {"label": "# Analysts",        "value": str(n_analysts) if n_analysts else "N/A",          "color": TEXT_COLOR},
    ]


# ---------------------------------------------------------------------------
# API payload builders
# ---------------------------------------------------------------------------

def build_ticker_payload(symbol: str, positions: list[dict], period: str = "10y") -> dict:
    warnings: list[str] = []
    df = pdata.fetch_history(symbol, period=period)
    if df.empty:
        df = pdata.fetch_history(symbol, period="2y")
    if df.empty:
        return {"error": f"No price data for {symbol}"}

    info = pdata.fetch_info(symbol)
    if not info:
        warnings.append(f"No fundamentals for {symbol}")

    cost_basis    = _cost_basis_for(symbol, positions)
    current_price = (info.get("currentPrice") if info else None) or \
                    (float(df["Close"].iloc[-1]) if not df.empty else None)

    payload: dict = {
        "symbol":   symbol,
        "warnings": warnings,
        "cards":    _valuation_cards(info) if info else [],
        "price":    _price_data(df, symbol, cost_basis),
        "drawdown": _drawdown_data(df, symbol),
    }

    recs = _analyst_recs_data(symbol)
    if recs:
        payload["analyst_recs"] = recs
    else:
        warnings.append("Analyst recommendations unavailable.")

    pt = _price_target_data(info, current_price) if info else {}
    if pt:
        payload["price_targets"] = pt
    else:
        warnings.append("Price targets unavailable.")

    return payload


def build_portfolio_payload(positions: list[dict]) -> dict:
    warnings: list[str] = []
    port   = _portfolio_vs_spy_data(positions)
    alloc  = _run_sector_allocation()
    sector = _sector_donut_data(alloc)
    if not port:
        warnings.append("Portfolio vs SPY chart unavailable.")
    if not sector:
        warnings.append("Sector donut unavailable.")
    return {"warnings": warnings, "portfolio_vs_spy": port, "sector": sector}


# ---------------------------------------------------------------------------
# AI chat — runs claude CLI as a subprocess, streams stdout back
# ---------------------------------------------------------------------------

def run_agent_chat(question: str, positions: list[dict]) -> str:
    """Run the question through the orchestrator agent pipeline via claude CLI.

    Uses the full orchestrator spec as system prompt so the chat has access
    to all skills (sector-allocation, risk-metrics, market-regime, etc.)
    and follows the same routing logic as the CLI orchestrator.
    """
    orchestrator_spec = _load_agent_spec("orchestrator")
    user_views = _load_user_views()

    held = [p for p in positions if p.get("asset_class") in ("stock", "etf")]
    holdings_summary = ", ".join(
        f"{p.get('yf_symbol') or p.get('symbol')} ({p.get('quantity')} shares, "
        f"{p.get('currency','USD')})"
        for p in held[:25]
    )

    today = dt.date.today().isoformat()
    prompt = f"""Today is {today}.

Current portfolio holdings: {holdings_summary}
Portfolio NAV: see sector-allocation skill output for exact figures.

User views context (read-only — challenge, do not defer):
{user_views}

User question: {question}

Follow your orchestrator spec to route this question. Use the skill scripts
(sector-allocation, risk-metrics, market-regime, earnings-calendar, currency-conversion)
as needed. Ground every number in a skill output or cited source.
Keep your response concise and actionable — aim for 200-400 words.
End with the educational-analysis disclaimer.
"""

    allowed_tools = "Read,Bash,Glob,Grep,WebSearch,WebFetch"

    claude_bin = "claude"
    try:
        cmd = [claude_bin, "--dangerously-skip-permissions", "-p", prompt,
               "--allowedTools", allowed_tools]
        if orchestrator_spec:
            cmd += ["--system-prompt", orchestrator_spec]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=480,
            cwd=str(ROOT),
        )
        output = proc.stdout.strip()
        if proc.returncode != 0 and not output:
            stderr = proc.stderr.strip()
            return f"Agent error (exit {proc.returncode}): {stderr or 'no output'}"
        return output or "(no response)"
    except FileNotFoundError:
        return "Error: `claude` CLI not found. Make sure Claude Code is installed and in PATH."
    except subprocess.TimeoutExpired:
        return "Error: agent timed out after 8 minutes."
    except Exception as e:
        return f"Error running agent: {e}"


# ---------------------------------------------------------------------------
# Deep analysis — full 3-agent pipeline with streamed step updates
# ---------------------------------------------------------------------------

def _agent_script(name: str) -> str:
    return str(ROOT / f".claude/agents/{name}.md")


def _signal_path(symbol: str, date: str) -> Path:
    return SIGNALS_DIR / f"{symbol}_{date}.json"


def _load_signal_cache(symbol: str) -> dict | None:
    """Return today's cached signal if it exists and matches PROMPT_VERSION, else None."""
    today = dt.date.today().isoformat()
    path  = _signal_path(symbol, today)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("prompt_version") != PROMPT_VERSION:
            return None
        return data
    except Exception:
        return None


def _save_signal_cache(symbol: str, result: dict) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    today  = dt.date.today().isoformat()
    result = dict(result)
    result["prompt_version"] = PROMPT_VERSION
    result["cached_at"]      = dt.datetime.now().isoformat(timespec="seconds")
    _signal_path(symbol, today).write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )


def _load_agent_spec(name: str) -> str:
    """Read .claude/agents/<name>.md, strip YAML frontmatter, return body as system prompt."""
    path = ROOT / ".claude" / "agents" / f"{name}.md"
    if not path.exists():
        return ""
    text = path.read_text()
    if text.startswith("---"):
        try:
            end = text.index("---", 3)
            text = text[end + 3:].lstrip()
        except ValueError:
            pass
    return text


def _load_user_views() -> str:
    """Read research/user-views.md (first 80 lines = Profile + Stated beliefs)."""
    path = ROOT / "research" / "user-views.md"
    if not path.exists():
        return ""
    lines = path.read_text().splitlines()
    return "\n".join(lines[:80])


def run_deep_analysis(symbol: str, positions: list[dict], on_step=None, force: bool = False) -> dict:
    """Run bull-officer + risk-officer (parallel) then portfolio-manager for one ticker.

    Injects the real .claude/agents/*.md spec as --system-prompt for each subprocess,
    so the dashboard uses the identical agent ruleset as the chat pipeline.
    Results cached to research/signals/<TICKER>_<date>.json; reused same-day unless
    force=True or PROMPT_VERSION changed.
    """
    def step(name, status):
        if on_step:
            on_step(name, status)

    symbol = symbol.upper().strip()
    today  = dt.date.today().isoformat()

    if not force:
        cached = _load_signal_cache(symbol)
        if cached:
            return cached

    bull_spec    = _load_agent_spec("bull-officer")
    risk_spec    = _load_agent_spec("risk-officer")
    manager_spec = _load_agent_spec("portfolio-manager")
    user_views   = _load_user_views()

    held = [p for p in positions if
            p.get("symbol", "").upper() == symbol or
            (p.get("yf_symbol") or "").upper() == symbol or
            (p.get("yf_symbol") or "").upper().replace(".TA", "") == symbol]
    if held:
        p = held[0]
        pos_context = (
            f"The user CURRENTLY HOLDS {p.get('quantity')} shares of {symbol} "
            f"at avg cost {p.get('cost_basis_adj_local', 'unknown')} "
            f"{p.get('currency','USD')}. "
            f"Current weight: {p.get('broker_pct_of_portfolio', '?')}% of portfolio. "
            f"This is a HOLD/TRIM/SELL decision, not a fresh buy."
        )
    else:
        pos_context = f"The user does NOT currently hold {symbol}. This is a BUY/PASS decision."

    held_syms = ", ".join(
        p.get("yf_symbol") or p.get("symbol", "")
        for p in positions if p.get("currency") == "USD"
    ) or "unknown"

    prior_context = ""
    if SIGNALS_DIR.exists():
        prior_files = sorted(SIGNALS_DIR.glob(f"{symbol}_*.json"), reverse=True)
        for pf in prior_files:
            if pf.name == f"{symbol}_{today}.json":
                continue
            try:
                prior      = json.loads(pf.read_text())
                prior_date = prior.get("date", pf.stem.split("_", 1)[-1])
                prior_sig  = prior.get("signal", "—")
                prior_conf = prior.get("confidence", "—")
                days_ago   = (dt.date.fromisoformat(today) - dt.date.fromisoformat(prior_date)).days
                if days_ago <= 14:
                    prior_context = (
                        f"\nPRIOR SIGNAL ({days_ago}d ago on {prior_date}): "
                        f"{prior_sig} | confidence: {prior_conf}\n"
                        f"If signal today contradicts prior, include: "
                        f"SIGNAL CHANGE: {prior_sig} → <new> because <specific new evidence>\n"
                        f"If it agrees, include: SIGNAL CONFIRMED: still {prior_sig} because <one sentence>.\n"
                    )
                break
            except Exception:
                continue

    officer_user_msg = (
        f"Ticker: {symbol}\nToday: {today}\n"
        f"{pos_context}\nCurrent US holdings: {held_syms}\n"
        f"{prior_context}\n"
        f"User views context (read-only — challenge, do not defer):\n{user_views}\n\n"
        f"Run your mandatory research process as specified. Output the full format per your spec."
    )

    step("bull_officer", "running")
    step("risk_officer", "running")

    results: dict = {}
    errors:  dict = {}

    def run_agent(name: str, system_spec: str, user_msg: str):
        try:
            cmd = ["claude", "--dangerously-skip-permissions", "-p", user_msg,
                   "--allowedTools", "Read,Bash,Glob,Grep,WebSearch,WebFetch"]
            if system_spec:
                cmd += ["--system-prompt", system_spec]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
            results[name] = proc.stdout.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            errors[name] = f"{name} timed out after 10 min"
        except Exception as e:
            errors[name] = str(e)

    bull_thread = threading.Thread(target=run_agent, args=("bull", bull_spec, officer_user_msg))
    risk_thread = threading.Thread(target=run_agent, args=("risk", risk_spec, officer_user_msg))
    bull_thread.start()
    risk_thread.start()
    bull_thread.join()
    risk_thread.join()

    if errors:
        return {"error": " | ".join(errors.values())}

    step("bull_officer", "done")
    step("risk_officer", "done")
    step("portfolio_manager", "running")

    bull_out = results.get("bull", "")
    risk_out = results.get("risk", "")

    manager_user_msg = (
        f"Ticker: {symbol}\nToday: {today}\n"
        f"{pos_context}\nCurrent US holdings: {held_syms}\n"
        f"{prior_context}\n"
        f"User views context (read-only):\n{user_views}\n\n"
        f"BULL CASE (from bull-officer):\n{bull_out}\n\n"
        f"BEAR CASE (from risk-officer):\n{risk_out}\n\n"
        f"Synthesize per your spec. Start output with:\n"
        f"SIGNAL: <BUY|HOLD|TRIM|SELL> | CONFIDENCE: <weak|moderate|strong>\n"
        f"data_as_of: {today}"
    )

    try:
        cmd = ["claude", "--dangerously-skip-permissions", "-p", manager_user_msg,
               "--allowedTools", "Read,Bash,Glob,Grep,WebSearch"]
        if manager_spec:
            cmd += ["--system-prompt", manager_spec]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
        manager_out = proc.stdout.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        manager_out = "portfolio-manager timed out after 10 min"
    except Exception as e:
        manager_out = f"Error: {e}"

    step("portfolio_manager", "done")

    signal_match = re.search(r"SIGNAL:\s*([A-Z]+)\s*\|", manager_out)
    conf_match   = re.search(r"CONFIDENCE:\s*(\w+)", manager_out)
    signal = signal_match.group(1).strip() if signal_match else "—"
    conf   = conf_match.group(1).strip()   if conf_match   else "—"

    result = {
        "symbol":     symbol,
        "date":       today,
        "bull":       bull_out,
        "risk":       risk_out,
        "manager":    manager_out,
        "signal":     signal,
        "confidence": conf,
    }
    _save_signal_cache(symbol, result)
    return result


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------

def _html_shell(initial_symbol: str, port: int, initial_positions: list[dict]) -> str:
    # Prepare initial portfolio rows as JSON for JS
    pos_js = json.dumps(_safe_json(initial_positions), ensure_ascii=False)

    # Latest report as HTML
    report_md   = _latest_report_md()
    report_html = _md_to_html(report_md) if report_md else "<p style='color:#888'>No report found. Run the daily report first.</p>"
    report_date = ""
    if report_md:
        m = re.search(r"# Daily Report — (\S+)", report_md)
        report_date = m.group(1) if m else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Investing Workbench</title>
<script src="{PLOTLY_CDN}"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:{DARK_BG}; color:{TEXT_COLOR}; font-family:system-ui,sans-serif; }}

/* ---- Tab bar ---- */
.tabbar {{
  display:flex; background:#0a0c12; border-bottom:2px solid {GRID_COLOR};
  padding:0 24px; position:sticky; top:0; z-index:100;
}}
.tab {{
  padding:14px 24px; cursor:pointer; font-size:0.95rem; color:#888;
  border-bottom:2px solid transparent; margin-bottom:-2px; white-space:nowrap;
  transition:color 0.15s;
}}
.tab:hover {{ color:{TEXT_COLOR}; }}
.tab.active {{ color:{ACCENT_BLUE}; border-bottom-color:{ACCENT_BLUE}; font-weight:600; }}
.tab-content {{ display:none; padding:24px; }}
.tab-content.active {{ display:block; }}

/* ---- Shared ---- */
.sub {{ font-size:0.82rem; color:#666; margin-bottom:20px; }}
.warn {{ background:#1a1200; border:1px solid #665500; border-radius:6px;
         padding:10px 14px; margin-bottom:16px; font-size:0.82rem; color:{ACCENT_YELLOW}; }}
.warn ul {{ margin:4px 0 0 16px; }}
.stat-row {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:24px; }}
.stat-card {{ background:#1a1d27; border:1px solid {GRID_COLOR}; border-radius:8px;
              padding:12px 18px; min-width:130px; flex:1; }}
.stat-label {{ font-size:0.72rem; color:#888; margin-bottom:4px; text-transform:uppercase; letter-spacing:0.05em; }}
.stat-value {{ font-size:1.1rem; font-weight:600; }}
.chart-full {{ width:100%; height:420px; margin-bottom:24px; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; margin-bottom:24px; }}
.grid2 > div {{ height:400px; }}
@media(max-width:860px){{ .grid2 {{ grid-template-columns:1fr; }} }}
#error-msg {{ color:{ACCENT_RED}; font-size:0.9rem; margin-bottom:16px; display:none; }}
button.primary {{
  background:{ACCENT_BLUE}; color:#fff; border:none; border-radius:6px;
  padding:8px 20px; font-size:0.9rem; cursor:pointer;
}}
button.primary:hover {{ opacity:0.85; }}
button.secondary {{
  background:transparent; border:1px solid {GRID_COLOR}; color:{TEXT_COLOR};
  border-radius:6px; padding:8px 16px; font-size:0.9rem; cursor:pointer;
}}
button.secondary:hover {{ border-color:{ACCENT_BLUE}; color:{ACCENT_BLUE}; }}
button.danger {{
  background:transparent; border:1px solid {ACCENT_RED}; color:{ACCENT_RED};
  border-radius:6px; padding:6px 12px; font-size:0.82rem; cursor:pointer;
}}
button.danger:hover {{ background:{ACCENT_RED}; color:#fff; }}

/* ---- Tab 1: Charts ---- */
.topbar {{ display:flex; align-items:center; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
.topbar h1 {{ font-size:1.3rem; color:#fff; white-space:nowrap; }}
.search-wrap {{ display:flex; gap:8px; flex:1; max-width:420px; }}
#ticker-input {{
  flex:1; background:#1a1d27; border:1px solid #3a3d4a; border-radius:6px;
  color:{TEXT_COLOR}; font-size:1rem; padding:8px 14px; outline:none;
  text-transform:uppercase;
}}
#ticker-input:focus {{ border-color:{ACCENT_BLUE}; }}
#loading {{ font-size:0.85rem; color:#888; min-width:80px; }}
#compare-mode-btn {{
  background:transparent; border:1px solid {ACCENT_BLUE}; color:{ACCENT_BLUE};
  border-radius:6px; padding:6px 14px; font-size:0.85rem; cursor:pointer; margin-left:auto;
}}
#compare-mode-btn.active {{ background:{ACCENT_BLUE}; color:#fff; }}
#compare-box {{
  display:none; background:#1a1d27; border:1px solid {ACCENT_BLUE};
  border-radius:8px; padding:16px 20px; margin-bottom:20px;
}}
#compare-box h3 {{ font-size:0.9rem; color:{ACCENT_BLUE}; margin-bottom:12px; }}
.cmp-row {{ display:flex; flex-wrap:wrap; gap:16px; }}
.cmp-card {{ flex:1; min-width:140px; }}
.cmp-label {{ font-size:0.72rem; color:#888; text-transform:uppercase; letter-spacing:0.05em; margin-bottom:4px; }}
.cmp-val {{ font-size:1.05rem; font-weight:600; }}
.cmp-hint {{ font-size:0.78rem; color:#666; margin-top:10px; }}

/* ---- Tab 2: Portfolio ---- */
.port-layout {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
@media(max-width:900px) {{ .port-layout {{ grid-template-columns:1fr; }} }}

.port-table-wrap {{ overflow-x:auto; }}
table.holdings {{
  width:100%; border-collapse:collapse; font-size:0.88rem;
}}
table.holdings th {{
  background:#1a1d27; color:#888; font-weight:600; padding:10px 12px;
  text-align:left; border-bottom:1px solid {GRID_COLOR}; white-space:nowrap;
}}
table.holdings td {{
  padding:9px 12px; border-bottom:1px solid #1a1d27; vertical-align:middle;
}}
table.holdings tr:hover td {{ background:#161920; }}
.port-actions {{ display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; align-items:center; }}
.add-form {{
  display:none; background:#1a1d27; border:1px solid {GRID_COLOR};
  border-radius:8px; padding:16px; margin-bottom:16px; gap:10px;
  flex-wrap:wrap;
}}
.add-form.open {{ display:flex; }}
.add-form input {{
  background:#0f1117; border:1px solid {GRID_COLOR}; border-radius:6px;
  color:{TEXT_COLOR}; padding:8px 12px; font-size:0.9rem; outline:none; flex:1; min-width:100px;
}}
.add-form input:focus {{ border-color:{ACCENT_BLUE}; }}
.badge {{
  display:inline-block; font-size:0.7rem; padding:2px 7px; border-radius:10px;
  font-weight:600; text-transform:uppercase;
}}
.badge-stock  {{ background:#1a2a1a; color:#66bb6a; }}
.badge-etf    {{ background:#1a1a2a; color:#7986cb; }}
.badge-fund   {{ background:#2a1a1a; color:#ef9a9a; }}
.badge-il     {{ background:#2a2a1a; color:#ffd54f; }}

/* ---- Chat ---- */
.chat-wrap {{
  display:flex; flex-direction:column; height:520px;
  background:#1a1d27; border:1px solid {GRID_COLOR}; border-radius:8px; overflow:hidden;
}}
.chat-header {{
  padding:12px 16px; background:#161920; border-bottom:1px solid {GRID_COLOR};
  font-size:0.85rem; font-weight:600; color:{ACCENT_BLUE};
}}
.chat-messages {{
  flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:12px;
}}
.msg {{ max-width:88%; border-radius:8px; padding:10px 14px; font-size:0.88rem; line-height:1.5; }}
.msg.user {{
  align-self:flex-end; background:{ACCENT_BLUE}; color:#fff; border-bottom-right-radius:2px;
}}
.msg.agent {{
  align-self:flex-start; background:#222535; color:{TEXT_COLOR}; border-bottom-left-radius:2px;
  white-space:pre-wrap;
}}
.msg.thinking {{ color:#888; font-style:italic; }}
.chat-input-row {{
  display:flex; gap:8px; padding:12px 16px; border-top:1px solid {GRID_COLOR};
  background:#161920;
}}
#chat-input {{
  flex:1; background:#0f1117; border:1px solid {GRID_COLOR}; border-radius:6px;
  color:{TEXT_COLOR}; padding:9px 14px; font-size:0.9rem; outline:none; resize:none;
  font-family:inherit;
}}
#chat-input:focus {{ border-color:{ACCENT_BLUE}; }}
#chat-send {{ padding:9px 20px; }}

/* Quick-ask chips */
.quick-chips {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }}
.chip {{
  background:#1a1d27; border:1px solid {GRID_COLOR}; color:#aaa;
  border-radius:20px; padding:5px 14px; font-size:0.8rem; cursor:pointer;
}}
.chip:hover {{ border-color:{ACCENT_BLUE}; color:{ACCENT_BLUE}; }}

/* ---- Tab 3: Deep Analysis ---- */
.progress-step {{
  background:#1a1d27; border:1px solid {GRID_COLOR}; border-radius:8px;
  padding:12px 16px; min-width:130px; text-align:center; flex:1;
  transition: border-color 0.3s;
}}
.progress-step.running {{ border-color:{ACCENT_YELLOW}; }}
.progress-step.done    {{ border-color:{ACCENT_GREEN}; }}
.step-icon  {{ font-size:1.4rem; margin-bottom:4px; }}
.step-label {{ font-size:0.78rem; color:#aaa; margin-bottom:4px; }}
.step-status {{
  font-size:0.72rem; text-transform:uppercase; letter-spacing:0.05em;
  color:#555; font-weight:600;
}}
.progress-step.running .step-status {{ color:{ACCENT_YELLOW}; }}
.progress-step.done    .step-status {{ color:{ACCENT_GREEN}; }}
.step-arrow {{ align-self:center; color:#444; font-size:1.2rem; }}
.deep-panel {{
  background:#1a1d27; border:1px solid {GRID_COLOR}; border-radius:8px;
  padding:16px 20px; margin-bottom:16px;
}}
.deep-panel-title {{
  font-size:0.88rem; font-weight:700; margin-bottom:10px;
  text-transform:uppercase; letter-spacing:0.04em;
}}
.deep-panel-body {{
  font-size:0.87rem; color:{TEXT_COLOR}; white-space:pre-wrap; line-height:1.6;
}}

/* ---- Tab 4: Opportunities ---- */
.opp-columns {{
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 20px;
}}
@media(max-width:1100px) {{ .opp-columns {{ grid-template-columns: 1fr 1fr; }} }}
@media(max-width:700px)  {{ .opp-columns {{ grid-template-columns: 1fr; }} }}
.opp-col-title {{
  font-size: 0.9rem; font-weight: 700; margin-bottom: 14px;
  padding-bottom: 8px; border-bottom: 2px solid {GRID_COLOR};
  text-transform: uppercase; letter-spacing: 0.05em;
}}
.opp-card {{
  background: #1a1d27; border: 1px solid {GRID_COLOR}; border-radius: 8px;
  padding: 14px 16px; margin-bottom: 12px; position: relative;
  transition: border-color 0.15s;
}}
.opp-card:hover {{ border-color: {ACCENT_BLUE}; }}
.opp-card-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
.opp-ticker {{ font-size: 1rem; font-weight: 700; color: {ACCENT_BLUE}; }}
.opp-name   {{ font-size: 0.78rem; color: #888; flex: 1; }}
.opp-score  {{
  font-size: 0.8rem; font-weight: 700; padding: 3px 9px;
  border-radius: 12px; white-space: nowrap;
}}
.score-high   {{ background: #1a2e1a; color: {ACCENT_GREEN}; }}
.score-medium {{ background: #2a2a1a; color: {ACCENT_YELLOW}; }}
.score-low    {{ background: #2a1a1a; color: {ACCENT_RED}; }}
.opp-signal {{
  font-size: 0.72rem; font-weight: 700; padding: 2px 8px;
  border-radius: 10px; text-transform: uppercase;
}}
.signal-buy  {{ background: #1a2e1a; color: {ACCENT_GREEN}; }}
.signal-hold {{ background: #2a2a1a; color: {ACCENT_YELLOW}; }}
.signal-sell {{ background: #2a1a1a; color: {ACCENT_RED}; }}
.opp-type {{ font-size: 0.7rem; color: #666; padding: 2px 6px; border: 1px solid #333; border-radius: 4px; }}
.opp-row {{ font-size: 0.82rem; margin-bottom: 5px; color: #bbb; line-height: 1.45; }}
.opp-row strong {{ color: {TEXT_COLOR}; }}
.opp-invest {{ font-size: 0.78rem; color: {ACCENT_YELLOW}; margin-top: 6px; }}
.opp-meta {{ font-size:0.72rem; color:#555; margin-top:6px; }}
.opp-loading {{ color: #888; font-size: 0.9rem; padding: 20px 0; }}
.opp-refresh-bar {{
  display: flex; align-items: center; gap: 12px; margin-bottom: 20px; flex-wrap: wrap;
}}

/* ---- Tab 5: Report ---- */
.report-wrap {{
  max-width:900px; margin:0 auto;
  background:#13151f; border:1px solid {GRID_COLOR}; border-radius:8px;
  padding:32px 36px; line-height:1.7;
}}
.report-wrap h2 {{ color:{ACCENT_BLUE}; font-size:1.15rem; margin:24px 0 10px; border-bottom:1px solid {GRID_COLOR}; padding-bottom:6px; }}
.report-wrap h3 {{ color:{ACCENT_YELLOW}; font-size:1rem; margin:18px 0 8px; }}
.report-wrap h4, .report-wrap h5 {{ color:#aaa; margin:14px 0 6px; }}
.report-wrap p  {{ margin:6px 0; font-size:0.9rem; }}
.report-wrap ul {{ margin:6px 0 6px 20px; }}
.report-wrap li {{ font-size:0.9rem; margin:3px 0; }}
.report-wrap table {{
  border-collapse:collapse; width:100%; font-size:0.85rem; margin:12px 0;
}}
.report-wrap th {{
  background:#1a1d27; padding:8px 12px; text-align:left;
  color:#888; border-bottom:1px solid {GRID_COLOR};
}}
.report-wrap td {{ padding:7px 12px; border-bottom:1px solid #1a1d27; }}
.report-wrap tr:hover td {{ background:#161920; }}
.report-wrap hr {{ border:none; border-top:1px solid {GRID_COLOR}; margin:20px 0; }}
.report-wrap code {{
  background:#1a1d27; padding:1px 6px; border-radius:4px; font-size:0.82rem;
}}
.report-wrap a {{ color:{ACCENT_BLUE}; }}
.report-wrap strong {{ color:#fff; }}
.report-meta {{ font-size:0.82rem; color:#666; margin-bottom:20px; }}

</style>
</head>
<body>

<div class="tabbar">
  <div class="tab active" onclick="switchTab('charts')">📈 Charts</div>
  <div class="tab"        onclick="switchTab('portfolio')">💼 Portfolio & AI Chat</div>
  <div class="tab"        onclick="switchTab('deep')">🔬 Deep Analysis</div>
  <div class="tab"        onclick="switchTab('opps')">💡 Opportunities</div>
  <div class="tab"        onclick="switchTab('report')">📋 Daily Report</div>
</div>

<!-- ================================================================ TAB 1: CHARTS -->
<div id="tab-charts" class="tab-content active">
  <div class="topbar">
    <h1 id="page-title">Stock Dashboard</h1>
    <div class="search-wrap">
      <input id="ticker-input" type="text" placeholder="Enter ticker, e.g. MSFT" value="{initial_symbol}"/>
      <button class="primary" id="go-btn">Load</button>
    </div>
    <button id="compare-mode-btn" title="Click two points to compare">⚖ Compare</button>
    <span id="loading"></span>
  </div>

  <div id="compare-box">
    <h3>📊 Price Comparison</h3>
    <div class="cmp-row" id="cmp-row"></div>
    <div class="cmp-hint" id="cmp-hint">Click a point on the price chart to set Point A, then click another for Point B.</div>
  </div>

  <div class="sub">Data via yfinance · Educational analysis, not investment advice · <code>localhost:{port}</code></div>
  <div id="error-msg"></div>
  <div id="warnings-box"></div>
  <div id="cards-row" class="stat-row"></div>
  <div id="return-badge" style="font-size:1.1rem;font-weight:700;margin-bottom:8px;min-height:1.4em;"></div>
  <div id="price-chart"    class="chart-full"></div>
  <div id="drawdown-chart" class="chart-full"></div>
  <div class="grid2">
    <div id="rec-pie"></div>
    <div id="price-targets"></div>
  </div>
</div>

<!-- ================================================================ TAB 2: PORTFOLIO + CHAT -->
<div id="tab-portfolio" class="tab-content">
  <div class="port-layout">

    <!-- LEFT: Holdings table -->
    <div>
      <h2 style="margin-bottom:16px;font-size:1.1rem;">Holdings</h2>
      <div class="port-actions">
        <button class="primary" onclick="toggleAddForm()">+ Add position</button>
        <button class="secondary" onclick="savePortfolio()">💾 Save</button>
        <span id="save-status" style="font-size:0.82rem;color:#888;"></span>
      </div>
      <div class="add-form" id="add-form">
        <input id="new-symbol"   type="text"   placeholder="Ticker (e.g. NVDA)" style="max-width:130px;text-transform:uppercase"/>
        <input id="new-name"     type="text"   placeholder="Name (optional)"    style="max-width:200px"/>
        <input id="new-qty"      type="number" placeholder="Shares"             style="max-width:100px" min="0" step="any"/>
        <input id="new-currency" type="text"   placeholder="USD / ILS"          style="max-width:80px" value="USD"/>
        <button class="primary" onclick="addPosition()">Add</button>
        <button class="secondary" onclick="toggleAddForm()">Cancel</button>
      </div>
      <div class="port-table-wrap">
        <table class="holdings">
          <thead>
            <tr>
              <th>Ticker</th><th>Name</th><th>Shares</th><th>Currency</th><th>Type</th><th></th>
            </tr>
          </thead>
          <tbody id="holdings-tbody"></tbody>
        </table>
      </div>
    </div>

    <!-- RIGHT: AI Chat -->
    <div>
      <h2 style="margin-bottom:12px;font-size:1.1rem;">AI Portfolio Manager</h2>
      <div class="quick-chips" id="quick-chips"></div>
      <div class="chat-wrap">
        <div class="chat-header">🤖 Powered by bull-officer + risk-officer + portfolio-manager agents</div>
        <div class="chat-messages" id="chat-messages">
          <div class="msg agent">Hello! I'm your AI portfolio manager. I use the bull-officer and risk-officer agents to analyse stocks, then synthesise a recommendation.\n\nAsk me about any holding, or type a ticker to get a signal.</div>
        </div>
        <div class="chat-input-row">
          <textarea id="chat-input" rows="2" placeholder="Ask about a stock or your portfolio…"></textarea>
          <button class="primary" id="chat-send">Send</button>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- ================================================================ TAB 3: DEEP ANALYSIS -->
<div id="tab-deep" class="tab-content">
  <div style="max-width:860px;margin:0 auto;">
    <h2 style="font-size:1.15rem;margin-bottom:6px;">🔬 Deep Analysis</h2>
    <p style="color:#888;font-size:0.85rem;margin-bottom:20px;">
      Runs the full 3-agent pipeline: <strong>bull-officer</strong> + <strong>risk-officer</strong> in parallel,
      then <strong>portfolio-manager</strong> synthesises. Takes 2–5 minutes.
    </p>

    <div style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap;align-items:center;">
      <input id="deep-ticker" type="text" placeholder="Enter ticker, e.g. NVDA"
        style="background:#1a1d27;border:1px solid {GRID_COLOR};border-radius:6px;
               color:{TEXT_COLOR};padding:9px 14px;font-size:1rem;outline:none;
               text-transform:uppercase;flex:1;max-width:260px;"/>
      <button class="primary" id="deep-run-btn" onclick="runDeepAnalysis()">▶ Run Deep Analysis</button>
      <span id="deep-loading" style="font-size:0.85rem;color:#888;"></span>
    </div>

    <!-- Portfolio ticker chips -->
    <div id="deep-chips" class="quick-chips" style="margin-bottom:20px;"></div>

    <!-- Progress tracker -->
    <div id="deep-progress" style="display:none;margin-bottom:24px;">
      <div style="font-size:0.82rem;color:#888;margin-bottom:10px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;">Pipeline Progress</div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;">
        <div class="progress-step" id="step-bull">
          <div class="step-icon">🐂</div>
          <div class="step-label">Bull Officer</div>
          <div class="step-status" id="step-bull-status">waiting</div>
        </div>
        <div class="step-arrow">→</div>
        <div class="progress-step" id="step-risk">
          <div class="step-icon">🛡</div>
          <div class="step-label">Risk Officer</div>
          <div class="step-status" id="step-risk-status">waiting</div>
        </div>
        <div class="step-arrow">→</div>
        <div class="progress-step" id="step-manager">
          <div class="step-icon">🧠</div>
          <div class="step-label">Portfolio Manager</div>
          <div class="step-status" id="step-manager-status">waiting</div>
        </div>
      </div>
    </div>

    <!-- Signal card -->
    <div id="deep-signal-card" style="display:none;margin-bottom:24px;">
      <div style="background:#1a1d27;border:1px solid {GRID_COLOR};border-radius:8px;padding:16px 20px;">
        <div style="font-size:0.75rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">Final Signal</div>
        <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap;">
          <div>
            <div style="font-size:0.75rem;color:#888;margin-bottom:2px;">Signal</div>
            <div id="deep-signal-val" style="font-size:1.4rem;font-weight:700;"></div>
          </div>
          <div>
            <div style="font-size:0.75rem;color:#888;margin-bottom:2px;">Confidence</div>
            <div id="deep-conf-val" style="font-size:1.1rem;font-weight:600;color:{ACCENT_YELLOW}"></div>
          </div>
          <div style="flex:1;font-size:0.78rem;color:#666;">Educational analysis, not investment advice.</div>
        </div>
      </div>
    </div>

    <!-- Three panels -->
    <div id="deep-results" style="display:none;">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;">
        <div class="deep-panel" id="panel-bull">
          <div class="deep-panel-title" style="color:{ACCENT_GREEN};">🟢 Bull Officer</div>
          <div class="deep-panel-body" id="body-bull"></div>
        </div>
        <div class="deep-panel" id="panel-risk">
          <div class="deep-panel-title" style="color:{ACCENT_RED};">🔴 Risk Officer</div>
          <div class="deep-panel-body" id="body-risk"></div>
        </div>
      </div>
      <div class="deep-panel">
        <div class="deep-panel-title" style="color:{ACCENT_BLUE};">🧠 Portfolio Manager Synthesis</div>
        <div class="deep-panel-body" id="body-manager"></div>
      </div>
    </div>

    <div id="deep-error" style="display:none;color:{ACCENT_RED};font-size:0.9rem;margin-top:16px;"></div>
  </div>
</div>

<!-- ================================================================ TAB 4: OPPORTUNITIES -->
<div id="tab-opps" class="tab-content">
  <div class="opp-refresh-bar">
    <h2 style="font-size:1.15rem;">💡 Opportunities</h2>
    <span id="opps-date" style="font-size:0.82rem;color:#666;"></span>
    <button class="secondary" onclick="loadOpportunities()" style="font-size:0.82rem;padding:6px 14px;">🔄 Refresh</button>
    <button class="primary"   onclick="runOpportunityScan()" id="opps-run-btn" style="font-size:0.82rem;padding:6px 14px;">▶ Run New Scan</button>
    <span id="opps-loading" style="font-size:0.82rem;color:#888;"></span>
  </div>
  <p style="font-size:0.82rem;color:#666;margin-bottom:20px;">
    Three independent lists — sorted by agent score. Runs daily with the report.
  </p>
  <div class="opp-columns">
    <div>
      <div class="opp-col-title" style="color:{ACCENT_BLUE};">📊 List 1 — Portfolio Fit
        <div style="font-size:0.7rem;font-weight:400;color:#666;margin-top:2px;text-transform:none;letter-spacing:0;">Agents know your holdings — fills gaps</div>
      </div>
      <div id="opps-list1"></div>
    </div>
    <div>
      <div class="opp-col-title" style="color:{ACCENT_GREEN};">👤 List 2 — Profile Fit
        <div style="font-size:0.7rem;font-weight:400;color:#666;margin-top:2px;text-transform:none;letter-spacing:0;">Agents know age/risk/cash only — no holdings</div>
      </div>
      <div id="opps-list2"></div>
    </div>
    <div>
      <div class="opp-col-title" style="color:{ACCENT_YELLOW};">🌍 List 3 — Market Picks
        <div style="font-size:0.7rem;font-weight:400;color:#666;margin-top:2px;text-transform:none;letter-spacing:0;">Agents know nothing about you — pure market</div>
      </div>
      <div id="opps-list3"></div>
    </div>
  </div>
</div>

<!-- ================================================================ TAB 5: REPORT -->
<div id="tab-report" class="tab-content">
  <div style="max-width:900px;margin:0 auto;">
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:20px;">
      <h2 style="font-size:1.2rem;">Daily Report</h2>
      <span class="report-meta">{report_date}</span>
      <button class="secondary" onclick="location.reload()" style="margin-left:auto;font-size:0.82rem;padding:6px 14px;">🔄 Refresh</button>
    </div>
    <div class="report-wrap">
      {report_html}
    </div>
  </div>
</div>


<script>
const SERVER = 'http://localhost:{port}';
const cfg    = {{responsive:true, displayModeBar:true}};

// ================================================================ TAB SWITCHING
function switchTab(name) {{
  const names = ['charts','portfolio','deep','opps','report'];
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', names[i] === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'opps') loadOpportunities();
}}

// ================================================================ TAB 1: CHARTS
let compareMode  = false;
let comparePoint = [null, null];

function setLoading(on) {{ document.getElementById('loading').textContent = on ? '⏳ Loading…' : ''; }}
function showError(msg)  {{
  const el = document.getElementById('error-msg');
  el.textContent = msg; el.style.display = msg ? 'block' : 'none';
}}

function renderCards(cards) {{
  document.getElementById('cards-row').innerHTML = cards.map(c => `
    <div class="stat-card">
      <div class="stat-label">${{c.label}}</div>
      <div class="stat-value" style="color:${{c.color}}">${{c.value}}</div>
    </div>`).join('');
}}

function renderWarnings(warnings) {{
  const box = document.getElementById('warnings-box');
  if (!warnings || !warnings.length) {{ box.innerHTML=''; return; }}
  box.innerHTML = `<div class="warn"><strong>Warnings:</strong>
    <ul>${{warnings.map(w=>`<li>${{w}}</li>`).join('')}}</ul></div>`;
}}

function plotOrClear(divId, data) {{
  const el = document.getElementById(divId);
  if (!data || !data.traces || !data.traces.length) {{ Plotly.purge(el); el.innerHTML=''; return; }}
  Plotly.react(el, data.traces, data.layout, cfg);
}}

function _updateReturnBadge(x0ms, x1ms) {{
  const el = document.getElementById('price-chart');
  if (!el || !el.data) return;
  const mainTrace = el.data.find(t => t.curveNumber === 0) || el.data[0];
  if (!mainTrace || !mainTrace.x || !mainTrace.y) return;
  // Find first and last closes within the visible range
  let firstPrice = null, lastPrice = null, firstDate = null, lastDate = null;
  for (let i = 0; i < mainTrace.x.length; i++) {{
    const t = new Date(mainTrace.x[i]).getTime();
    if (isNaN(t) || mainTrace.y[i] == null || !isFinite(mainTrace.y[i])) continue;
    if (t >= x0ms && t <= x1ms) {{
      if (firstPrice === null) {{ firstPrice = mainTrace.y[i]; firstDate = mainTrace.x[i]; }}
      lastPrice = mainTrace.y[i]; lastDate = mainTrace.x[i];
    }}
  }}
  const badge = document.getElementById('return-badge');
  if (!badge) return;
  if (firstPrice === null || lastPrice === null || firstPrice === 0) {{
    badge.textContent = ''; return;
  }}
  const pct   = (lastPrice - firstPrice) / firstPrice * 100;
  const days  = Math.round((new Date(lastDate) - new Date(firstDate)) / 86400000);
  const arrow = pct >= 0 ? '▲' : '▼';
  const color = pct >= 0 ? '{ACCENT_GREEN}' : '{ACCENT_RED}';
  const absPct = Math.abs(pct).toFixed(2);
  const absDollar = Math.abs(lastPrice - firstPrice).toFixed(2);
  badge.innerHTML = `<span style="color:${{color}}">${{arrow}} ${{absPct}}% (${{pct>=0?'+':'-'}}$${{absDollar}})</span>`
    + `<span style="color:#666;font-size:0.78rem;margin-left:10px;">`
    + `$${{firstPrice.toFixed(2)}} → $${{lastPrice.toFixed(2)}} · ${{days}} days</span>`;
}}

function _computeReturnFromCurrentRange() {{
  const el = document.getElementById('price-chart');
  if (!el || !el.layout || !el.layout.xaxis || !el.layout.xaxis.range) return;
  const [r0, r1] = el.layout.xaxis.range;
  const x0 = new Date(r0).getTime(), x1 = new Date(r1).getTime();
  if (!isNaN(x0) && !isNaN(x1)) _updateReturnBadge(x0, x1);
}}

function attachRangeSync() {{
  for (const divId of ['price-chart', 'drawdown-chart']) {{
    const el = document.getElementById(divId);
    if (!el) continue;
    el.removeAllListeners('plotly_relayout');
    el.on('plotly_relayout', ev => {{
      const hasRange = ev['xaxis.range[0]'] !== undefined || ev['xaxis.range'] !== undefined
                    || ev['xaxis.autorange'] !== undefined;
      if (!hasRange) return;
      const xrange = el.layout && el.layout.xaxis && el.layout.xaxis.range;
      if (!xrange) return;
      const x0 = new Date(xrange[0]).getTime(), x1 = new Date(xrange[1]).getTime();
      if (isNaN(x0) || isNaN(x1)) return;
      // Update return badge from price chart range
      if (divId === 'price-chart') _updateReturnBadge(x0, x1);
      let ymin = Infinity, ymax = -Infinity;
      for (const tr of (el.data || [])) {{
        if (!tr.x || !tr.y || tr.yaxis === 'y2') continue;
        for (let i = 0; i < tr.x.length; i++) {{
          const t = new Date(tr.x[i]).getTime();
          if (t >= x0 && t <= x1 && tr.y[i] != null && isFinite(tr.y[i])) {{
            if (tr.y[i] < ymin) ymin = tr.y[i];
            if (tr.y[i] > ymax) ymax = tr.y[i];
          }}
        }}
      }}
      if (!isFinite(ymin)) return;
      const pad = Math.max((ymax - ymin) * 0.06, Math.abs(ymax) * 0.01, 0.01);
      requestAnimationFrame(() => {{
        Plotly.relayout(el, {{'yaxis.range': [ymin - pad, ymax + pad]}});
      }});
    }});
  }}
}}

function updateComparePanel() {{
  const box = document.getElementById('compare-box');
  const row = document.getElementById('cmp-row');
  const hint = document.getElementById('cmp-hint');
  const [a, b] = comparePoint;
  if (!compareMode) {{ box.style.display='none'; return; }}
  box.style.display = 'block';
  if (!a) {{ hint.textContent='Click a point on the price chart to set Point A.'; row.innerHTML=''; return; }}
  if (!b) {{ hint.textContent=`Point A: ${{a.date}} @ $${{a.price.toFixed(2)}}. Now click Point B.`; row.innerHTML=''; return; }}
  const change = b.price - a.price;
  const pct    = (change / a.price * 100);
  const days   = Math.abs(Math.round((new Date(b.date) - new Date(a.date)) / 86400000));
  const color  = change >= 0 ? '{ACCENT_GREEN}' : '{ACCENT_RED}';
  const arrow  = change >= 0 ? '▲' : '▼';
  row.innerHTML = `
    <div class="cmp-card"><div class="cmp-label">Point A</div><div class="cmp-val">${{a.date}}</div><div style="color:#aaa;font-size:.9rem">$${{a.price.toFixed(2)}}</div></div>
    <div class="cmp-card"><div class="cmp-label">Point B</div><div class="cmp-val">${{b.date}}</div><div style="color:#aaa;font-size:.9rem">$${{b.price.toFixed(2)}}</div></div>
    <div class="cmp-card"><div class="cmp-label">Change</div><div class="cmp-val" style="color:${{color}}">${{arrow}} $${{Math.abs(change).toFixed(2)}}</div><div style="color:${{color}};font-size:.9rem">${{arrow}} ${{Math.abs(pct).toFixed(2)}}%</div></div>
    <div class="cmp-card"><div class="cmp-label">Period</div><div class="cmp-val">${{days}} days</div><div style="color:#aaa;font-size:.9rem">${{(days/365.25).toFixed(1)}} years</div></div>`;
  hint.textContent = 'Click any point to reset and start a new comparison.';
}}

function onPriceChartClick(eventData) {{
  if (!eventData || !eventData.points) return;
  const pt = eventData.points.find(p => p.curveNumber === 0);
  if (!pt) return;
  const clicked = {{ date: pt.x, price: pt.y }};
  if (!comparePoint[0] || comparePoint[1]) {{
    comparePoint = [clicked, null];
  }} else {{
    comparePoint[1] = clicked;
    if (new Date(comparePoint[0].date) > new Date(comparePoint[1].date))
      comparePoint = [comparePoint[1], comparePoint[0]];
  }}
  updateComparePanel();
}}

function toggleCompareMode() {{
  compareMode = !compareMode;
  comparePoint = [null, null];
  document.getElementById('compare-mode-btn').classList.toggle('active', compareMode);
  updateComparePanel();
  const el = document.getElementById('price-chart');
  if (compareMode) {{
    el.style.cursor = 'crosshair';
    el.on('plotly_click', onPriceChartClick);
  }} else {{
    el.style.cursor = '';
    el.removeAllListeners('plotly_click');
    document.getElementById('compare-box').style.display = 'none';
  }}
}}

async function loadTicker(symbol) {{
  if (!symbol) return;
  symbol = symbol.toUpperCase().trim();
  setLoading(true); showError('');
  comparePoint = [null, null]; updateComparePanel();
  document.getElementById('page-title').textContent = symbol + ' — Stock Analysis';
  try {{
    const res  = await fetch(`${{SERVER}}/data?symbol=${{encodeURIComponent(symbol)}}`);
    if (!res.ok) throw new Error(`Server returned ${{res.status}}`);
    const data = await res.json();
    if (data.error) {{ showError(data.error); setLoading(false); return; }}
    renderWarnings(data.warnings || []);
    renderCards(data.cards || []);
    plotOrClear('price-chart',    data.price);
    plotOrClear('drawdown-chart', data.drawdown);
    plotOrClear('rec-pie',        data.analyst_recs);
    plotOrClear('price-targets',  data.price_targets);
    attachRangeSync();
    setTimeout(() => _computeReturnFromCurrentRange(), 150);
    if (compareMode) {{
      const el = document.getElementById('price-chart');
      el.removeAllListeners('plotly_click');
      el.on('plotly_click', onPriceChartClick);
    }}
  }} catch(e) {{ showError('Failed to load data: ' + e.message); }}
  setLoading(false);
}}

document.getElementById('go-btn').addEventListener('click', () => loadTicker(document.getElementById('ticker-input').value));
document.getElementById('ticker-input').addEventListener('keydown', e => {{ if (e.key==='Enter') loadTicker(e.target.value); }});
document.getElementById('compare-mode-btn').addEventListener('click', toggleCompareMode);
loadTicker('{initial_symbol}');

// ================================================================ TAB 2: PORTFOLIO

let portfolio = {pos_js};

function badgeClass(p) {{
  if (p.asset_class === 'mutual_fund') return 'badge-fund';
  if (p.asset_class === 'etf') return 'badge-etf';
  if ((p.region || '').toUpperCase() === 'IL') return 'badge-il';
  return 'badge-stock';
}}
function badgeLabel(p) {{
  if (p.asset_class === 'mutual_fund') return 'Fund';
  if (p.asset_class === 'etf') return 'ETF';
  if ((p.region || '').toUpperCase() === 'IL') return 'IL';
  return 'Stock';
}}

function renderHoldings() {{
  const tbody = document.getElementById('holdings-tbody');
  tbody.innerHTML = portfolio.map((p, i) => `
    <tr>
      <td style="font-weight:600;color:{ACCENT_BLUE}">${{p.yf_symbol || p.symbol}}</td>
      <td style="color:#aaa;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{p.name_en || p.name || ''}}</td>
      <td>
        <input type="number" value="${{p.quantity || 0}}" min="0" step="any"
          style="background:#0f1117;border:1px solid {GRID_COLOR};border-radius:4px;color:{TEXT_COLOR};padding:4px 8px;width:90px;font-size:0.88rem"
          onchange="updateQty(${{i}}, this.value)"/>
      </td>
      <td style="color:#888">${{p.currency || ''}}</td>
      <td><span class="badge ${{badgeClass(p)}}">${{badgeLabel(p)}}</span></td>
      <td>
        <button class="danger" onclick="removePosition(${{i}})">✕</button>
        <button class="secondary" style="margin-left:4px;padding:4px 10px;font-size:0.78rem"
          onclick="startDeep('${{p.yf_symbol || p.symbol}}')">🔬 Deep</button>
      </td>
    </tr>`).join('');
}}

function updateQty(i, val) {{
  portfolio[i].quantity = parseFloat(val) || 0;
}}

function removePosition(i) {{
  if (!confirm(`Remove ${{portfolio[i].yf_symbol || portfolio[i].symbol}}?`)) return;
  portfolio.splice(i, 1);
  renderHoldings();
}}

function toggleAddForm() {{
  const f = document.getElementById('add-form');
  f.classList.toggle('open');
  if (f.classList.contains('open')) document.getElementById('new-symbol').focus();
}}

function addPosition() {{
  const sym = document.getElementById('new-symbol').value.trim().toUpperCase();
  const name = document.getElementById('new-name').value.trim();
  const qty  = parseFloat(document.getElementById('new-qty').value) || 0;
  const cur  = (document.getElementById('new-currency').value.trim() || 'USD').toUpperCase();
  if (!sym) return alert('Ticker is required');
  portfolio.push({{ symbol: sym, yf_symbol: sym, name_en: name, quantity: qty, currency: cur, asset_class: 'stock' }});
  renderHoldings();
  document.getElementById('new-symbol').value = '';
  document.getElementById('new-name').value   = '';
  document.getElementById('new-qty').value    = '';
  toggleAddForm();
}}

async function savePortfolio() {{
  const status = document.getElementById('save-status');
  status.textContent = '💾 Saving…';
  try {{
    const res = await fetch(`${{SERVER}}/positions`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(portfolio),
    }});
    const d = await res.json();
    status.textContent = d.ok ? '✓ Saved' : ('Error: ' + (d.error || 'unknown'));
    setTimeout(() => status.textContent = '', 3000);
  }} catch(e) {{
    status.textContent = 'Save failed: ' + e.message;
  }}
}}

renderHoldings();

// Quick-ask chips
const quickQuestions = [
  'What is the overall portfolio risk today?',
  'Which holding has the strongest buy signal?',
  'Any position I should trim or sell?',
  'What are my biggest concentration risks?',
  'Give me a market regime summary',
];
document.getElementById('quick-chips').innerHTML = quickQuestions.map(q =>
  `<div class="chip" onclick="sendChat('${{q.replace(/'/g,"\\'")}}')">${{q}}</div>`
).join('');

// ---- Chat ----
async function sendChat(text) {{
  const input = document.getElementById('chat-input');
  const msgs  = document.getElementById('chat-messages');
  const msg   = (text || input.value).trim();
  if (!msg) return;
  input.value = '';

  const userDiv = document.createElement('div');
  userDiv.className = 'msg user';
  userDiv.textContent = msg;
  msgs.appendChild(userDiv);

  const thinkDiv = document.createElement('div');
  thinkDiv.className = 'msg agent thinking';
  thinkDiv.textContent = '⏳ Running agents (bull-officer + risk-officer + portfolio-manager)… this takes ~30–60 seconds.';
  msgs.appendChild(thinkDiv);
  msgs.scrollTop = msgs.scrollHeight;

  document.getElementById('chat-send').disabled = true;
  try {{
    const res  = await fetch(`${{SERVER}}/chat`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ question: msg }}),
    }});
    const data = await res.json();
    thinkDiv.className   = 'msg agent';
    thinkDiv.textContent = data.answer || data.error || '(no response)';
  }} catch(e) {{
    thinkDiv.className   = 'msg agent';
    thinkDiv.textContent = 'Error: ' + e.message;
  }}
  document.getElementById('chat-send').disabled = false;
  msgs.scrollTop = msgs.scrollHeight;
}}

document.getElementById('chat-send').addEventListener('click', () => sendChat());
document.getElementById('chat-input').addEventListener('keydown', e => {{
  if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); sendChat(); }}
}});

function askAbout(symbol) {{
  switchTab('portfolio');
  sendChat(`Analyse ${{symbol}} — what is the current signal?`);
}}

// ================================================================ TAB 3: DEEP ANALYSIS

// Populate ticker chips from portfolio
(function() {{
  const usSymbols = portfolio
    .filter(p => p.currency === 'USD' && ['stock','etf'].includes(p.asset_class))
    .map(p => p.yf_symbol || p.symbol)
    .filter(Boolean);
  document.getElementById('deep-chips').innerHTML = usSymbols.map(s =>
    `<div class="chip" onclick="startDeep('${{s}}')">${{s}}</div>`
  ).join('');
}})();

function startDeep(symbol) {{
  document.getElementById('deep-ticker').value = symbol;
  switchTab('deep');
  runDeepAnalysis();
}}

function setStep(name, status) {{
  const el = document.getElementById('step-' + name);
  const st = document.getElementById('step-' + name + '-status');
  if (!el) return;
  el.classList.remove('running','done');
  if (status === 'running') {{ el.classList.add('running'); st.textContent = '⏳ running…'; }}
  if (status === 'done')    {{ el.classList.add('done');    st.textContent = '✓ done'; }}
  if (status === 'waiting') {{ st.textContent = 'waiting'; }}
}}

function signalColor(signal) {{
  if (!signal) return '{TEXT_COLOR}';
  const s = signal.toUpperCase();
  if (s.includes('STRONG BUY') || s.includes('BUY')) return '{ACCENT_GREEN}';
  if (s.includes('SELL'))  return '{ACCENT_RED}';
  if (s.includes('TRIM'))  return '#ef9a9a';
  if (s.includes('ADD'))   return '#66bb6a';
  return '{ACCENT_YELLOW}';
}}

// ================================================================ TAB 4: OPPORTUNITIES

function fitClass(fit) {{
  const f = (fit || '').toLowerCase();
  if (f === 'exceptional' || f === 'good') return 'score-high';
  if (f === 'neutral') return 'score-medium';
  return 'score-low';
}}
function fitLabel(fit) {{
  const f = (fit || '').toLowerCase();
  const labels = {{exceptional:'★ Exceptional', good:'Good', neutral:'Neutral', weak:'Weak', not_recommended:'Avoid'}};
  return labels[f] || fit || '—';
}}
function signalClass(signal) {{
  if (!signal) return 'signal-hold';
  const s = signal.toUpperCase();
  if (s.includes('BUY') || s.includes('ADD')) return 'signal-buy';
  if (s.includes('SELL') || s.includes('TRIM')) return 'signal-sell';
  return 'signal-hold';
}}

function analyseFromOpp(ticker) {{
  switchTab('deep');
  document.getElementById('deep-ticker').value = ticker;
  // Check cache first; if none, auto-run
  _checkDeepCache(ticker).then(() => {{
    const hasResult = document.getElementById('deep-results').style.display === 'block';
    if (!hasResult) runDeepAnalysis(false);
  }});
}}

function renderOppCard(item) {{
  if (item.error) return `<div class="opp-card"><div style="color:{ACCENT_RED};font-size:0.82rem;">${{item.error}}</div></div>`;
  const fc      = fitClass(item.fit);
  const signalC = signalClass(item.signal);
  const typeLabel = item.type === 'ETF' ? '🏦 ETF' : '📌 Stock';
  const has3Agent = item.analysis_cached && item.confidence && item.confidence !== '—';
  const agentBadge = has3Agent
    ? `<span style="font-size:0.7rem;background:#1e3a2f;color:#4caf50;border:1px solid #4caf50;
         border-radius:3px;padding:1px 6px;margin-left:6px;vertical-align:middle;">
         3-agent · ${{item.confidence}}</span>`
    : '';
  const managerLine = has3Agent && item.manager_summary
    ? `<div class="opp-row" style="font-style:italic;color:#aaa;font-size:0.82rem;">
         💬 ${{item.manager_summary}}</div>`
    : '';
  return `
  <div class="opp-card">
    <div class="opp-card-header">
      <span class="opp-ticker">${{item.ticker || '?'}}</span>
      <span class="opp-type">${{typeLabel}}</span>
      <span class="opp-signal ${{signalC}}">${{item.signal || '—'}}</span>
      ${{agentBadge}}
      <span class="opp-score ${{fc}}">${{fitLabel(item.fit)}}</span>
      <button class="secondary" style="margin-left:auto;padding:4px 12px;font-size:0.78rem;"
        onclick="analyseFromOpp('${{item.ticker}}')">Deep Analysis →</button>
    </div>
    <div class="opp-name">${{item.name || ''}}</div>
    <div class="opp-row"><strong>Why:</strong> ${{item.reason_fits || '—'}}</div>
    <div class="opp-row">🐂 ${{item.bull_point || '—'}}</div>
    <div class="opp-row">🛡 ${{item.risk_point || '—'}}</div>
    ${{managerLine}}
    <div class="opp-invest">💰 Max invest: ₪${{(item.max_invest_ils || 0).toLocaleString()}}</div>
    <div class="opp-meta">data_as_of: ${{item.data_as_of || '—'}}</div>
  </div>`;
}}

function renderOppList(containerId, items) {{
  const el = document.getElementById(containerId);
  if (!items || !items.length) {{
    el.innerHTML = '<div class="opp-loading">No data — run a scan first.</div>';
    return;
  }}
  el.innerHTML = items.map(renderOppCard).join('');
}}

async function loadOpportunities() {{
  document.getElementById('opps-loading').textContent = '⏳ Loading…';
  try {{
    const res  = await fetch(`${{SERVER}}/opportunities`);
    const data = await res.json();
    if (data.error) {{
      document.getElementById('opps-loading').textContent = 'Error: ' + data.error;
      return;
    }}
    document.getElementById('opps-date').textContent = data.date ? 'Last scan: ' + data.date : '';
    renderOppList('opps-list1', data.list1_portfolio_fit);
    renderOppList('opps-list2', data.list2_profile_fit);
    renderOppList('opps-list3', data.list3_market_picks);
    document.getElementById('opps-loading').textContent = '';
  }} catch(e) {{
    document.getElementById('opps-loading').textContent = 'No scan data yet — click ▶ Run New Scan.';
  }}
}}

let _oppsPollTimer = null;

async function runOpportunityScan() {{
  const btn = document.getElementById('opps-run-btn');
  btn.disabled = true;
  document.getElementById('opps-loading').textContent = '⏳ Starting scan…';
  ['opps-list1','opps-list2','opps-list3'].forEach(id => {{
    document.getElementById(id).innerHTML = '<div class="opp-loading">⏳ Agents working — bull-officer + risk-officer + portfolio-manager running for all 3 lists in parallel. This takes 5–10 minutes…</div>';
  }});
  try {{
    const res  = await fetch(`${{SERVER}}/opportunities/run`, {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ cash_ils: 120000 }})
    }});
    const data = await res.json();
    if (data.error) {{
      document.getElementById('opps-loading').textContent = 'Error: ' + data.error;
      btn.disabled = false;
      return;
    }}
    // Start polling
    document.getElementById('opps-loading').textContent = '⏳ Scan running in background…';
    _startOppsPoll(btn);
  }} catch(e) {{
    document.getElementById('opps-loading').textContent = 'Failed: ' + e.message;
    btn.disabled = false;
  }}
}}

const LIST_LABELS = {{
  'running:list1_portfolio_fit': '📊 Running List 1 — Portfolio Fit…',
  'running:list2_profile_fit':   '👤 Running List 2 — Profile Fit (List 1 done)…',
  'running:list3_market_picks':  '🌍 Running List 3 — Market Picks (Lists 1–2 done)…',
}};

function _startOppsPoll(btn) {{
  if (_oppsPollTimer) clearInterval(_oppsPollTimer);
  let elapsed = 0;
  _oppsPollTimer = setInterval(async () => {{
    elapsed += 15;
    try {{
      const res  = await fetch(`${{SERVER}}/opportunities/status`);
      const data = await res.json();
      if (data.status === 'running') {{
        const label = LIST_LABELS[data.current] || '⏳ Scan running…';
        document.getElementById('opps-loading').textContent =
          `${{label}} (${{elapsed}}s elapsed)`;
        // Load partial results so completed lists appear immediately
        try {{
          const pr = await fetch(`${{SERVER}}/opportunities/partial`);
          const pd = await pr.json();
          if (!pd.error) {{
            if (pd.list1_portfolio_fit?.length) renderOppList('opps-list1', pd.list1_portfolio_fit);
            if (pd.list2_profile_fit?.length)   renderOppList('opps-list2', pd.list2_profile_fit);
            if (pd.list3_market_picks?.length)  renderOppList('opps-list3', pd.list3_market_picks);
          }}
        }} catch(e) {{}}
      }} else if (data.status === 'done') {{
        clearInterval(_oppsPollTimer);
        document.getElementById('opps-loading').textContent = '✓ Done — loading results…';
        await loadOpportunities();
        document.getElementById('opps-loading').textContent = '';
        if (btn) btn.disabled = false;
      }}
    }} catch(e) {{ /* server momentarily busy, keep polling */ }}
  }}, 15000);  // poll every 15 seconds
}}

function _renderDeepResult(data) {{
  setStep('bull', 'done'); setStep('risk', 'done'); setStep('manager', 'done');
  const sigEl  = document.getElementById('deep-signal-val');
  const confEl = document.getElementById('deep-conf-val');
  sigEl.textContent  = data.signal || '—';
  sigEl.style.color  = signalColor(data.signal);
  confEl.textContent = data.confidence || '—';
  document.getElementById('deep-signal-card').style.display = 'block';
  document.getElementById('body-bull').textContent    = data.bull    || '(no output)';
  document.getElementById('body-risk').textContent    = data.risk    || '(no output)';
  document.getElementById('body-manager').textContent = data.manager || '(no output)';
  document.getElementById('deep-results').style.display = 'block';
  // Show cached_at timestamp + force-refresh link if from cache
  let meta = document.getElementById('deep-cache-meta');
  if (!meta) {{
    meta = document.createElement('div');
    meta.id = 'deep-cache-meta';
    meta.style.cssText = 'font-size:0.78rem;color:#666;margin-top:8px;';
    document.getElementById('deep-signal-card').after(meta);
  }}
  if (data.cached_at) {{
    meta.innerHTML = `Cached at ${{data.cached_at}} · <a href="#" style="color:#4f8ef7" onclick="runDeepAnalysis(true);return false;">Force Refresh</a>`;
  }} else {{
    meta.textContent = '';
  }}
}}

async function _checkDeepCache(symbol) {{
  try {{
    const res  = await fetch(`${{SERVER}}/signals/${{symbol}}`);
    const data = await res.json();
    if (data && data.signal) {{
      document.getElementById('deep-loading').textContent = `Loaded cached analysis for ${{symbol}}`;
      document.getElementById('deep-progress').style.display = 'flex';
      _renderDeepResult(data);
      document.getElementById('deep-loading').textContent = '';
    }}
  }} catch(e) {{}}
}}

async function runDeepAnalysis(force=false) {{
  const symbol = document.getElementById('deep-ticker').value.trim().toUpperCase();
  if (!symbol) {{ alert('Enter a ticker first'); return; }}

  // Reset UI
  document.getElementById('deep-error').style.display   = 'none';
  document.getElementById('deep-results').style.display = 'none';
  document.getElementById('deep-signal-card').style.display = 'none';
  document.getElementById('deep-progress').style.display = 'flex';
  document.getElementById('deep-run-btn').disabled = true;
  document.getElementById('deep-loading').textContent = `⏳ Analysing ${{symbol}}…`;
  ['bull','risk','manager'].forEach(s => setStep(s, 'waiting'));

  // Simulate step progress while waiting
  let elapsed = 0;
  const timer = setInterval(() => {{
    elapsed += 5;
    if (elapsed === 10)  setStep('bull',    'running');
    if (elapsed === 10)  setStep('risk',    'running');
    if (elapsed === 120) setStep('bull',    'done');
    if (elapsed === 120) setStep('risk',    'done');
    if (elapsed === 125) setStep('manager', 'running');
  }}, 5000);

  try {{
    const res  = await fetch(`${{SERVER}}/deep-analysis`, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ symbol, force }}),
    }});
    clearInterval(timer);
    const data = await res.json();

    if (data.error) {{
      document.getElementById('deep-error').textContent = 'Error: ' + data.error;
      document.getElementById('deep-error').style.display = 'block';
    }} else {{
      _renderDeepResult(data);
    }}
  }} catch(e) {{
    clearInterval(timer);
    document.getElementById('deep-error').textContent = 'Request failed: ' + e.message;
    document.getElementById('deep-error').style.display = 'block';
  }}

  document.getElementById('deep-run-btn').disabled = false;
  document.getElementById('deep-loading').textContent = '';
}}

// Check cache whenever ticker input changes (debounced)
let _deepCacheTimer = null;
document.addEventListener('DOMContentLoaded', () => {{
  const inp = document.getElementById('deep-ticker');
  if (inp) {{
    inp.addEventListener('input', () => {{
      clearTimeout(_deepCacheTimer);
      const sym = inp.value.trim().toUpperCase();
      if (sym.length >= 1) {{
        _deepCacheTimer = setTimeout(() => _checkDeepCache(sym), 600);
      }}
    }});
  }}
}});


</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    positions: list[dict] = []
    _scan_running: bool = False

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(_safe_json(data), ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        parsed = urlparse(self.path)
        qs     = parse_qs(parsed.query)

        if parsed.path == "/":
            self._send_html(_Handler._shell_html)
            return

        if parsed.path == "/data":
            symbol = (qs.get("symbol", [""])[0] or "").upper().strip()
            period = (qs.get("period", ["10y"])[0] or "10y").strip()
            if not symbol:
                self._send_json({"error": "symbol parameter required"}, 400)
                return
            try:
                payload = build_ticker_payload(symbol, _Handler.positions, period)
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/portfolio":
            try:
                payload = build_portfolio_payload(_Handler.positions)
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path.startswith("/signals/"):
            symbol = parsed.path.split("/signals/")[-1].upper().strip()
            if not symbol:
                self._send_json({"error": "symbol required"}, 400)
                return
            cached = _load_signal_cache(symbol)
            if cached:
                self._send_json(cached)
            else:
                self._send_json({"cached": False})
            return

        if parsed.path == "/opportunities":
            try:
                dirs = sorted(REPORT_DIR.iterdir(), reverse=True) if REPORT_DIR.exists() else []
                opp_path = None
                for d in dirs:
                    p = d / "opportunities.json"
                    if p.exists():
                        opp_path = p
                        break
                if opp_path and opp_path.exists():
                    self._send_json(json.loads(opp_path.read_text()))
                else:
                    self._send_json({"error": "No opportunities data yet. Click Run New Scan."})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/opportunities/status":
            today        = dt.date.today().isoformat()
            opp_path     = REPORT_DIR / today / "opportunities.json"
            partial_path = REPORT_DIR / today / "opportunities.partial.json"
            if _Handler._scan_running:
                # Try to read current list from partial file
                current = "running"
                if partial_path.exists():
                    try:
                        p = json.loads(partial_path.read_text())
                        current = p.get("_status", "running")
                    except Exception:
                        pass
                self._send_json({"status": "running", "current": current})
            elif opp_path.exists():
                self._send_json({"status": "done"})
            else:
                self._send_json({"status": "idle"})
            return

        if parsed.path == "/opportunities/partial":
            today        = dt.date.today().isoformat()
            partial_path = REPORT_DIR / today / "opportunities.partial.json"
            opp_path     = REPORT_DIR / today / "opportunities.json"
            # Return whatever is available: partial > final > nothing
            for p in (partial_path, opp_path):
                if p.exists():
                    try:
                        self._send_json(json.loads(p.read_text()))
                        return
                    except Exception:
                        break
            self._send_json({"error": "no partial data yet"})
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/positions":
            try:
                body = self._read_body()
                positions = json.loads(body)
                if not isinstance(positions, list):
                    self._send_json({"error": "expected JSON array"}, 400)
                    return
                _save_positions(positions)
                _Handler.positions = positions
                self._send_json({"ok": True, "count": len(positions)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/chat":
            try:
                body     = self._read_body()
                payload  = json.loads(body)
                question = payload.get("question", "").strip()
                if not question:
                    self._send_json({"error": "question is required"}, 400)
                    return
                answer = run_agent_chat(question, _Handler.positions)
                self._send_json({"answer": answer})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/opportunities/run":
            try:
                body     = self._read_body()
                params   = json.loads(body) if body else {}
                cash_ils = int(params.get("cash_ils", 120000))
                # Start background scan, return immediately
                if _Handler._scan_running:
                    self._send_json({"started": False, "message": "Scan already running"})
                    return
                _Handler._scan_running = True
                def _bg_scan():
                    try:
                        today = dt.date.today().isoformat()
                        scan_input = json.dumps({"cash_ils": cash_ils, "date": today})
                        proc = subprocess.run(
                            ["python3",
                             str(ROOT / ".claude/skills/opportunity-scanner/scripts/scan.py")],
                            input=scan_input, capture_output=True, text=True,
                            timeout=900, cwd=str(ROOT),
                        )
                        if proc.stdout.strip():
                            # scan.py already wrote the file; parse to validate
                            json.loads(proc.stdout.strip())
                    except Exception:
                        pass
                    finally:
                        _Handler._scan_running = False
                threading.Thread(target=_bg_scan, daemon=True).start()
                self._send_json({"started": True, "message": "Scan started in background"})
            except Exception as e:
                _Handler._scan_running = False
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/deep-analysis":
            try:
                body   = self._read_body()
                params = json.loads(body)
                symbol = (params.get("symbol") or "").upper().strip()
                force  = bool(params.get("force", False))
                if not symbol:
                    self._send_json({"error": "symbol is required"}, 400)
                    return
                result = run_deep_analysis(symbol, _Handler.positions, force=force)
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self.send_response(404)
        self.end_headers()


def _free_port(preferred: int = 0) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", preferred))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        raw    = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    kind   = params.get("kind", "ticker")
    symbol = (params.get("symbol") or "AAPL").upper().strip()
    port   = _free_port(int(params.get("port", 0)))

    positions = _load_positions()
    _Handler.positions  = positions
    _Handler._shell_html = _html_shell(symbol if kind == "ticker" else "", port, positions)

    server = HTTPServer(("localhost", port), _Handler)
    url    = f"http://localhost:{port}"

    result = {
        "url":        url,
        "data_as_of": dt.date.today().isoformat(),
        "warnings":   [],
        "note":       "Server is running. Close terminal / Ctrl-C to stop.",
    }
    print(json.dumps(result, indent=2), flush=True)

    threading.Timer(0.5, webbrowser.open, args=[url]).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
