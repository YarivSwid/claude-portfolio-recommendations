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


def _load_cash_ready() -> dict:
    """Return {'amount_usd': float|None, 'updated_at': str|None}."""
    if not POSITIONS_PATH.exists():
        return {"amount_usd": None, "updated_at": None}
    try:
        data = json.loads(POSITIONS_PATH.read_text())
        if not isinstance(data, dict):
            return {"amount_usd": None, "updated_at": None}
        amt = data.get("cash_ready_usd")
        return {
            "amount_usd": float(amt) if amt not in (None, "") else None,
            "updated_at": data.get("cash_ready_updated_at"),
        }
    except Exception:
        return {"amount_usd": None, "updated_at": None}


def _save_cash_ready(amount_usd: float | None) -> dict:
    existing: dict = {}
    if POSITIONS_PATH.exists():
        try:
            loaded = json.loads(POSITIONS_PATH.read_text())
            if isinstance(loaded, dict):
                existing = loaded
            else:
                existing = {"positions": loaded}
        except Exception:
            existing = {}
    if amount_usd is None:
        existing.pop("cash_ready_usd", None)
        existing.pop("cash_ready_updated_at", None)
    else:
        existing["cash_ready_usd"] = float(amount_usd)
        existing["cash_ready_updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    return _load_cash_ready()


def _fx_usdils() -> dict:
    """Fetch USDILS rate via the currency-conversion skill. Returns {rate, date} or {}."""
    script = ROOT / ".claude/skills/currency-conversion/scripts/fx.py"
    try:
        proc = subprocess.run(
            ["python3", str(script)], input="{}",
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            d = json.loads(proc.stdout)
            rate = d.get("rate_ils_per_usd") or d.get("usdils") or d.get("rate")
            date = d.get("as_of") or d.get("data_as_of") or d.get("date")
            if rate:
                return {"rate": float(rate), "date": date}
    except Exception:
        pass
    return {}


def _cash_ready_payload() -> dict:
    """Bundle cash-ready + FX + staleness flag for the dashboard."""
    cr   = _load_cash_ready()
    fx   = _fx_usdils()
    amt  = cr.get("amount_usd")
    upd  = cr.get("updated_at")
    stale = False
    if upd:
        try:
            updated = dt.datetime.fromisoformat(upd)
            stale = (dt.datetime.now() - updated).days >= 7
        except Exception:
            pass
    payload: dict = {
        "amount_usd":  amt,
        "amount_ils":  (amt * fx["rate"]) if (amt is not None and fx) else None,
        "updated_at":  upd,
        "stale":       stale,
        "fx_rate":     fx.get("rate"),
        "fx_date":     fx.get("date"),
    }
    return payload


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
    """Return the most recent daily report content, or empty string.
    Tolerates filename variants: report.md, daily-report.md, daily-scan.md."""
    if not REPORT_DIR.exists():
        return ""
    candidates = ("report.md", "daily-report.md", "daily-scan.md")
    dirs = sorted(REPORT_DIR.iterdir(), reverse=True)
    for d in dirs:
        if not d.is_dir():
            continue
        for name in candidates:
            p = d / name
            if p.exists():
                return p.read_text(encoding="utf-8")
    return ""


def _md_to_html(md: str) -> str:
    """Markdown → HTML: headings, bold, italic, tables, bullets, blockquotes, hr.
    Also applies signal badges (FE-1), heading anchors (FE-4), numeric cell styling (FE-6),
    blockquote callout boxes (FE-3), and italic text support (FE-8)."""

    # ---- FE-1: signal badge map ----
    _SIGNAL_MAP = [
        (re.compile(r"\*\*(STRONG BUY)\*\*"),  "sig sig-sbuy",   "STRONG BUY"),
        (re.compile(r"\*\*(BUY)\*\*"),          "sig sig-buy",    "BUY"),
        (re.compile(r"\*\*(ADD)\*\*"),          "sig sig-add",    "ADD"),
        (re.compile(r"\*\*(KEEP)\*\*"),         "sig sig-keep",   "KEEP"),
        (re.compile(r"\*\*(HOLD)\*\*"),         "sig sig-hold",   "HOLD"),
        (re.compile(r"\*\*(REDUCE)\*\*"),       "sig sig-reduce", "REDUCE"),
        (re.compile(r"\*\*(TRIM)\*\*"),         "sig sig-reduce", "TRIM"),
        (re.compile(r"\*\*(SELL)\*\*"),         "sig sig-sell",   "SELL"),
        (re.compile(r"\*\*(EXIT)\*\*"),         "sig sig-sell",   "EXIT"),
        (re.compile(r"\*\*(WATCH)\*\*"),        "sig sig-watch",  "WATCH"),
    ]

    def _apply_signal_badges(text: str) -> str:
        for pat, cls, label in _SIGNAL_MAP:
            text = pat.sub(f'<span class="{cls}">{label}</span>', text)
        return text

    def _fmt_inline(text: str) -> str:
        """Apply bold, italic, code, links, and signal badges to inline text."""
        # Signal badges first (consume **TOKEN** before bold fires)
        text = _apply_signal_badges(text)
        # Remaining **bold**
        text = re.sub(r"\*\*(.*?)\*\*", r"<strong>\1</strong>", text)
        # _italic_ (single underscore, not adjacent to word chars)
        text = re.sub(r"(?<!\w)_((?:[^_\n])+?)_(?!\w)", r"<em>\1</em>", text)
        # `code`
        text = re.sub(r"`(.*?)`", r"<code>\1</code>", text)
        # [link](url)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank">\1</a>', text)
        return text

    # ---- FE-6: numeric cell detection ----
    _NUM_RE = re.compile(r"^[+\-]?\$?[\d,.]+%?$")

    def _fmt_cell(c: str) -> str:
        raw = re.sub(r"[*_`]", "", c).strip()
        badged = _apply_signal_badges(c)
        # strip remaining markdown punctuation that wasn't consumed by badge
        clean = re.sub(r"[*_`]", "", badged) if badged == c else badged
        if _NUM_RE.match(raw) and raw not in ("—", "-"):
            if raw.startswith("+"):
                return f'<td class="num num-pos">{clean}</td>'
            elif raw.startswith("-"):
                return f'<td class="num num-neg">{clean}</td>'
            return f'<td class="num">{clean}</td>'
        return f"<td>{clean}</td>"

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

        # ---- FE-4: Headings with ID slugs + hover anchors ----
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            flush_ul(); flush_table()
            lvl = len(m.group(1)) + 1  # h2-h5
            raw_text = m.group(2)
            text = _fmt_inline(raw_text)
            slug = re.sub(r"[^a-z0-9]+", "-", raw_text.lower()).strip("-")
            out.append(
                f'<h{lvl} id="{slug}" class="report-heading">'
                f'{text}'
                f'<a class="heading-anchor" href="#{slug}" title="Link to section">#</a>'
                f'</h{lvl}>'
            )
            continue

        # Table row
        if line.startswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            # separator row
            if all(re.match(r"^-+$", c.replace(":", "")) for c in cells if c):
                if not in_table:
                    if out and out[-1].startswith("<tr"):
                        out[-1] = "<thead>" + out[-1] + "</thead><tbody>"
                    in_table = True
                continue
            flush_ul()
            if not in_table:
                out.append("<table>")
                in_table = True
            row = "".join(_fmt_cell(c) for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue

        flush_table()

        # ---- FE-3: Blockquote callout boxes ----
        if line.startswith("> "):
            flush_ul()
            text = _fmt_inline(line[2:].strip())
            stripped = line[2:].strip()
            if stripped.startswith(("⚠", "🚨")):
                cls = "callout callout-warn"
            elif stripped.startswith(("✓", "✅")):
                cls = "callout callout-ok"
            elif stripped.startswith(("ℹ",)):
                cls = "callout callout-info"
            else:
                cls = "callout callout-neutral"
            out.append(f'<blockquote class="{cls}">{text}</blockquote>')
            continue

        # Bullet
        if re.match(r"^[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            text = _fmt_inline(line[2:].strip())
            out.append(f"<li>{text}</li>")
            continue

        flush_ul()

        # Blank line
        if not line:
            out.append("<br>")
            continue

        # Normal paragraph
        text = _fmt_inline(line)
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


def _valuation_cards(info: dict, current_override: float | None = None) -> list[dict]:
    # Prefer caller-supplied current price (resolved with freshness logic in
    # build_ticker_payload) over info["currentPrice"], which can be a stale
    # pre-market snapshot.
    current      = current_override or info.get("currentPrice") or info.get("previousClose") or info.get("navPrice")
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

    cost_basis = _cost_basis_for(symbol, positions)

    # Prefer the latest history close over info["currentPrice"]: the `info` JSON
    # is often cached pre-market (early-morning hours) and carries yesterday's
    # close, while the history parquet is refreshed later in the day and has
    # today's actual close. We only fall back to info when history is older
    # than the info snapshot (e.g., a stock that didn't trade today).
    hist_last_close = float(df["Close"].iloc[-1]) if not df.empty else None
    hist_last_date  = df.index[-1].date() if not df.empty else None
    info_price      = info.get("currentPrice") if info else None
    today           = dt.date.today()

    if hist_last_close is not None and hist_last_date is not None and hist_last_date >= today:
        current_price = hist_last_close
        current_price_source = f"history (close on {hist_last_date})"
    elif info_price is not None:
        current_price = float(info_price)
        current_price_source = "info[currentPrice]"
    else:
        current_price = hist_last_close
        current_price_source = f"history (close on {hist_last_date})" if hist_last_date else "unavailable"

    if current_price is not None and hist_last_date is not None and hist_last_date < today:
        warnings.append(
            f"Price source: {current_price_source}. Latest cached close is {hist_last_date} "
            f"({(today - hist_last_date).days}d old)."
        )

    payload: dict = {
        "symbol":   symbol,
        "warnings": warnings,
        "cards":    _valuation_cards(info, current_price) if info else [],
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

    cash_ctx = ""
    try:
        cr = _load_cash_ready()
        if cr.get("amount_usd") is not None:
            cash_ctx = (
                f"Cash ready to invest: ${cr['amount_usd']:,.0f} USD"
                + (f" (set {cr['updated_at'][:10]})" if cr.get("updated_at") else "")
                + "\n"
            )
    except Exception:
        pass

    prompt = f"""Today is {today}.

Current portfolio holdings: {holdings_summary}
Portfolio NAV: see sector-allocation skill output for exact figures.
{cash_ctx}
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

    report_date = ""  # report is now loaded dynamically via /report endpoint

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
.report-wrap tbody tr:nth-child(even) td {{ background:#111318; }}
.report-wrap tr:hover td {{ background:#1a1f2a; }}
.report-wrap hr {{ border:none; border-top:1px solid {GRID_COLOR}; margin:20px 0; }}
.report-wrap code {{
  background:#1a1d27; padding:1px 6px; border-radius:4px; font-size:0.82rem;
}}
.report-wrap a {{ color:{ACCENT_BLUE}; }}
.report-wrap strong {{ color:#fff; }}
/* FE-8: italic metadata lines */
.report-wrap em {{ color:#888; font-style:italic; }}
.report-meta {{ font-size:0.82rem; color:#666; margin-bottom:20px; }}
/* FE-6: numeric cells */
.report-wrap td.num {{ text-align:right; font-variant-numeric:tabular-nums; font-family:monospace; font-size:0.83rem; }}
.report-wrap td.num-pos {{ color:{ACCENT_GREEN}; }}
.report-wrap td.num-neg {{ color:{ACCENT_RED}; }}
/* FE-1: signal badges */
.sig {{ display:inline-block; padding:1px 9px; border-radius:10px; font-size:0.78rem; font-weight:700; letter-spacing:0.04em; vertical-align:middle; }}
.sig-sbuy   {{ background:#0a1f1a; color:#00e5cc; border:1px solid #1a3a30; }}
.sig-buy    {{ background:#112211; color:{ACCENT_GREEN}; border:1px solid #1a3a2a; }}
.sig-add    {{ background:#0d2211; color:#66bb6a; border:1px solid #1a3a1a; }}
.sig-keep   {{ background:#1e2030; color:#90a4ae; border:1px solid #2a2d3a; }}
.sig-hold   {{ background:#22201a; color:{ACCENT_YELLOW}; border:1px solid #3a3322; }}
.sig-reduce {{ background:#2a2010; color:#ffa726; border:1px solid #3a2e18; }}
.sig-sell   {{ background:#2a1212; color:{ACCENT_RED}; border:1px solid #3a1f1f; }}
.sig-watch  {{ background:#1e1e10; color:#fff176; border:1px solid #2e2e18; }}
/* FE-3: blockquote callout boxes */
.report-wrap blockquote.callout {{
  margin:12px 0; padding:10px 16px; border-radius:0 6px 6px 0;
  font-size:0.88rem; line-height:1.6;
}}
.callout-warn    {{ border-left:4px solid {ACCENT_YELLOW}; background:#1e1a10; color:#e0d0a0; }}
.callout-ok      {{ border-left:4px solid {ACCENT_GREEN};  background:#0d1a18; color:#a0d0c0; }}
.callout-info    {{ border-left:4px solid {ACCENT_BLUE};   background:#0d1220; color:#a0b8d8; }}
.callout-neutral {{ border-left:4px solid #2a2d3a; background:#13151f; color:#aaa; }}
/* FE-4: heading anchors */
.report-wrap .report-heading {{ position:relative; }}
.heading-anchor {{
  display:inline-block; margin-left:8px; font-size:0.75rem; color:#3a3d4a;
  text-decoration:none; vertical-align:middle; opacity:0; transition:opacity 0.15s;
}}
.report-heading:hover .heading-anchor {{ opacity:1; color:{ACCENT_BLUE}; }}
/* FE-2: per-holding collapsible blocks */
.holding-block {{
  margin:4px 0; border-left:3px solid #2a2d3a;
  padding-left:10px; border-radius:0 4px 4px 0;
}}
.holding-block[open] {{ border-left-color:{ACCENT_BLUE}; }}
.holding-summary {{
  cursor:pointer; list-style:none; padding:4px 0;
  font-size:0.9rem; line-height:1.6;
}}
.holding-summary::-webkit-details-marker {{ display:none; }}
.holding-summary::before {{
  content:'▶ '; font-size:0.65rem; color:#555; margin-right:4px;
}}
.holding-block[open] .holding-summary::before {{ content:'▼ '; color:{ACCENT_BLUE}; }}

</style>
</head>
<body>

<div class="tabbar">
  <div class="tab active" onclick="switchTab('charts')">📈 Charts</div>
  <div class="tab"        onclick="switchTab('portfolio')">💼 Portfolio & AI Chat</div>
  <div class="tab"        onclick="switchTab('deep')">🔬 Deep Analysis</div>
  <div class="tab"        onclick="switchTab('opps')">💡 Opportunities</div>
  <div class="tab"        onclick="switchTab('filtered')">🔍 Opportunity Unfiltered</div>
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
      <div id="cash-ready-card" style="background:#13151f;border:1px solid {GRID_COLOR};border-radius:8px;padding:12px 16px;margin-bottom:16px;">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
          <span style="font-size:0.72rem;color:#888;text-transform:uppercase;letter-spacing:0.05em;">Cash ready to invest</span>
          <span id="cash-ready-display" style="font-size:1.05rem;font-weight:600;color:{TEXT_COLOR};">—</span>
          <span id="cash-ready-meta" style="font-size:0.74rem;color:#666;"></span>
          <button class="secondary" id="cash-edit-btn" onclick="toggleCashEdit()" style="margin-left:auto;font-size:0.78rem;padding:4px 10px;">✎ Edit</button>
        </div>
        <div id="cash-edit-form" style="display:none;margin-top:10px;gap:8px;align-items:center;flex-wrap:wrap;">
          <span style="font-size:0.82rem;color:#aaa;">$</span>
          <input id="cash-input" type="number" min="0" step="any" placeholder="50000" style="background:#0f1117;border:1px solid {GRID_COLOR};border-radius:6px;color:{TEXT_COLOR};padding:6px 12px;font-size:0.9rem;outline:none;max-width:140px;"/>
          <button class="primary" onclick="saveCash()" style="font-size:0.82rem;padding:6px 14px;">Save</button>
          <button class="secondary" onclick="toggleCashEdit()" style="font-size:0.82rem;padding:6px 14px;">Cancel</button>
          <button class="secondary" onclick="clearCash()" style="font-size:0.78rem;padding:4px 10px;color:#aaa;">Clear</button>
          <span id="cash-save-status" style="font-size:0.78rem;color:#888;"></span>
        </div>
        <div id="cash-stale-warn" style="display:none;margin-top:8px;font-size:0.78rem;color:{ACCENT_YELLOW};">⚠ Last updated more than 7 days ago — update if your cash position has changed.</div>
      </div>
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
    <button type="button" class="secondary" onclick="loadOpportunities(); return false;" style="font-size:0.82rem;padding:6px 14px;">🔄 Refresh</button>
    <button type="button" class="primary"   onclick="runOpportunityScan(); return false;" id="opps-run-btn" style="font-size:0.82rem;padding:6px 14px;">▶ Run New Scan</button>
    <span id="opps-loading" style="font-size:0.82rem;color:#888;"></span>
  </div>
  <p style="font-size:0.82rem;color:#666;margin-bottom:8px;">
    Three independent lists — sorted by agent score. Runs daily with the report.
  </p>
  <div id="opps-universe" style="font-size:0.78rem;color:#888;margin-bottom:20px;
       background:#1a1a1a;border:1px solid #2a2a2a;border-radius:4px;padding:8px 12px;display:none;">
  </div>
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

<!-- ================================================================ TAB 4b: FILTERED -->
<div id="tab-filtered" class="tab-content">
  <div class="opp-refresh-bar">
    <h2 style="font-size:1.15rem;">🔍 Opportunity Unfiltered</h2>
    <span id="unf-date" style="font-size:0.82rem;color:#666;"></span>
    <button type="button" class="secondary" onclick="loadUnfiltered(); return false;" style="font-size:0.82rem;padding:6px 14px;">🔄 Refresh</button>
    <button type="button" class="primary"   onclick="runUnfilteredScan(); return false;" id="unf-run-btn" style="font-size:0.82rem;padding:6px 14px;">▶ Run No-Filter Scan</button>
    <span id="unf-loading" style="font-size:0.82rem;color:#888;"></span>
  </div>
  <p style="font-size:0.82rem;color:#666;margin-bottom:8px;">
    5 picks per list from the FULL universe — parabolic-flagged tickers included,
    weak-evidence rows kept. Excludes tickers already in the main 💡 Opportunities scan
    so this view is purely "what the filters hid from me."
  </p>
  <div id="unf-universe" style="font-size:0.78rem;color:#888;margin-bottom:20px;
       background:#1a1a1a;border:1px solid #2a2a2a;border-radius:4px;padding:8px 12px;display:none;">
  </div>
  <div class="opp-columns">
    <div>
      <div class="opp-col-title" style="color:{ACCENT_BLUE};">📊 List 1 — Portfolio Fit
        <div style="font-size:0.7rem;font-weight:400;color:#666;margin-top:2px;text-transform:none;letter-spacing:0;">Same prompt as main, no filters</div>
      </div>
      <div id="unf-list1"></div>
    </div>
    <div>
      <div class="opp-col-title" style="color:{ACCENT_GREEN};">👤 List 2 — Profile Fit
        <div style="font-size:0.7rem;font-weight:400;color:#666;margin-top:2px;text-transform:none;letter-spacing:0;">Same prompt as main, no filters</div>
      </div>
      <div id="unf-list2"></div>
    </div>
    <div>
      <div class="opp-col-title" style="color:{ACCENT_YELLOW};">🌍 List 3 — Market Picks
        <div style="font-size:0.7rem;font-weight:400;color:#666;margin-top:2px;text-transform:none;letter-spacing:0;">Same prompt as main, no filters</div>
      </div>
      <div id="unf-list3"></div>
    </div>
  </div>
</div>

<!-- ================================================================ TAB 5: REPORT -->
<div id="tab-report" class="tab-content">
  <div style="max-width:900px;margin:0 auto;">
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:8px;flex-wrap:wrap;">
      <h2 style="font-size:1.2rem;">Daily Report</h2>
      <span class="report-meta" id="report-date-label">{report_date}</span>
      <button class="primary" id="report-run-btn" onclick="runReport()" style="margin-left:auto;font-size:0.82rem;padding:6px 14px;">▶ Generate Daily Report</button>
      <button class="secondary" id="improve-run-btn" onclick="runImprove()" style="font-size:0.82rem;padding:6px 14px;">✨ Suggest improvements</button>
      <button class="secondary" onclick="loadReport()" style="font-size:0.82rem;padding:6px 14px;">🔄 Refresh</button>
    </div>
    <div id="report-status" style="font-size:0.82rem;color:#888;margin-bottom:8px;min-height:1.2em;"></div>
    <div id="improve-status" style="font-size:0.82rem;color:#888;margin-bottom:8px;min-height:1.2em;"></div>
    <div id="improve-panel" style="display:none;background:#1a1d27;border:1px solid {GRID_COLOR};border-radius:8px;padding:18px 22px;margin-bottom:18px;"></div>
    <div class="report-wrap" id="report-content">
      <p style="color:#888">Loading report…</p>
    </div>
  </div>
</div>


<script>
const SERVER = 'http://localhost:{port}';
const cfg    = {{responsive:true, displayModeBar:true}};

// ================================================================ TAB SWITCHING
function switchTab(name) {{
  const names = ['charts','portfolio','deep','opps','filtered','report'];
  document.querySelectorAll('.tab').forEach((t,i) => t.classList.toggle('active', names[i] === name));
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (name === 'opps')     loadOpportunities();
  if (name === 'filtered') loadUnfiltered();
  if (name === 'report')   {{ loadReport(); _checkReportRunning(); _checkImproveRunning(); }}
}}

// ================================================================ REPORT (live reload)
async function loadReport() {{
  const el   = document.getElementById('report-content');
  const meta = document.getElementById('report-date-label');
  el.innerHTML = "<p style='color:#888'>Loading…</p>";
  try {{
    const r = await fetch(SERVER + '/report');
    const d = await r.json();
    if (d.error) {{ el.innerHTML = `<p style='color:#f88'>${{d.error}}</p>`; return; }}
    el.innerHTML = d.html;
    if (meta && d.date) meta.textContent = d.date;
    // FE-2: wrap per-holding <li> items in collapsible <details> blocks
    _wrapHoldingBlocks(el);
  }} catch(e) {{ el.innerHTML = "<p style='color:#f88'>Failed to load report.</p>"; }}
}}

function _wrapHoldingBlocks(container) {{
  // Find the Per-holding Analysis section by heading text
  let inHoldings = false;
  const nodes = Array.from(container.childNodes);
  for (const node of nodes) {{
    if (node.nodeType !== 1) continue;
    const tag = node.tagName;
    const txt = node.textContent || '';
    if (tag.match(/^H[2-5]$/)) {{
      inHoldings = txt.toLowerCase().includes('per-holding');
      if (!inHoldings && txt.toLowerCase().includes('early mover')) break;
      continue;
    }}
    if (!inHoldings) continue;
    // Each holding is a <ul> containing <li> items that start with a ticker
    if (tag === 'UL') {{
      const items = Array.from(node.querySelectorAll('li'));
      let i = 0;
      while (i < items.length) {{
        const li = items[i];
        const html = li.innerHTML;
        // Detect holding-start: contains a signal badge class
        if (html.includes('class="sig ')) {{
          // Collect subsequent non-signal <li> items as detail lines
          const summary = li.cloneNode(true);
          const details = document.createElement('details');
          details.className = 'holding-block';
          // Open by default for action signals; closed for KEEP
          const isKeep = html.includes('sig-keep');
          if (!isKeep) details.setAttribute('open', '');
          const sumEl = document.createElement('summary');
          sumEl.className = 'holding-summary';
          sumEl.innerHTML = summary.innerHTML;
          details.appendChild(sumEl);
          // Gather detail lines (non-signal siblings until next signal li)
          i++;
          while (i < items.length && !items[i].innerHTML.includes('class="sig ')) {{
            const detail = items[i].cloneNode(true);
            details.appendChild(detail);
            items[i].remove();
            i++;
          }}
          li.replaceWith(details);
        }} else {{
          i++;
        }}
      }}
    }}
  }}
}}

// ---- Generate daily report (background) ----
let reportPollTimer = null;
function _setReportStatus(text, color) {{
  const s = document.getElementById('report-status');
  if (!s) return;
  s.textContent = text || '';
  s.style.color = color || '#888';
}}
async function runReport() {{
  const btn = document.getElementById('report-run-btn');
  if (!btn) return;
  if (!confirm('Generate today\\'s daily report? This invokes the agent pipeline and can take 5-15 minutes.')) return;
  btn.disabled = true;
  btn.textContent = '⏳ Starting…';
  _setReportStatus('Starting report generation…', '{ACCENT_YELLOW}');
  try {{
    const r = await fetch(SERVER + '/report/run', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: '{{}}'
    }});
    const d = await r.json();
    if (d.error) {{
      _setReportStatus('Error: ' + d.error, '{ACCENT_RED}');
      btn.disabled = false; btn.textContent = '▶ Generate Daily Report';
      return;
    }}
    if (d.started === false) {{
      _setReportStatus(d.message || 'Already running…', '{ACCENT_YELLOW}');
    }}
    startReportPolling();
  }} catch(e) {{
    _setReportStatus('Failed to start: ' + e, '{ACCENT_RED}');
    btn.disabled = false; btn.textContent = '▶ Generate Daily Report';
  }}
}}
function startReportPolling() {{
  if (reportPollTimer) clearInterval(reportPollTimer);
  let ticks = 0;
  reportPollTimer = setInterval(async () => {{
    ticks++;
    try {{
      const r = await fetch(SERVER + '/report/status');
      const d = await r.json();
      const btn = document.getElementById('report-run-btn');
      if (d.status === 'running') {{
        const mins = Math.floor(ticks * 5 / 60);
        const secs = (ticks * 5) % 60;
        _setReportStatus(`⏳ Generating report… (${{mins}}m ${{secs}}s elapsed since polling started — agent pipeline typically takes 5-15 min)`, '{ACCENT_YELLOW}');
        if (btn) {{ btn.disabled = true; btn.textContent = '⏳ Running…'; }}
      }} else {{
        clearInterval(reportPollTimer); reportPollTimer = null;
        if (btn) {{ btn.disabled = false; btn.textContent = '▶ Generate Daily Report'; }}
        if (d.status === 'done') {{
          _setReportStatus(`✓ Report ready for ${{d.date}}. Reloading…`, '{ACCENT_GREEN}');
          loadReport();
        }} else if (d.status === 'error' || d.error) {{
          _setReportStatus('Finished with error: ' + (d.error || 'unknown'), '{ACCENT_RED}');
        }} else {{
          _setReportStatus('Idle (no report produced).', '#888');
        }}
      }}
    }} catch(e) {{ /* keep polling */ }}
  }}, 5000);
}}
// Resume polling if a run is already in progress when the tab opens
async function _checkReportRunning() {{
  try {{
    const r = await fetch(SERVER + '/report/status');
    const d = await r.json();
    if (d.status === 'running') startReportPolling();
  }} catch(e) {{}}
}}

// ---- Suggest improvements (background) ----
let improvePollTimer = null;
function _setImproveStatus(text, color) {{
  const s = document.getElementById('improve-status');
  if (!s) return;
  s.textContent = text || '';
  s.style.color = color || '#888';
}}
function _esc(s) {{
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({{
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }})[c]);
}}
async function runImprove() {{
  const btn = document.getElementById('improve-run-btn');
  if (!btn) return;
  if (!confirm('Run the report-improvement chain? Invokes stock-analyst + frontend-developer in parallel, then report-reviewer. Takes 3-10 minutes and uses Claude API tokens.')) return;
  btn.disabled = true;
  btn.textContent = '⏳ Starting…';
  _setImproveStatus('Starting improvement chain…', '{ACCENT_YELLOW}');
  try {{
    const r = await fetch(SERVER + '/report/improve/run', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}}, body: '{{}}'
    }});
    const d = await r.json();
    if (d.error) {{
      _setImproveStatus('Error: ' + d.error, '{ACCENT_RED}');
      btn.disabled = false; btn.textContent = '✨ Suggest improvements';
      return;
    }}
    if (d.started === false) _setImproveStatus(d.message || 'Already running…', '{ACCENT_YELLOW}');
    startImprovePolling();
  }} catch(e) {{
    _setImproveStatus('Failed to start: ' + e, '{ACCENT_RED}');
    btn.disabled = false; btn.textContent = '✨ Suggest improvements';
  }}
}}
function startImprovePolling() {{
  if (improvePollTimer) clearInterval(improvePollTimer);
  let ticks = 0;
  improvePollTimer = setInterval(async () => {{
    ticks++;
    try {{
      const r = await fetch(SERVER + '/report/improve/status');
      const d = await r.json();
      const btn = document.getElementById('improve-run-btn');
      if (d.status === 'running') {{
        const mins = Math.floor(ticks * 5 / 60);
        const secs = (ticks * 5) % 60;
        _setImproveStatus(`⏳ Running improvement chain… (${{mins}}m ${{secs}}s — analyst + frontend in parallel then reviewer; typically 3-10 min)`, '{ACCENT_YELLOW}');
        if (btn) {{ btn.disabled = true; btn.textContent = '⏳ Running…'; }}
      }} else {{
        clearInterval(improvePollTimer); improvePollTimer = null;
        if (btn) {{ btn.disabled = false; btn.textContent = '✨ Suggest improvements'; }}
        if (d.status === 'error') {{
          _setImproveStatus('Finished with error: ' + d.error, '{ACCENT_RED}');
        }} else if (d.status === 'done') {{
          _setImproveStatus(`✓ Proposals ready for ${{d.date}}. Loading…`, '{ACCENT_GREEN}');
          loadImprovePanel();
        }} else {{
          _setImproveStatus('Idle (no proposals produced).', '#888');
        }}
      }}
    }} catch(e) {{ /* keep polling */ }}
  }}, 5000);
}}
async function _checkImproveRunning() {{
  try {{
    const r = await fetch(SERVER + '/report/improve/status');
    const d = await r.json();
    if (d.status === 'running') startImprovePolling();
    else if (d.status === 'done') loadImprovePanel();
  }} catch(e) {{}}
}}
async function loadImprovePanel() {{
  const panel = document.getElementById('improve-panel');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = "<p style='color:#888'>Loading proposals…</p>";
  try {{
    const r = await fetch(SERVER + '/report/improve/result');
    const d = await r.json();
    if (d.error) {{ panel.innerHTML = `<p style='color:#f88'>${{_esc(d.error)}}</p>`; return; }}
    renderImprovePanel(d);
  }} catch(e) {{
    panel.innerHTML = "<p style='color:#f88'>Failed to load proposals.</p>";
  }}
}}
function renderImprovePanel(d) {{
  const panel = document.getElementById('improve-panel');
  const reviewer = d.reviewer || {{}};
  const analyst  = d.analyst_proposals || {{}};
  const frontend = d.frontend_proposals || {{}};
  const ranked   = (reviewer.ranked || []);
  const rejected = (reviewer.rejected || []);
  const summary  = reviewer.summary || '(no summary)';
  const sourceP  = d._source_path || 'research/daily/.../report-improvements.json';

  // Build a lookup of all proposals by id from analyst + frontend
  const lookup = {{}};
  (analyst.proposals  || []).forEach(p => lookup[p.id] = {{...p, _from:'stock-analyst'}});
  (frontend.proposals || []).forEach(p => lookup[p.id] = {{...p, _from:'frontend-developer'}});

  const html = [];
  html.push(`<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">`);
  html.push(`<h3 style="font-size:1rem;color:{ACCENT_BLUE};margin:0;">✨ Proposed improvements</h3>`);
  html.push(`<span style="font-size:0.78rem;color:#666;">${{_esc(sourceP)}}</span>`);
  html.push(`<button class="secondary" style="margin-left:auto;font-size:0.78rem;padding:4px 10px;" onclick="document.getElementById('improve-panel').style.display='none'">Hide</button>`);
  html.push(`</div>`);

  html.push(`<div style="background:#13151f;border-left:3px solid {ACCENT_BLUE};padding:10px 14px;margin-bottom:14px;font-size:0.88rem;line-height:1.55;white-space:pre-wrap;">${{_esc(summary)}}</div>`);

  if (ranked.length) {{
    html.push(`<h4 style="font-size:0.85rem;color:{ACCENT_GREEN};margin:14px 0 8px;">Ranked proposals (${{ranked.length}})</h4>`);
    ranked.forEach((r, i) => {{
      const p = lookup[r.id] || {{}};
      const title = r.title || p.title || r.id;
      const from  = r.from  || p._from || '—';
      const reason = r.rationale || '';
      const change = p.change || p.change_summary || '';
      html.push(`<div style="background:#13151f;border:1px solid {GRID_COLOR};border-radius:6px;padding:10px 14px;margin-bottom:8px;">`);
      html.push(`<div style="font-size:0.88rem;margin-bottom:4px;"><strong style="color:{TEXT_COLOR};">#${{i+1}}</strong> <span style="color:{ACCENT_YELLOW};">[${{_esc(from)}}]</span> <strong>${{_esc(title)}}</strong></div>`);
      if (reason) html.push(`<div style="font-size:0.8rem;color:#aaa;margin-bottom:6px;">${{_esc(reason)}}</div>`);
      if (change) html.push(`<details style="font-size:0.8rem;color:#999;"><summary style="cursor:pointer;color:#888;">Show change details</summary><div style="margin-top:6px;white-space:pre-wrap;font-family:monospace;font-size:0.78rem;color:#bbb;">${{_esc(change)}}</div></details>`);
      html.push(`</div>`);
    }});
  }} else {{
    html.push(`<p style="color:#888;font-size:0.85rem;">No proposals ranked.</p>`);
  }}

  if (rejected.length) {{
    html.push(`<details style="margin-top:14px;"><summary style="cursor:pointer;color:#888;font-size:0.82rem;">Rejected proposals (${{rejected.length}})</summary><div style="margin-top:8px;">`);
    rejected.forEach(rj => {{
      html.push(`<div style="font-size:0.8rem;color:#888;margin-bottom:4px;"><strong>${{_esc(rj.id||'?')}}</strong> <em>${{_esc(rj.title||'')}}</em> — ${{_esc(rj.why||'')}}</div>`);
    }});
    html.push(`</div></details>`);
  }}

  html.push(`<hr style="border:none;border-top:1px solid {GRID_COLOR};margin:16px 0;">`);
  html.push(`<div style="font-size:0.82rem;color:#aaa;line-height:1.6;">`);
  html.push(`<strong style="color:{ACCENT_YELLOW};">To apply these:</strong> switch to your Claude Code session and say:`);
  html.push(`<div style="background:#0f1117;border:1px solid {GRID_COLOR};border-radius:4px;padding:8px 12px;margin-top:8px;font-family:monospace;font-size:0.82rem;color:{ACCENT_BLUE};">apply the report improvements from ${{_esc(sourceP)}}</div>`);
  html.push(`<div style="margin-top:8px;color:#888;">Your main session will read the JSON, ask you which proposals to apply, and write the changes to <code>_md_to_html</code>, the Report tab CSS, and/or <code>.claude/commands/daily-report.md</code>.</div>`);
  html.push(`</div>`);

  panel.innerHTML = html.join('');
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

// ---- Cash ready to invest ----
function _fmtUSD(v) {{ return v == null ? '—' : ('$' + Number(v).toLocaleString('en-US',{{maximumFractionDigits:0}})); }}
function _fmtILS(v) {{ return v == null ? '' : ('₪' + Number(v).toLocaleString('en-US',{{maximumFractionDigits:0}})); }}
async function loadCashReady() {{
  try {{
    const r = await fetch(SERVER + '/cash-ready');
    const d = await r.json();
    const disp = document.getElementById('cash-ready-display');
    const meta = document.getElementById('cash-ready-meta');
    const card = document.getElementById('cash-ready-card');
    const warn = document.getElementById('cash-stale-warn');
    if (d.amount_usd == null) {{
      disp.textContent = 'not set';
      disp.style.color = '#666';
      meta.textContent = 'Click Edit to set the cash you have ready to deploy.';
      card.style.borderColor = '{GRID_COLOR}';
      warn.style.display = 'none';
    }} else {{
      disp.textContent = _fmtUSD(d.amount_usd);
      disp.style.color = '{ACCENT_GREEN}';
      const ils  = d.amount_ils != null ? _fmtILS(d.amount_ils) : '';
      const fxd  = d.fx_date ? ` (FX ${{d.fx_date}})` : '';
      const upd  = d.updated_at ? ` · updated ${{d.updated_at.slice(0,10)}}` : '';
      meta.textContent = (ils ? `~${{ils}}${{fxd}}` : '') + upd;
      if (d.stale) {{
        card.style.borderColor = '{ACCENT_YELLOW}';
        warn.style.display = 'block';
      }} else {{
        card.style.borderColor = '{GRID_COLOR}';
        warn.style.display = 'none';
      }}
    }}
    // Pre-fill the input with current value for easier edits
    const input = document.getElementById('cash-input');
    if (input && d.amount_usd != null) input.value = d.amount_usd;
  }} catch(e) {{ /* silent */ }}
}}
function toggleCashEdit() {{
  const f = document.getElementById('cash-edit-form');
  f.style.display = f.style.display === 'flex' ? 'none' : 'flex';
  if (f.style.display === 'flex') {{
    const input = document.getElementById('cash-input');
    setTimeout(() => input && input.focus(), 50);
  }}
}}
async function saveCash() {{
  const status = document.getElementById('cash-save-status');
  const input  = document.getElementById('cash-input');
  const raw    = input.value.trim();
  if (raw === '') {{ status.textContent = 'Enter an amount or click Clear'; status.style.color = '{ACCENT_RED}'; return; }}
  const amount = Number(raw);
  if (!Number.isFinite(amount) || amount < 0) {{ status.textContent = 'Invalid amount'; status.style.color = '{ACCENT_RED}'; return; }}
  status.textContent = '💾 Saving…';
  status.style.color = '#888';
  try {{
    const r = await fetch(SERVER + '/cash-ready', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{amount_usd: amount}}),
    }});
    const d = await r.json();
    if (d.error) {{ status.textContent = 'Error: ' + d.error; status.style.color = '{ACCENT_RED}'; return; }}
    status.textContent = '✓ Saved';
    status.style.color = '{ACCENT_GREEN}';
    await loadCashReady();
    setTimeout(() => {{ status.textContent = ''; toggleCashEdit(); }}, 800);
  }} catch(e) {{ status.textContent = 'Save failed: ' + e.message; status.style.color = '{ACCENT_RED}'; }}
}}
async function clearCash() {{
  if (!confirm('Clear cash-ready amount?')) return;
  try {{
    await fetch(SERVER + '/cash-ready', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{amount_usd: null}}),
    }});
    document.getElementById('cash-input').value = '';
    await loadCashReady();
    toggleCashEdit();
  }} catch(e) {{}}
}}
loadCashReady();

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

  // New schema (post-upgrade): bull_thesis / bear_threshold / catalyst / evidence_strength.
  // Legacy fallback: bull_point / risk_point.
  const bullText = item.bull_thesis || item.bull_point || '—';
  const bearText = item.bear_threshold || item.risk_point || '—';

  // evidence_strength badge — coloured by tier
  const evMap = {{
    weak:     {{bg:'#3a1e1e', border:'#cc6666', txt:'#ffaaaa'}},
    moderate: {{bg:'#3a2f1e', border:'#cc9966', txt:'#ffd699'}},
    strong:   {{bg:'#1e3a2f', border:'#66cc99', txt:'#aaffd6'}},
  }};
  const ev = item.evidence_strength;
  const evBadge = ev && evMap[ev]
    ? `<span style="font-size:0.7rem;background:${{evMap[ev].bg}};color:${{evMap[ev].txt}};
         border:1px solid ${{evMap[ev].border}};border-radius:3px;padding:1px 6px;
         margin-left:6px;vertical-align:middle;">evidence: ${{ev}}</span>`
    : '';

  // catalyst row — show date + what_to_watch when populated
  let catalystLine = '';
  if (item.catalyst && item.catalyst.type && item.catalyst.type !== 'none') {{
    const cd = item.catalyst.date ? ` (${{item.catalyst.date}})` : '';
    const cw = item.catalyst.what_to_watch ? ` — ${{item.catalyst.what_to_watch}}` : '';
    catalystLine = `<div class="opp-row">📅 ${{item.catalyst.type}}${{cd}}${{cw}}</div>`;
  }} else if (item.catalyst && item.catalyst.what_to_watch) {{
    catalystLine = `<div class="opp-row">📅 ${{item.catalyst.what_to_watch}}</div>`;
  }}

  // cooling_trigger row — only shown for WAIT-FOR-COOLING signals
  const coolingLine = item.cooling_trigger
    ? `<div class="opp-row" style="color:#ffd699;">🌡 cooling trigger: ${{item.cooling_trigger}}</div>`
    : '';

  // entry_quality + when_to_buy
  const eq = item.entry_quality ? `<span style="font-size:0.72rem;color:#888;">[${{item.entry_quality}}]</span> ` : '';
  const whenLine = item.when_to_buy
    ? `<div class="opp-row">⏱ ${{eq}}${{item.when_to_buy}}</div>`
    : '';

  return `
  <div class="opp-card">
    <div class="opp-card-header">
      <span class="opp-ticker">${{item.ticker || '?'}}</span>
      <span class="opp-type">${{typeLabel}}</span>
      <span class="opp-signal ${{signalC}}">${{item.signal || '—'}}</span>
      ${{evBadge}}
      ${{agentBadge}}
      <span class="opp-score ${{fc}}">${{fitLabel(item.fit)}}</span>
      <button class="secondary" style="margin-left:auto;padding:4px 12px;font-size:0.78rem;"
        onclick="analyseFromOpp('${{item.ticker}}')">Deep Analysis →</button>
    </div>
    <div class="opp-name">${{item.name || ''}}</div>
    <div class="opp-row"><strong>Why:</strong> ${{item.reason_fits || '—'}}</div>
    <div class="opp-row">🐂 ${{bullText}}</div>
    <div class="opp-row">🛡 ${{bearText}}</div>
    ${{catalystLine}}
    ${{coolingLine}}
    ${{whenLine}}
    ${{managerLine}}
    <div class="opp-invest">💰 Max invest: ₪${{(item.max_invest_ils || 0).toLocaleString()}}</div>
    <div class="opp-meta">data_as_of: ${{item.data_as_of || '—'}}</div>
  </div>`;
}}

function renderUniverseQuality(uq) {{
  const el = document.getElementById('opps-universe');
  if (!el) return;
  if (!uq) {{ el.style.display = 'none'; return; }}
  const dropped = (uq.parabolic_dropped || []).join(', ') || '(none)';
  el.style.display = 'block';
  el.innerHTML =
    `<strong style="color:#aaa;">📡 Universe pre-filter</strong> — `
    + `<span style="color:#66cc99;">${{uq.clean_count || 0}} clean</span> · `
    + `<span style="color:#ffd699;">${{uq.extended_count || 0}} extended</span> · `
    + `<span style="color:#ff9999;">${{(uq.parabolic_dropped || []).length}} parabolic dropped</span>`
    + ` <span style="color:#666;">(${{uq.data_as_of || 'n/a'}})</span>`
    + `<div style="margin-top:6px;color:#888;">Dropped (RSI≥80 OR &gt;50% above 200DMA OR ≥10 up-day streak): `
    + `<span style="color:#ff9999;">${{dropped}}</span></div>`;
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
  // First check whether a scan is currently in flight — if so, switch into
  // poll mode (lock the button, render per-list state) instead of painting
  // stale results from the prior scan.
  try {{
    const statusRes  = await fetch(`${{SERVER}}/opportunities/status`);
    const statusData = await statusRes.json();
    if (statusData.status === 'running') {{
      const btn = document.getElementById('opps-run-btn');
      if (btn && !btn.disabled) {{
        btn.disabled = true;
        btn.dataset.origLabel = btn.dataset.origLabel || btn.textContent;
        btn.textContent = '⏳ Running…';
      }}
      // Clear all slots; the poll tick will paint per-list state next.
      ['opps-list1','opps-list2','opps-list3'].forEach(id => {{
        const el = document.getElementById(id);
        if (el) el.innerHTML = '';
      }});
      _startOppsPoll(btn);
      return;
    }}
  }} catch(e) {{ /* fall through to normal load */ }}

  try {{
    const res  = await fetch(`${{SERVER}}/opportunities`);
    const data = await res.json();
    if (data.error) {{
      document.getElementById('opps-loading').textContent = 'Error: ' + data.error;
      return;
    }}
    document.getElementById('opps-date').textContent = data.date ? 'Last scan: ' + data.date : '';
    renderUniverseQuality(data._universe_quality);
    renderOppList('opps-list1', data.list1_portfolio_fit);
    renderOppList('opps-list2', data.list2_profile_fit);
    renderOppList('opps-list3', data.list3_market_picks);
    document.getElementById('opps-loading').textContent = '';
  }} catch(e) {{
    document.getElementById('opps-loading').textContent = 'No scan data yet — click ▶ Run New Scan.';
  }}
}}

let _oppsPollTimer = null;

// Map the partial-file `_status` value to which list is *currently* in-flight.
// Anything not currently in-flight is either DONE (we've already rendered picks)
// or QUEUED (we leave the slot empty per user spec — no premature placeholder).
const LIST_KEYS = ['list1_portfolio_fit', 'list2_profile_fit', 'list3_market_picks'];
const LIST_DOM  = {{
  list1_portfolio_fit: 'opps-list1',
  list2_profile_fit:   'opps-list2',
  list3_market_picks:  'opps-list3',
}};
const LIST_TITLE = {{
  list1_portfolio_fit: 'List 1 — Portfolio Fit',
  list2_profile_fit:   'List 2 — Profile Fit',
  list3_market_picks:  'List 3 — Market Picks',
}};
const LIST_LABELS = {{
  'running:list1_portfolio_fit': '📊 Running List 1 — Portfolio Fit…',
  'running:list2_profile_fit':   '👤 Running List 2 — Profile Fit (List 1 done)…',
  'running:list3_market_picks':  '🌍 Running List 3 — Market Picks (Lists 1–2 done)…',
}};

function _parseCurrentList(current) {{
  // current is "phase1:list2_profile_fit" or "running:list2_profile_fit" or "running"
  if (!current) return null;
  for (const k of LIST_KEYS) {{
    if (current.endsWith(k)) return k;
  }}
  return null;
}}

function _renderListSlot(listKey, partialData, currentlyRunning) {{
  // Three states per slot:
  //   DONE     → render the picks
  //   RUNNING  → show "⏳ Running List N…"
  //   QUEUED   → empty (no placeholder)
  const el = document.getElementById(LIST_DOM[listKey]);
  if (!el) return;
  const items = partialData?.[listKey] || [];
  if (items.length > 0) {{
    renderOppList(LIST_DOM[listKey], items);
    return;
  }}
  if (listKey === currentlyRunning) {{
    el.innerHTML = `<div class="opp-loading">⏳ Running ${{LIST_TITLE[listKey]}}… (agents working — typically 2–4 minutes per list)</div>`;
    return;
  }}
  // Queued — leave the slot empty per user request (no premature placeholder).
  el.innerHTML = '';
}}

async function runOpportunityScan() {{
  const btn = document.getElementById('opps-run-btn');
  // Lock the button for the entire scan. Re-enable only on a terminal status.
  btn.disabled = true;
  btn.dataset.origLabel = btn.dataset.origLabel || btn.textContent;
  btn.textContent = '⏳ Running…';
  document.getElementById('opps-loading').textContent = '⏳ Starting scan…';
  // Show "Queued" placeholders immediately so the click has visible feedback
  // (the 15-second gap before the first poll tick used to look like the page
  // just refreshed and cleared itself).
  LIST_KEYS.forEach(k => {{
    const el = document.getElementById(LIST_DOM[k]);
    if (el) el.innerHTML = '<div class="opp-loading">⏳ Queued — waiting for agent to start…</div>';
  }});
  // Resolve cash from the Cash Ready card (omit field so the server can fall
  // back to positions.json.cash_ready_usd × FX, which mirrors the scanner CLI).
  let body = {{}};
  try {{
    const cr = await fetch(`${{SERVER}}/cash-ready`);
    const cd = await cr.json();
    if (cd && cd.amount_ils) {{
      body.cash_ils = Math.round(cd.amount_ils);
    }}
  }} catch(e) {{ /* leave body empty → server defaults */ }}
  try {{
    const res  = await fetch(`${{SERVER}}/opportunities/run`, {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(body)
    }});
    const data = await res.json();
    if (data.error) {{
      document.getElementById('opps-loading').textContent = 'Error: ' + data.error;
      _resetOppsButton(btn);
      return;
    }}
    if (data.started === false) {{
      document.getElementById('opps-loading').textContent =
        '⚠ ' + (data.message || 'Scan already running — picking up existing run.');
    }} else {{
      const cashNote = body.cash_ils
        ? ` (cash ₪${{body.cash_ils.toLocaleString()}})`
        : ' (cash: server default)';
      document.getElementById('opps-loading').innerHTML =
        `⏳ Scan running in background${{cashNote}} — Phase 1 ~2 min, then Phase 2 bull/risk/manager on ~30 tickers (~20–40 min total)…`;
    }}
    _startOppsPoll(btn);
  }} catch(e) {{
    document.getElementById('opps-loading').textContent = 'Failed: ' + e.message;
    _resetOppsButton(btn);
  }}
}}

function _resetOppsButton(btn) {{
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = btn.dataset.origLabel || '▶ Run New Scan';
}}

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
        // Per-list rendering: only paint slots that are DONE or actively RUNNING.
        // Queued lists stay empty until their turn arrives.
        try {{
          const pr = await fetch(`${{SERVER}}/opportunities/partial`);
          const pd = await pr.json();
          if (!pd.error) {{
            const currentlyRunning = _parseCurrentList(data.current);
            LIST_KEYS.forEach(k => _renderListSlot(k, pd, currentlyRunning));
          }}
        }} catch(e) {{}}
      }} else if (data.status === 'done') {{
        clearInterval(_oppsPollTimer);
        document.getElementById('opps-loading').textContent = '✓ Done — loading results…';
        await loadOpportunities();
        document.getElementById('opps-loading').textContent = '';
        _resetOppsButton(btn);
      }} else if (data.status === 'partial_failure') {{
        clearInterval(_oppsPollTimer);
        const failed = (data.failed_lists || []).join(', ');
        document.getElementById('opps-loading').innerHTML =
          `<span style="color:{ACCENT_RED}">⚠ Scan completed with failures: ${{failed}}.</span> ` +
          `Loading partial results… Debug log: <code>${{data.scan_debug_log || ''}}</code>`;
        await loadOpportunities();
        _resetOppsButton(btn);
      }}
    }} catch(e) {{ /* server momentarily busy, keep polling */ }}
  }}, 15000);  // poll every 15 seconds
}}

// ================================================================ FILTERED TAB (unfiltered scan)
async function loadUnfiltered() {{
  document.getElementById('unf-loading').textContent = '⏳ Loading…';
  try {{
    const sr = await fetch(`${{SERVER}}/opportunities-unfiltered/status`);
    const sd = await sr.json();
    if (sd.status === 'running') {{
      const btn = document.getElementById('unf-run-btn');
      if (btn && !btn.disabled) {{
        btn.disabled = true;
        btn.dataset.origLabel = btn.dataset.origLabel || btn.textContent;
        btn.textContent = '⏳ Running…';
      }}
      ['unf-list1','unf-list2','unf-list3'].forEach(id => {{
        const el = document.getElementById(id);
        if (el) el.innerHTML = '<div class="opp-loading">⏳ Scan in progress…</div>';
      }});
      _startUnfilteredPoll(btn);
      return;
    }}
  }} catch(e) {{}}

  try {{
    const res  = await fetch(`${{SERVER}}/opportunities-unfiltered`);
    const data = await res.json();
    if (data.error) {{
      document.getElementById('unf-loading').textContent = 'No scan data yet — click ▶ Run No-Filter Scan.';
      ['unf-list1','unf-list2','unf-list3'].forEach(id => {{
        const el = document.getElementById(id);
        if (el) el.innerHTML = '<div class="opp-loading">No data — run a no-filter scan first.</div>';
      }});
      return;
    }}
    document.getElementById('unf-date').textContent = data.date ? 'Last scan: ' + data.date : '';
    _renderUnfilteredUniverse(data._universe_quality, data._excluded_from_main);
    renderOppList('unf-list1', data.list1_portfolio_fit);
    renderOppList('unf-list2', data.list2_profile_fit);
    renderOppList('unf-list3', data.list3_market_picks);
    document.getElementById('unf-loading').textContent = '';
  }} catch(e) {{
    document.getElementById('unf-loading').textContent = 'No scan data yet — click ▶ Run No-Filter Scan.';
  }}
}}

function _renderUnfilteredUniverse(uq, excluded) {{
  const el = document.getElementById('unf-universe');
  if (!el) return;
  if (!uq) {{ el.style.display = 'none'; return; }}
  const parabolic = (uq.parabolic_included || []).join(', ') || '(none)';
  const exCount   = (excluded || []).length;
  el.style.display = 'block';
  el.innerHTML =
    `<strong style="color:#aaa;">📡 Unfiltered universe</strong> — `
    + `<span style="color:#66cc99;">${{uq.clean_count || 0}} clean</span> · `
    + `<span style="color:#ffd699;">${{uq.extended_count || 0}} extended</span> · `
    + `<span style="color:#ff9999;">${{(uq.parabolic_included || []).length}} parabolic INCLUDED</span>`
    + ` <span style="color:#666;">(${{uq.data_as_of || 'n/a'}})</span>`
    + `<div style="margin-top:6px;color:#888;">Parabolic names eligible here: `
    + `<span style="color:#ff9999;">${{parabolic}}</span></div>`
    + `<div style="margin-top:4px;color:#888;">Excluding ${{exCount}} tickers already in 💡 Opportunities.</div>`;
}}

async function runUnfilteredScan() {{
  const btn = document.getElementById('unf-run-btn');
  btn.disabled = true;
  btn.dataset.origLabel = btn.dataset.origLabel || btn.textContent;
  btn.textContent = '⏳ Running…';
  document.getElementById('unf-loading').textContent = '⏳ Starting no-filter scan…';
  ['unf-list1','unf-list2','unf-list3'].forEach(id => {{
    const el = document.getElementById(id);
    if (el) el.innerHTML = '<div class="opp-loading">⏳ Queued — waiting for agent to start…</div>';
  }});
  let body = {{}};
  try {{
    const cr = await fetch(`${{SERVER}}/cash-ready`);
    const cd = await cr.json();
    if (cd && cd.amount_ils) body.cash_ils = Math.round(cd.amount_ils);
  }} catch(e) {{}}
  try {{
    const res  = await fetch(`${{SERVER}}/opportunities-unfiltered/run`, {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(body),
    }});
    const data = await res.json();
    if (data.error) {{
      document.getElementById('unf-loading').textContent = 'Error: ' + data.error;
      _resetUnfilteredButton(btn);
      return;
    }}
    if (data.started === false) {{
      document.getElementById('unf-loading').textContent =
        '⚠ ' + (data.message || 'Scan already running.');
    }} else {{
      const cashNote = body.cash_ils
        ? ` (cash ₪${{body.cash_ils.toLocaleString()}})`
        : ' (cash: server default)';
      document.getElementById('unf-loading').textContent =
        '⏳ No-filter scan running' + cashNote + ' — 5 picks per list, no parabolic gate (~5–10 min)…';
    }}
    _startUnfilteredPoll(btn);
  }} catch(e) {{
    document.getElementById('unf-loading').textContent = 'Failed: ' + e.message;
    _resetUnfilteredButton(btn);
  }}
}}

function _resetUnfilteredButton(btn) {{
  if (!btn) return;
  btn.disabled = false;
  btn.textContent = btn.dataset.origLabel || '▶ Run No-Filter Scan';
}}

let _unfPollTimer = null;
function _startUnfilteredPoll(btn) {{
  if (_unfPollTimer) clearInterval(_unfPollTimer);
  let elapsed = 0;
  _unfPollTimer = setInterval(async () => {{
    elapsed += 15;
    try {{
      const r  = await fetch(`${{SERVER}}/opportunities-unfiltered/status`);
      const d  = await r.json();
      if (d.status === 'running') {{
        document.getElementById('unf-loading').textContent =
          `⏳ No-filter scan running… (${{elapsed}}s elapsed)`;
      }} else if (d.status === 'done') {{
        clearInterval(_unfPollTimer);
        document.getElementById('unf-loading').textContent = '✓ Done — loading results…';
        await loadUnfiltered();
        document.getElementById('unf-loading').textContent = '';
        _resetUnfilteredButton(btn);
      }} else if (d.status === 'error') {{
        clearInterval(_unfPollTimer);
        document.getElementById('unf-loading').innerHTML =
          `<span style="color:{ACCENT_RED}">⚠ Scan failed: ${{d.error || 'unknown'}}</span>`;
        _resetUnfilteredButton(btn);
      }}
    }} catch(e) {{}}
  }}, 15000);
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
    _report_running: bool = False
    _report_started_at: str = ""
    _report_last_error: str = ""
    _improve_running: bool = False
    _improve_started_at: str = ""
    _improve_last_error: str = ""
    _unfiltered_running: bool = False
    _unfiltered_started_at: str = ""
    _unfiltered_last_error: str = ""

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
                # Reload positions on each request so cost-basis lookups reflect
                # the latest portfolio-update trades.
                _Handler.positions = _load_positions()
                payload = build_ticker_payload(symbol, _Handler.positions, period)
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/portfolio":
            try:
                # Reload positions.json on every request so trades recorded via
                # portfolio-update (which modifies the file on disk) are visible
                # without needing to restart the dashboard server.
                _Handler.positions = _load_positions()
                payload = build_portfolio_payload(_Handler.positions)
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/cash-ready":
            try:
                self._send_json(_cash_ready_payload())
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

        if parsed.path == "/opportunities-unfiltered":
            try:
                dirs = sorted(REPORT_DIR.iterdir(), reverse=True) if REPORT_DIR.exists() else []
                unf_path = None
                for d in dirs:
                    p = d / "opportunities-unfiltered.json"
                    if not p.exists():
                        continue
                    # Skip partial/running files — fall through to the last completed scan
                    try:
                        status = json.loads(p.read_text()).get("_status", "")
                        if status in ("running",) or status.startswith("phase1:"):
                            continue
                    except Exception:
                        continue
                    unf_path = p
                    break
                if unf_path:
                    self._send_json(json.loads(unf_path.read_text()))
                else:
                    self._send_json({"error": "No unfiltered scan yet."})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/opportunities-unfiltered/status":
            today    = dt.date.today().isoformat()
            unf_path = REPORT_DIR / today / "opportunities-unfiltered.json"
            if _Handler._unfiltered_running:
                self._send_json({"status": "running",
                                 "started_at": _Handler._unfiltered_started_at})
            elif _Handler._unfiltered_last_error:
                self._send_json({"status": "error",
                                 "error": _Handler._unfiltered_last_error})
            elif unf_path.exists():
                # Only report "done" if the file is actually complete
                try:
                    file_status = json.loads(unf_path.read_text()).get("_status", "")
                    if file_status == "done" or file_status == "done:phase1_only":
                        self._send_json({"status": "done"})
                    else:
                        # Partial file on disk (crashed or interrupted) — treat as idle
                        self._send_json({"status": "idle"})
                except Exception:
                    self._send_json({"status": "idle"})
            else:
                self._send_json({"status": "idle"})
            return

        if parsed.path == "/report":
            try:
                md = _latest_report_md()
                if md:
                    html = _md_to_html(md)
                    m = re.search(r"# Daily Report — (\S+)", md)
                    date = m.group(1) if m else ""
                    self._send_json({"html": html, "date": date})
                else:
                    self._send_json({"error": "No report found. Run the daily report first."})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/report/status":
            today = dt.date.today().isoformat()
            today_report = REPORT_DIR / today / "report.md"
            if _Handler._report_running:
                self._send_json({
                    "status": "running",
                    "started_at": _Handler._report_started_at,
                })
            elif today_report.exists() and today_report.stat().st_size > 0:
                self._send_json({
                    "status": "done",
                    "date": today,
                    "error": _Handler._report_last_error or None,
                })
            elif _Handler._report_last_error:
                # Subprocess ran but no report.md was produced — surface the captured error
                # rather than going silently back to "idle".
                self._send_json({
                    "status": "error",
                    "error": _Handler._report_last_error,
                })
            else:
                self._send_json({
                    "status": "idle",
                    "error": None,
                })
            return

        if parsed.path == "/report/improve/status":
            today = dt.date.today().isoformat()
            improvements = REPORT_DIR / today / "report-improvements.json"
            if _Handler._improve_running:
                self._send_json({
                    "status": "running",
                    "started_at": _Handler._improve_started_at,
                })
            elif improvements.exists() and improvements.stat().st_size > 0:
                self._send_json({
                    "status": "done",
                    "date": today,
                    "error": _Handler._improve_last_error or None,
                })
            elif _Handler._improve_last_error:
                self._send_json({
                    "status": "error",
                    "error": _Handler._improve_last_error,
                })
            else:
                self._send_json({"status": "idle", "error": None})
            return

        if parsed.path == "/report/improve/result":
            try:
                # Find the most recent report-improvements.json (today preferred, else newest)
                dirs = sorted(REPORT_DIR.iterdir(), reverse=True) if REPORT_DIR.exists() else []
                found = None
                for d in dirs:
                    if not d.is_dir():
                        continue
                    p = d / "report-improvements.json"
                    if p.exists() and p.stat().st_size > 0:
                        found = p
                        break
                if not found:
                    self._send_json({"error": "No improvement proposals yet. Click ✨ Suggest improvements."})
                    return
                data = json.loads(found.read_text())
                data["_source_path"] = str(found.relative_to(ROOT))
                self._send_json(data)
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
                # Don't blindly report "done" — if any list contains only error sentinels
                # (PARSE_ERROR / TIMEOUT / ERROR) the scan silently failed for that list.
                # Surface it so the UI doesn't paint a green check on a broken scan.
                failed_lists: list[str] = []
                try:
                    data = json.loads(opp_path.read_text())
                    for k in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
                        items = data.get(k, [])
                        if not items:
                            failed_lists.append(f"{k}:empty")
                            continue
                        error_tickers = {"ERROR", "TIMEOUT", "PARSE_ERROR"}
                        real_picks = [
                            i for i in items
                            if (i.get("ticker") or "").upper() not in error_tickers
                        ]
                        if not real_picks:
                            # Surface the first sentinel's reason if available.
                            why = items[0].get("error") or items[0].get("ticker") or "unknown"
                            failed_lists.append(f"{k}:{why}")
                except Exception as e:
                    failed_lists.append(f"parse:{e}")
                if failed_lists:
                    self._send_json({
                        "status": "partial_failure",
                        "failed_lists": failed_lists,
                        "scan_debug_log": str((REPORT_DIR / today / "scan-debug.log").relative_to(ROOT)),
                    })
                else:
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

        if parsed.path == "/cash-ready":
            try:
                body    = self._read_body()
                payload = json.loads(body) if body else {}
                raw     = payload.get("amount_usd")
                if raw is None or raw == "":
                    _save_cash_ready(None)
                else:
                    amount = float(raw)
                    if amount < 0:
                        self._send_json({"error": "amount must be >= 0"}, 400)
                        return
                    _save_cash_ready(amount)
                self._send_json(_cash_ready_payload())
            except (ValueError, TypeError) as e:
                self._send_json({"error": f"invalid amount: {e}"}, 400)
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
                # If the caller omits cash_ils, let scan.py auto-resolve it from
                # positions.json.cash_ready_usd × FX. Only pass cash_ils when the
                # caller explicitly sent it.
                cash_ils = int(params["cash_ils"]) if "cash_ils" in params else None
                # Start background scan, return immediately
                if _Handler._scan_running:
                    self._send_json({"started": False, "message": "Scan already running"})
                    return
                _Handler._scan_running = True
                # Allow the caller to opt back into the fast Phase-1-only path.
                # Default is the full pipeline (Phase 1 + Phase 2 deep analysis).
                phase1_only = bool(params.get("phase1_only", False))
                def _bg_scan():
                    try:
                        today = dt.date.today().isoformat()
                        scan_payload = {"date": today, "phase1_only": phase1_only}
                        if cash_ils is not None:
                            scan_payload["cash_ils"] = cash_ils
                        scan_input = json.dumps(scan_payload)
                        # Phase 2 runs the bull/risk/manager triad on ~30 tickers
                        # in batches of 3 → up to ~40 min wall-time. Give it 60.
                        proc = subprocess.run(
                            ["python3",
                             str(ROOT / ".claude/skills/opportunity-scanner/scripts/scan.py")],
                            input=scan_input, capture_output=True, text=True,
                            timeout=3600, cwd=str(ROOT),
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

        if parsed.path == "/opportunities-unfiltered/run":
            try:
                body     = self._read_body()
                params   = json.loads(body) if body else {}
                cash_ils = int(params["cash_ils"]) if "cash_ils" in params else None
                if _Handler._unfiltered_running:
                    self._send_json({"started": False, "message": "Unfiltered scan already running"})
                    return
                _Handler._unfiltered_running   = True
                _Handler._unfiltered_started_at = dt.datetime.now().isoformat(timespec="seconds")
                _Handler._unfiltered_last_error = ""

                def _bg_unf():
                    try:
                        today = dt.date.today().isoformat()
                        payload = {"date": today}
                        if cash_ils is not None:
                            payload["cash_ils"] = cash_ils
                        proc = subprocess.run(
                            ["python3",
                             str(ROOT / ".claude/skills/opportunity-scanner/scripts/scan_unfiltered.py")],
                            input=json.dumps(payload), capture_output=True, text=True,
                            timeout=1800, cwd=str(ROOT),
                        )
                        if proc.returncode != 0:
                            _Handler._unfiltered_last_error = (
                                f"exit {proc.returncode}: "
                                + (proc.stderr or proc.stdout or "no output")
                            )[:1000]
                        elif proc.stdout.strip():
                            json.loads(proc.stdout.strip())  # validate
                    except subprocess.TimeoutExpired:
                        _Handler._unfiltered_last_error = "Unfiltered scan timed out after 30 min"
                    except Exception as e:
                        _Handler._unfiltered_last_error = str(e)
                    finally:
                        _Handler._unfiltered_running = False

                threading.Thread(target=_bg_unf, daemon=True).start()
                self._send_json({"started": True, "message": "Unfiltered scan started"})
            except Exception as e:
                _Handler._unfiltered_running = False
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/report/run":
            try:
                if _Handler._report_running:
                    self._send_json({"started": False, "message": "Report already running"})
                    return
                _Handler._report_running = True
                _Handler._report_started_at = dt.datetime.now().isoformat(timespec="seconds")
                # Clear any stale error from a previous run before starting fresh.
                _Handler._report_last_error = ""
                # If today's report already exists from a prior run, move it aside so the
                # new run is observable — otherwise stale-success would mask a fresh failure.
                today_str = dt.date.today().isoformat()
                stale     = REPORT_DIR / today_str / "report.md"
                if stale.exists():
                    try:
                        stale.rename(stale.with_name(
                            f"report.prev-{dt.datetime.now().strftime('%H%M%S')}.md"
                        ))
                    except Exception:
                        pass

                def _bg_report():
                    today_str    = dt.date.today().isoformat()
                    expected_md  = REPORT_DIR / today_str / "report.md"
                    # Tee subprocess stdout/stderr to a log file so the user can inspect
                    # what the agent actually did when something goes wrong.
                    log_dir      = REPORT_DIR / today_str
                    log_dir.mkdir(parents=True, exist_ok=True)
                    run_log      = log_dir / "daily-report-run.log"
                    try:
                        cmd = ["claude", "--dangerously-skip-permissions",
                               "-p", "/daily-report",
                               "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep,Task,WebSearch,WebFetch"]
                        proc = subprocess.run(
                            cmd, capture_output=True, text=True,
                            timeout=1800, cwd=str(ROOT),
                        )
                        try:
                            run_log.write_text(
                                f"# /daily-report run @ {dt.datetime.now().isoformat(timespec='seconds')}\n"
                                f"## returncode\n{proc.returncode}\n\n"
                                f"## stderr\n{proc.stderr}\n\n"
                                f"## stdout (last 4000 chars)\n{proc.stdout[-4000:]}\n"
                            )
                        except Exception:
                            pass
                        if proc.returncode != 0:
                            _Handler._report_last_error = (
                                f"exit {proc.returncode}: "
                                + (proc.stderr or proc.stdout or "no output")
                            )[:1000]
                            return
                        # The agent claimed success but no report.md was produced — this is
                        # the silent-failure mode we used to swallow. Surface it loudly.
                        if not (expected_md.exists() and expected_md.stat().st_size > 0):
                            _Handler._report_last_error = (
                                "Agent finished with exit 0 but did not write "
                                f"{expected_md.relative_to(ROOT)}. "
                                f"Check {run_log.relative_to(ROOT)} for what the agent did."
                            )
                    except subprocess.TimeoutExpired:
                        _Handler._report_last_error = "Report generation timed out after 30 min"
                    except FileNotFoundError:
                        _Handler._report_last_error = "claude CLI not found in PATH"
                    except Exception as e:
                        _Handler._report_last_error = str(e)
                    finally:
                        _Handler._report_running = False

                threading.Thread(target=_bg_report, daemon=True).start()
                self._send_json({"started": True, "message": "Report generation started"})
            except Exception as e:
                _Handler._report_running = False
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/report/improve/run":
            try:
                if _Handler._improve_running:
                    self._send_json({"started": False, "message": "Improvement chain already running"})
                    return
                _Handler._improve_running = True
                _Handler._improve_started_at = dt.datetime.now().isoformat(timespec="seconds")
                _Handler._improve_last_error = ""

                def _bg_improve():
                    today_str = dt.date.today().isoformat()
                    log_dir   = REPORT_DIR / today_str
                    log_dir.mkdir(parents=True, exist_ok=True)
                    run_log   = log_dir / "improve-report-run.log"
                    expected  = log_dir / "report-improvements.json"
                    try:
                        cmd = ["claude", "--dangerously-skip-permissions",
                               "-p", "/improve-report",
                               "--allowedTools", "Read,Write,Bash,Glob,Grep,Task"]
                        proc = subprocess.run(
                            cmd, capture_output=True, text=True,
                            timeout=1800, cwd=str(ROOT),
                        )
                        try:
                            run_log.write_text(
                                f"# /improve-report run @ {dt.datetime.now().isoformat(timespec='seconds')}\n"
                                f"## returncode\n{proc.returncode}\n\n"
                                f"## stderr\n{proc.stderr}\n\n"
                                f"## stdout (last 4000 chars)\n{proc.stdout[-4000:]}\n"
                            )
                        except Exception:
                            pass
                        if proc.returncode != 0:
                            _Handler._improve_last_error = (
                                f"exit {proc.returncode}: "
                                + (proc.stderr or proc.stdout or "no output")
                            )[:1000]
                            return
                        if not (expected.exists() and expected.stat().st_size > 0):
                            _Handler._improve_last_error = (
                                "Agent reported success but report-improvements.json "
                                f"was not written to {expected}. See {run_log} for details."
                            )
                    except subprocess.TimeoutExpired:
                        _Handler._improve_last_error = "improve-report timed out after 30 min"
                    except FileNotFoundError:
                        _Handler._improve_last_error = "claude CLI not found in PATH"
                    except Exception as e:
                        _Handler._improve_last_error = str(e)
                    finally:
                        _Handler._improve_running = False

                threading.Thread(target=_bg_improve, daemon=True).start()
                self._send_json({"started": True, "message": "Improvement chain started"})
            except Exception as e:
                _Handler._improve_running = False
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
