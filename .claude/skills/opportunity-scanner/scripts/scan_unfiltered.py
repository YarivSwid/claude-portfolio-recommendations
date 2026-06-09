"""opportunity-scanner — UNFILTERED variant.

Runs the same 3-list Phase-1 scan as scan.py, but:
  - Does NOT pre-filter parabolic momentum names (RSI≥80, >50% above 200DMA,
    10+ up-day streaks all stay in the LLM universe).
  - Does NOT reject weak-evidence picks post-hoc (every pick the agent returns
    is preserved, even if it has 0 citations or a non-numeric bear threshold).
  - Targets 5 picks per list (the user wants a focused "what did the filters
    hide from me?" view, not another 30-name dump).
  - Writes to research/daily/<today>/opportunities-unfiltered.json so the
    main /opportunities scan output is untouched.

Phase 2 (bull/risk/manager deep-dive) is NOT run from here. Each pick still
gets a Deep-Analysis button in the dashboard which routes to the existing
/deep-analysis endpoint.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3].parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Reuse the heavy lifters from scan.py so we never drift on prompt content,
# universe block formatting, or screener behaviour.
import scan  # noqa: E402


def _format_full_universe_block(universe: dict, max_total: int = 90) -> str:
    """Render the FULL universe (clean + extended + parabolic) as one block.

    The original _format_universe_block splits clean/extended into two pools and
    drops parabolic entirely. Here we surface everything in one labelled list so
    the LLM sees the parabolic names with their momentum flags and can decide
    whether to pick them anyway. The momentum-check fields (rally_pct_30d,
    price_vs_200dma) come along so the agent can write an honest entry_note.
    """
    cands_by_t = universe.get("candidates_by_ticker", {})
    metrics    = universe.get("metrics", {})
    rows: list[str] = []
    pools = (
        ("CLEAN", universe.get("clean", [])),
        ("EXTENDED", universe.get("extended", [])),
        ("PARABOLIC", universe.get("parabolic_dropped", [])),
    )
    total = 0
    for label, pool in pools:
        for t in pool:
            if total >= max_total:
                break
            c = cands_by_t.get(t, {})
            m = metrics.get(t, {})
            fwd_pe   = c.get("fwd_pe")
            rev_g    = c.get("rev_growth_yoy")
            gm       = c.get("gross_margin")
            score    = c.get("score")
            rsi      = m.get("rsi14")
            vs_200   = m.get("pct_vs_200dma")
            rally    = m.get("rally_pct_30d") or m.get("pct_change_30d")
            streak   = m.get("consecutive_up_days")
            flags    = ",".join(m.get("flags", []) or []) or "none"

            def fpct(v: float | None) -> str:
                return "n/a" if v is None else f"{v*100:+.0f}%"

            def fnum(v: float | None, prec: int = 1) -> str:
                return "n/a" if v is None else f"{v:.{prec}f}"

            rows.append(
                f"  {t:6} [{label}] fwd_pe={fnum(fwd_pe)} rev_g={fpct(rev_g)} "
                f"gm={fpct(gm)} score={fnum(score, 2)} | "
                f"rsi={fnum(rsi, 0)} vs200={fpct(vs_200)} rally30d={fpct(rally)} "
                f"streak={streak if streak is not None else 'n/a'} flags={flags}"
            )
            total += 1
    return (
        "FULL CANDIDATE UNIVERSE (clean + extended + parabolic combined — "
        "NO momentum gating in this scan):\n"
        + "\n".join(rows)
        + "\n\nNote: parabolic names carry their momentum flags so you can size "
        "with eyes open. Use them when you have a specific contrarian or "
        "catalyst-driven reason — never just because the score is high."
    )


_UNFILTERED_OUTPUT_FORMAT = """
Return ONLY a valid JSON array of EXACTLY 5 items. Every item must include
inline citations in bull_thesis and a numeric bear_threshold, BUT picks with
weaker evidence are kept (not rejected) so the user can see what the standard
filter would have dropped. No markdown, no explanation outside the JSON.
RESPONSE MUST start with `[` and end with `]`. No preamble.
Each item must have these exact keys:
{
  "ticker": "AMD",
  "name": "Advanced Micro Devices",
  "type": "stock",
  "fit": "exceptional",
  "max_invest_ils": 18000,
  "signal": "BUY",
  "when_to_buy": "now — or wait for AI-PC catalyst",
  "cooling_trigger": null,
  "rally_pct_30d": "n/a (overwritten by yfinance)",
  "price_vs_200dma": "n/a",
  "entry_note": "extended but contrarian-defensible — see filter_override_reason",
  "reason_fits": "one sentence why this fits despite the filter",
  "bull_thesis": "2-3 sentences with at least one (Source — YYYY-MM-DD) citation",
  "bear_threshold": "If <metric> falls below/exceeds <value>, thesis breaks",
  "catalyst": {"type": "earnings|product|regulatory|none", "date": "YYYY-MM-DD or null", "what_to_watch": "one line"},
  "filter_override_reason": "one sentence on why this pick is worth surfacing even though it was filtered (e.g. 'parabolic-flagged but pre-AI-PC launch')",
  "data_as_of": "YYYY-MM-DD"
}
SIGNAL VOCABULARY (4 values, same as the main scan):
- "STRONG_BUY" — clean entry, exceptional thesis. cooling_trigger must be null.
- "BUY" — solid thesis. cooling_trigger must be null.
- "WAIT-FOR-COOLING" — extended/parabolic momentum. Required: numeric
  cooling_trigger (e.g. "RSI <60 for 3 days" or "pullback to vs200 <0.20").
- "WAIT-FOR-CATALYST" — dated event within 14 days. catalyst.date populated.
DO NOT use HOLD, AVOID, SELL, WATCH.
SORT by signal strength: STRONG_BUY → BUY → WAIT-FOR-CATALYST → WAIT-FOR-COOLING.
"""


def _build_list1(positions, cash_ils, today, exclude, universe_block, portfolio_context):
    held = ", ".join(
        p.get("yf_symbol") or p.get("symbol", "")
        for p in positions if p.get("currency") == "USD"
    )
    excl = (f"\nCRITICAL: Do NOT include any of these tickers (already in the main "
            f"opportunities scan or another unfiltered list): {', '.join(exclude)}. "
            f"Pick DIFFERENT names — this is the alternate view.\n") if exclude else ""
    return f"""Today is {today}. You are an investment analyst running the UNFILTERED variant.

Task: recommend EXACTLY 5 stocks or ETFs that complement this portfolio AND that the standard parabolic/weak-evidence filter would have hidden. Every pick must still be defensible — back each one with a specific override reason in filter_override_reason.

Current US holdings: {held}

{scan._profile_summary()}

{portfolio_context}

{universe_block}

TOTAL cash available: ₪{cash_ils:,} (~${cash_ils//3:,} USD). Calibrate sizing accordingly.
Max per position: ₪{min(cash_ils // 5, 25000):,}.
{excl}
Use WebSearch to fill bull_thesis with at least one (Source — YYYY-MM-DD) citation and to find concrete numeric bear_threshold metrics. Anchor fundamentals to the universe block above.

{_UNFILTERED_OUTPUT_FORMAT}"""


def _build_list2(cash_ils, today, exclude, universe_block):
    excl = (f"\nCRITICAL: Do NOT include any of these tickers: "
            f"{', '.join(exclude)}. Pick DIFFERENT names.\n") if exclude else ""
    return f"""Today is {today}. You are an investment analyst running the UNFILTERED variant.

Task: recommend EXACTLY 5 stocks or ETFs for this investor profile that the standard parabolic/weak-evidence filter would have hidden. Treat as a fresh portfolio — ignore existing holdings. Every pick must be defensible with a filter_override_reason.

{scan._profile_summary()}

{universe_block}

TOTAL Cash: ₪{cash_ils:,} (~${cash_ils//3:,} USD). Calibrate to the profile above.
Max per position: ₪{min(cash_ils // 5, 25000):,}.
{excl}
Use WebSearch for citations and numeric bear_threshold metrics.

{_UNFILTERED_OUTPUT_FORMAT}"""


def _build_list3(today, cash_ils, exclude, universe_block):
    excl = (f"\nCRITICAL: Do NOT include any of these tickers: "
            f"{', '.join(exclude)}. Pick DIFFERENT names.\n") if exclude else ""
    return f"""Today is {today}. You are an investment analyst running the UNFILTERED variant.

Task: recommend EXACTLY 5 stocks or ETFs that are the best market opportunities right now AND that the standard parabolic filter would have hidden. You know nothing about the investor. Include at least 2 non-US or non-tech names. Every pick must have a filter_override_reason.

{universe_block}

TOTAL Cash budget: ₪{cash_ils:,}. Max per position: ₪{min(cash_ils // 5, 25000):,}.
{excl}
Use WebSearch for citations and numeric bear_threshold metrics.

{_UNFILTERED_OUTPUT_FORMAT}"""


def main() -> None:
    try:
        raw    = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    # Cash resolution mirrors scan.py
    if "cash_ils" in params:
        cash_ils    = int(params["cash_ils"])
        cash_source = "params.cash_ils"
    else:
        cash_usd = scan._load_cash_ready_usd()
        rate     = scan._fetch_usdils() if cash_usd is not None else None
        if cash_usd is not None and rate:
            cash_ils    = int(cash_usd * rate)
            cash_source = f"positions.cash_ready_usd ({cash_usd:.0f} USD × {rate:.4f})"
        else:
            cash_ils    = 120000
            cash_source = "default"
    print(f"[scan_unfiltered] cash_ils={cash_ils:,} from {cash_source}", file=sys.stderr)

    today = params.get("date", dt.date.today().isoformat())
    out_dir      = ROOT / "research" / "daily" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path     = out_dir / "opportunities-unfiltered.json"
    partial_path = out_dir / "opportunities-unfiltered.partial.json"

    positions = scan._load_positions()

    # Build the FULL universe (no parabolic drop).
    print("Building FULL candidate universe (no parabolic filter)…", file=sys.stderr)
    screener_top   = scan._run_screener_top(top_n=50)
    universe       = scan._prefilter_universe(screener_top)  # still classifies, just for labels
    universe_block = _format_full_universe_block(universe, max_total=90)

    # Read the main scan's output (if it exists today) so the unfiltered scan
    # excludes those tickers. The whole point of this view is to surface what
    # the main scan would NOT have shown.
    exclude_main: list[str] = []
    main_path = out_dir / "opportunities.json"
    if main_path.exists():
        try:
            md = json.loads(main_path.read_text())
            for k in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
                for it in md.get(k, []):
                    t = (it.get("ticker") or "").upper()
                    if t and t not in ("ERROR", "TIMEOUT", "PARSE_ERROR"):
                        exclude_main.append(t)
        except Exception:
            pass
    print(f"Excluding {len(exclude_main)} tickers already in main /opportunities scan.",
          file=sys.stderr)

    # Portfolio context (same source as main scan).
    portfolio_context = ""
    try:
        import subprocess
        sa_proc = subprocess.run(
            ["python3", str(ROOT / ".claude" / "skills" / "sector-allocation" / "scripts" / "sectors.py")],
            input="{}", capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        )
        if sa_proc.returncode == 0:
            sa_data = json.loads(sa_proc.stdout)
            by_sector = sa_data.get("by_sector", {})
            top = sorted(by_sector.items(), key=lambda kv: -kv[1])[:5]
            portfolio_context = (
                f"PORTFOLIO FACTOR WEIGHTS (data_as_of {sa_data.get('data_as_of')}): "
                + ", ".join(f"{k} {v*100:.0f}%" for k, v in top)
                + ". Use this only as background — the unfiltered scan's job is to "
                  "surface names the main scan would have hidden."
            )
    except Exception:
        pass

    output: dict = {
        "date":      today,
        "cash_ils":  cash_ils,
        "variant":   "unfiltered",
        "list1_portfolio_fit": [],
        "list2_profile_fit":   [],
        "list3_market_picks":  [],
        "_status":   "running",
        "_universe_quality": {
            "data_as_of":         today,
            "screener_count":     len(screener_top),
            "clean_count":        len(universe.get("clean", [])),
            "extended_count":     len(universe.get("extended", [])),
            "parabolic_included": universe.get("parabolic_dropped", []),
        },
        "_excluded_from_main": exclude_main,
    }

    def _write_partial(status: str) -> None:
        partial = dict(output)
        partial["_status"] = status
        partial_path.write_text(json.dumps(partial, ensure_ascii=False, indent=2))

    seen: list[str] = list(exclude_main)

    # ---------------------------------------------------------------- List 1
    _write_partial("phase1:list1_portfolio_fit")
    output["list1_portfolio_fit"] = scan._run_claude_json(
        _build_list1(positions, cash_ils, today, seen, universe_block, portfolio_context),
        "unfiltered:list1_portfolio_fit",
        max_turns=30, retries=1,
    )
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    seen.extend(
        it.get("ticker", "").upper()
        for it in output["list1_portfolio_fit"]
        if it.get("ticker") and it.get("ticker") not in ("ERROR", "TIMEOUT", "PARSE_ERROR")
    )

    # ---------------------------------------------------------------- List 2
    _write_partial("phase1:list2_profile_fit")
    output["list2_profile_fit"] = scan._run_claude_json(
        _build_list2(cash_ils, today, seen, universe_block),
        "unfiltered:list2_profile_fit",
        max_turns=30, retries=1,
    )
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    seen.extend(
        it.get("ticker", "").upper()
        for it in output["list2_profile_fit"]
        if it.get("ticker") and it.get("ticker") not in ("ERROR", "TIMEOUT", "PARSE_ERROR")
    )

    # ---------------------------------------------------------------- List 3
    _write_partial("phase1:list3_market_picks")
    output["list3_market_picks"] = scan._run_claude_json(
        _build_list3(today, cash_ils, seen, universe_block),
        "unfiltered:list3_market_picks",
        max_turns=30, retries=1,
    )

    # ---------------------------------------------------------------- Annotate
    # Apply momentum-check post-processing for accurate rally/vs200 numbers and
    # signal split. SKIP _apply_evidence_strength_to_list — the whole point of
    # this scan is to keep weak rows visible.
    for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
        if scan._is_valid(output[key]):
            output[key] = scan._apply_momentum_check_to_list(output[key])
            # Annotate evidence_strength so the UI can label rows, but DO NOT drop.
            for it in output[key]:
                if it.get("ticker") in ("ERROR", "TIMEOUT", "PARSE_ERROR"):
                    continue
                it["evidence_strength"] = scan._score_evidence_strength(it)

    # Same-list dedup (still useful — same ticker proposed twice in one list).
    SIG = {"STRONG_BUY": 4, "BUY": 3, "WAIT-FOR-CATALYST": 2, "WAIT-FOR-COOLING": 1}
    EVID = {"strong": 3, "moderate": 2, "weak": 1}
    def _key(it: dict) -> tuple:
        return (
            SIG.get((it.get("signal") or "").upper(), 0),
            EVID.get((it.get("evidence_strength") or "").lower(), 0),
            int(it.get("max_invest_ils") or 0),
        )
    for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
        best: dict[str, dict] = {}
        order: list[str] = []
        for it in output[key]:
            t = (it.get("ticker") or "").upper()
            if not t or t in ("ERROR", "TIMEOUT", "PARSE_ERROR"):
                order.append(f"__err__{len(order)}")
                best[order[-1]] = it
                continue
            if t in best:
                if _key(it) > _key(best[t]):
                    best[t] = it
            else:
                best[t] = it
                order.append(t)
        output[key] = [best[x] for x in order if x in best]

    output["_status"] = "done"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    if partial_path.exists():
        partial_path.unlink()

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
