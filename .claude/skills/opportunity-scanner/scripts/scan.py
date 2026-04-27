#!/usr/bin/env python3
"""opportunity-scanner — Phase 1: fast screener lists. Phase 2: 3-agent deep analysis.

Phase 1: generates 3 ranked candidate lists (portfolio-fit / profile-fit / market-picks)
         using a fast screener prompt with WebSearch. ~5 min, ~$2-4 on Sonnet.
Phase 2: runs the full bull-officer + risk-officer + portfolio-manager pipeline on each
         unique ticker across all 3 lists. Batches of 3 concurrent tickers. ~15-25 min,
         ~$30-50 on Sonnet (~$150-200 on Opus). Skips tickers already cached in
         research/signals/<TICKER>_<date>.json.

DEFAULT: Phase 1 only. Phase 2 is opt-in via phase1_only=false because deep-diving
all 25 candidates per scan is overkill when you typically deploy on 1-3 names. Use
the per-ticker bull/risk/manager pipeline directly (via /should-I-buy or chat) for
the deep-dive on the names you actually consider.

Input (stdin JSON):
  Cheap default (Phase 1 only):
    { "cash_ils": 120000, "date": "2026-04-22" }
  Full deep-dive (only when you have real cash to deploy):
    { "cash_ils": 120000, "date": "2026-04-22", "phase1_only": false }

Output: writes research/daily/<date>/opportunities.json, returns it on stdout.
"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT           = Path(__file__).resolve().parents[4]
POSITIONS_PATH = ROOT / "portfolio" / "positions.json"
SIGNALS_DIR    = ROOT / "research" / "signals"
AGENTS_DIR     = ROOT / ".claude" / "agents"

PROMPT_VERSION = "v4"  # bump to invalidate all signal caches


OUTPUT_FORMAT = """
Return ONLY a valid JSON array of exactly 10 items. No markdown, no explanation outside the JSON.
Each item must have these exact keys:
{
  "ticker": "NVDA",
  "name": "NVIDIA Corporation",
  "type": "stock",
  "fit": "exceptional",
  "max_invest_ils": 18000,
  "signal": "BUY",
  "when_to_buy": "now — no near-term catalyst risk",
  "rally_pct_30d": "+18% (from ~$170 on Mar 25 to ~$201 on Apr 25)",
  "price_vs_200dma": "+12% above 200DMA (~$180)",
  "entry_note": "modest extension, not parabolic — reasonable entry",
  "reason_fits": "one sentence why this fits",
  "bull_point": "one sentence strongest bull case with source if available",
  "risk_point": "one sentence key risk",
  "data_as_of": "YYYY-MM-DD"
}
CRITICAL RULES:
- All 10 items MUST be names you would CONSIDER BUYING. Do NOT include stocks to avoid, hold, or skip.
- For "signal" use ONLY: STRONG_BUY / BUY / WATCH.
  - STRONG_BUY = exceptional thesis, high conviction, buy now.
  - BUY = solid thesis, buy now or on minor dips.
  - WATCH = good thesis but a specific near-term trigger is pending (earnings in <7 days, awaiting a catalyst, better entry expected soon). The "when_to_buy" field MUST explain exactly when/what to wait for.
- Do NOT use HOLD, AVOID, SELL, or any other signal. If a name doesn't qualify as STRONG_BUY/BUY/WATCH, replace it with one that does.
- For "fit" use one of: exceptional / good / moderate.
- For "when_to_buy": if signal is STRONG_BUY or BUY, write "now" or a brief entry note. If WATCH, write the specific trigger (e.g. "after Apr 29 earnings print", "on pullback below $X", "after Q1 revenue confirms").
- For "rally_pct_30d": use WebSearch to find the approximate price ~30 days ago and today. Write the % change and the two reference prices (e.g. "+44% (from ~$293 on Mar 26 to ~$423 on Apr 25)"). If unavailable write "n/a".
- For "price_vs_200dma": use WebSearch to find the current 200-day moving average. Write how far above or below current price is (e.g. "+25% above 200DMA (~$338)" or "-8% below 200DMA (~$210)"). If unavailable write "n/a". WARNING: names more than +40% above their 200DMA are momentum-extended — prefer WATCH over BUY for these unless thesis is exceptional.
- For "entry_note": one plain-English sentence on whether the entry is clean, extended, near highs, near lows, or has a better entry condition. Examples: "near 52-week high after 44% rally — consider phasing", "recovering from trough, 15% below highs — clean entry", "parabolic — wait for consolidation".
- If a stock reports earnings within 7 days, use WATCH and explain in when_to_buy.
- Sort by signal strength first (STRONG_BUY → BUY → WATCH), then by fit quality within each tier.
"""


KNOWN_ETF_FAMILIES: dict[str, str] = {
    "SPY": "sp500", "VOO": "sp500", "IVV": "sp500",
    "QQQ": "nasdaq100", "QQQM": "nasdaq100",
    "SMH": "semis", "SOXX": "semis",
    "VGT": "ustech", "XLK": "ustech",
    "IEFA": "intl_dev", "EFA": "intl_dev", "VXUS": "intl_broad",
    "EEM": "em", "IEMG": "em", "VWO": "em",
    "EWJ": "japan", "DXJ": "japan",
    "VGK": "europe", "EZU": "europe", "FEZ": "europe",
    "XLF": "financials", "VFH": "financials",
    "XLV": "healthcare", "VHT": "healthcare",
    "XLE": "energy", "VDE": "energy",
    "XLI": "industrials", "VIS": "industrials",
    "IGV": "software", "WCLD": "software",
    "ARKK": "disruptive", "ARKW": "disruptive",
    "IWM": "smallcap_us", "VTWO": "smallcap_us",
    "TLT": "longbond", "VGLT": "longbond",
    "GLD": "gold", "IAU": "gold", "SGOL": "gold",
    "IBIT": "bitcoin", "FBTC": "bitcoin", "GBTC": "bitcoin",
}


def _detect_etf_overlaps(output: dict) -> list[str]:
    """Detect ETFs across all lists that track the same theme/index.
    Uses a known-family lookup (fast, no network) and flags overlaps.
    """
    etf_locations: dict[str, list[tuple[str, str]]] = {}
    for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
        for item in output.get(key, []):
            t = item.get("ticker", "").upper()
            tp = (item.get("type") or "").lower()
            if tp in ("etf", "etfs") or t in KNOWN_ETF_FAMILIES:
                family = KNOWN_ETF_FAMILIES.get(t)
                if family:
                    etf_locations.setdefault(family, []).append((t, key))

    warnings = []
    for family, entries in etf_locations.items():
        tickers = list(dict.fromkeys(e[0] for e in entries))
        if len(tickers) > 1:
            lists_involved = list(dict.fromkeys(e[1] for e in entries))
            warnings.append(
                f"Overlapping ETFs in '{family}' theme: {', '.join(tickers)} "
                f"(in {', '.join(lists_involved)}). These track the same underlying — keep only one."
            )
    return warnings


def _load_positions() -> list[dict]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        data = json.loads(POSITIONS_PATH.read_text())
        return data if isinstance(data, list) else data.get("positions", [])
    except Exception:
        return []


def _load_agent_spec(name: str) -> str:
    path = AGENTS_DIR / f"{name}.md"
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
    path = ROOT / "research" / "user-views.md"
    if not path.exists():
        return ""
    lines = path.read_text().splitlines()
    return "\n".join(lines[:80])


def _profile_summary() -> str:
    """One-line profile string injected into scanner prompts.
    Reads research/user-views.md if present; falls back to a generic medium-risk default.
    Pulls the Profile section verbatim so the prompt stays grounded in what the user wrote.
    """
    path = ROOT / "research" / "user-views.md"
    if not path.exists():
        return ("Investor profile not specified — assume MEDIUM risk, 10-year horizon, "
                "balanced growth/diversification, no concentrated single-name conviction. "
                "(Add research/user-views.md for personalized calibration.)")
    text = path.read_text()
    # Extract the "## Profile" block (until the next "## " heading)
    in_profile = False
    profile_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## Profile"):
            in_profile = True
            continue
        if in_profile and line.startswith("## "):
            break
        if in_profile and line.strip():
            profile_lines.append(line.strip())
    if not profile_lines:
        return ("Investor profile section missing in user-views.md — "
                "assume MEDIUM risk, 10-year horizon as fallback.")
    return "Investor profile (from user-views.md):\n" + "\n".join(profile_lines)


def _signal_path(ticker: str, date: str) -> Path:
    return SIGNALS_DIR / f"{ticker}_{date}.json"


def _load_signal_cache(ticker: str, date: str) -> dict | None:
    p = _signal_path(ticker, date)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        if data.get("prompt_version") != PROMPT_VERSION:
            return None
        return data
    except Exception:
        return None


def _save_signal_cache(ticker: str, date: str, result: dict) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    result = dict(result)
    result["prompt_version"] = PROMPT_VERSION
    result["cached_at"] = dt.datetime.now().isoformat(timespec="seconds")
    _signal_path(ticker, date).write_text(json.dumps(result, ensure_ascii=False, indent=2))


def _run_claude_json(prompt: str, label: str) -> list[dict]:
    """Run claude CLI with WebSearch only, parse JSON array from output.
    --tools restricts to WebSearch/WebFetch (no Bash/Read to prevent tangents).
    --no-session-persistence avoids writing session files to disk.
    --max-turns 15 caps iterations to prevent runaway loops.
    """
    try:
        proc = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "--no-session-persistence",
             "-p", prompt,
             "--tools", "WebSearch,WebFetch",
             "--max-turns", "15",
             "--output-format", "text"],
            capture_output=True, text=True, timeout=900, cwd=str(ROOT),
        )
        output = proc.stdout.strip()
        if not output:
            stderr = proc.stderr.strip()
            return [{"error": f"{label}: no output. stderr: {stderr[:200]}", "ticker": "ERROR", "fit": "not_recommended"}]
        # Try multiple extraction strategies for the JSON array
        # 1. Regex for [...] spanning the output
        match = re.search(r'\[\s*\{.*\}\s*\]', output, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        # 2. Strip markdown code fences (```json ... ```)
        cleaned = re.sub(r'```(?:json)?\s*', '', output)
        cleaned = re.sub(r'```', '', cleaned).strip()
        match2 = re.search(r'\[\s*\{.*\}\s*\]', cleaned, re.DOTALL)
        if match2:
            try:
                return json.loads(match2.group(0))
            except json.JSONDecodeError:
                pass
        # 3. Direct parse as last resort
        return json.loads(output)
    except subprocess.TimeoutExpired:
        return [{"error": f"{label}: timed out after 15 min", "ticker": "TIMEOUT", "fit": "not_recommended"}]
    except json.JSONDecodeError as e:
        return [{"error": f"{label}: JSON parse error: {e}", "ticker": "PARSE_ERROR", "fit": "not_recommended"}]
    except Exception as e:
        return [{"error": f"{label}: {e}", "ticker": "ERROR", "fit": "not_recommended"}]


def _run_claude_text(system_spec: str, user_msg: str, tools: str, label: str) -> str:
    """Run claude CLI with optional system-prompt, return stdout text.
    --tools restricts available tools. --no-session-persistence avoids disk bloat.
    """
    try:
        cmd = ["claude", "--dangerously-skip-permissions", "--no-session-persistence",
               "-p", user_msg,
               "--tools", tools,
               "--max-turns", "20",
               "--output-format", "text"]
        if system_spec:
            cmd += ["--system-prompt", system_spec]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
        return proc.stdout.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"{label} timed out after 10 min"
    except Exception as e:
        return f"{label} error: {e}"


def build_list1_prompt(positions: list[dict], cash_ils: int, today: str, exclude_tickers: list[str] | None = None) -> str:
    held_syms = ", ".join(
        p.get("yf_symbol") or p.get("symbol", "")
        for p in positions if p.get("currency") == "USD"
    )
    exclude_note = ""
    if exclude_tickers:
        exclude_note = f"\nCRITICAL: Do NOT include any of these tickers (already in another list): {', '.join(exclude_tickers)}. Pick DIFFERENT names.\n"
    max_per = min(cash_ils // 5, 25000)
    return f"""Today is {today}. You are an investment analyst.

Task: recommend 10 stocks or ETFs that complement this portfolio and are worth BUYING. Every pick must be actionable — something you would seriously consider purchasing. Do not pad the list with names to avoid or hold.

Current US holdings: {held_syms}

{_profile_summary()}

TOTAL cash available: ₪{cash_ils:,} (~${cash_ils//3:,} USD), split across ALL lists. Calibrate sizing to the profile above.
IMPORTANT: Do NOT recommend low-growth defensive sectors (healthcare ETFs, utilities, consumer staples, fixed income) just to "fill a gap." Every dollar has an opportunity cost — only recommend names whose expected return competes with high-growth alternatives at similar risk.
Prefer ETF over single stock ONLY when the thesis is a broad growth sector (e.g. semis, AI infra). For defensive/low-growth sectors, prefer specific high-growth single names over broad ETFs.
ETF OVERLAP RULE: Do NOT include two ETFs that track the same sector, index, or theme. Examples of overlapping pairs: SMH/SOXX (both semis), SPY/VOO (both S&P 500), VGT/XLK (both US tech), IEFA/EFA (both intl developed), EEM/IEMG (both emerging). If two ETFs share >50% of their top holdings, pick the better one — never both.
{exclude_note}
Use WebSearch to find what is performing well or has strong catalysts TODAY ({today}).
For each pick: assess bull case, key risk, fit quality, and when_to_buy. Use WATCH for names reporting earnings within 7 days and specify in when_to_buy exactly what to wait for.
Max invest per position: ₪{max_per:,}. Total across all 10 picks must not exceed ₪{cash_ils:,}.

{OUTPUT_FORMAT}"""


def build_list2_prompt(cash_ils: int, today: str, exclude_tickers: list[str] | None = None) -> str:
    exclude_note = ""
    if exclude_tickers:
        exclude_note = f"\nCRITICAL: Do NOT include any of these tickers (already in another list): {', '.join(exclude_tickers)}. Pick DIFFERENT names.\n"
    max_per = min(cash_ils // 5, 25000)
    return f"""Today is {today}. You are an investment analyst.

Task: recommend 10 stocks or ETFs for this investor profile that are worth BUYING. Every pick must be actionable — something you would seriously consider purchasing. Do not pad the list with names to avoid or hold.
Do NOT consider any specific existing holdings — treat this as a fresh portfolio.

{_profile_summary()}

TOTAL Cash: ₪{cash_ils:,} (~${cash_ils//3:,} USD) shared across ALL lists — size to the profile above.
Calibration based on the profile:
- For HIGH or VERY-HIGH risk profiles: prefer conviction growth positions over broad defensive diversification. Prefer ETF over single stock ONLY for broad growth themes (semis, AI infra). For other sectors prefer specific high-growth names. Do NOT recommend low-growth defensive names just for diversification.
- For LOW or MEDIUM risk profiles: prefer broad ETFs and dividend/quality names. Diversification across sectors IS a legitimate goal. Avoid stacking concentrated single-name growth bets.
- For MEDIUM-HIGH: balanced — mix of conviction names and diversified ETFs.
ETF OVERLAP RULE: Do NOT include two ETFs that track the same sector, index, or theme. Examples of overlapping pairs: SMH/SOXX (both semis), SPY/VOO (both S&P 500), VGT/XLK (both US tech), IEFA/EFA (both intl developed), EEM/IEMG (both emerging). If two ETFs share >50% of their top holdings, pick the better one — never both.
{exclude_note}
Use WebSearch to find strong opportunities in the market today ({today}).
For each pick: one-line bull case, one-line key risk, fit quality, and when_to_buy. Use WATCH for names reporting earnings within 7 days and specify in when_to_buy exactly what to wait for.
Max invest per position: ₪{max_per:,}. Total across all 10 picks must not exceed ₪{cash_ils:,}.

{OUTPUT_FORMAT}"""


def build_list3_prompt(today: str, cash_ils: int = 120000, exclude_tickers: list[str] | None = None) -> str:
    exclude_note = ""
    if exclude_tickers:
        exclude_note = f"\nCRITICAL: Do NOT include any of these tickers (already in another list): {', '.join(exclude_tickers)}. Pick DIFFERENT names.\n"
    max_per = min(cash_ils // 5, 25000)
    return f"""Today is {today}. You are an investment analyst.

Task: recommend 10 stocks or ETFs that are the best opportunities in the global market RIGHT NOW and are worth BUYING. Every pick must be actionable. Do not pad the list with names to avoid or hold.
You know nothing about the investor. Pick purely on merit — highest risk-adjusted return potential.
PRIORITIZE DIVERSITY: include at least 3 non-US or non-tech names (international ETFs, non-tech sectors, commodities). The value of this list is showing opportunities OUTSIDE the AI/tech consensus.
{exclude_note}
Use WebSearch to find what has strong momentum, catalysts, or value today ({today}).
Look across US large cap, international, sectors, ETFs — no constraints.
For each pick: one-line bull case, one-line key risk, fit quality, and when_to_buy. Use WATCH for names reporting earnings within 7 days and specify in when_to_buy exactly what to wait for.
Max invest per position: ₪{max_per:,}. Total across all 10 picks must not exceed ₪{cash_ils:,}.

{OUTPUT_FORMAT}"""


def _is_valid(items: list) -> bool:
    return len(items) >= 2 and not any(
        i.get("ticker") in ("TIMEOUT", "ERROR", "PARSE_ERROR") for i in items
    )


def _run_deep_analysis_for_ticker(
    ticker: str,
    today: str,
    positions: list[dict],
    user_views: str,
    bull_spec: str,
    risk_spec: str,
    manager_spec: str,
) -> dict:
    """Run bull + risk (parallel) then manager for one ticker. Return signal dict."""
    cached = _load_signal_cache(ticker, today)
    if cached:
        return cached

    held = [p for p in positions if
            p.get("symbol", "").upper() == ticker or
            (p.get("yf_symbol") or "").upper() == ticker or
            (p.get("yf_symbol") or "").upper().replace(".TA", "") == ticker]
    if held:
        p = held[0]
        pos_context = (
            f"The user CURRENTLY HOLDS {p.get('quantity')} shares of {ticker} "
            f"at avg cost {p.get('cost_basis_adj_local', 'unknown')} "
            f"{p.get('currency','USD')}. "
            f"Current weight: {p.get('broker_pct_of_portfolio', '?')}% of portfolio. "
            f"This is a HOLD/TRIM/SELL decision."
        )
    else:
        pos_context = f"The user does NOT currently hold {ticker}. This is a BUY/PASS decision."

    held_syms = ", ".join(
        p.get("yf_symbol") or p.get("symbol", "")
        for p in positions if p.get("currency") == "USD"
    ) or "unknown"

    officer_user_msg = (
        f"Ticker: {ticker}\nToday: {today}\n"
        f"{pos_context}\nCurrent US holdings: {held_syms}\n\n"
        f"User views context (read-only — challenge, do not defer):\n{user_views}\n\n"
        f"Run your mandatory research process as specified. Output the full format per your spec."
    )

    results: dict = {}

    def run_agent(name: str, spec: str, tools: str):
        results[name] = _run_claude_text(spec, officer_user_msg, tools, name)

    bull_thread = threading.Thread(target=run_agent, args=("bull", bull_spec, "Read,Bash,Glob,Grep,WebSearch,WebFetch"))
    risk_thread = threading.Thread(target=run_agent, args=("risk", risk_spec, "Read,Bash,Glob,Grep,WebSearch,WebFetch"))
    bull_thread.start()
    risk_thread.start()
    bull_thread.join()
    risk_thread.join()

    bull_out = results.get("bull", "(no output)")
    risk_out = results.get("risk", "(no output)")

    manager_user_msg = (
        f"Ticker: {ticker}\nToday: {today}\n"
        f"{pos_context}\nCurrent US holdings: {held_syms}\n\n"
        f"User views context (read-only):\n{user_views}\n\n"
        f"BULL CASE (from bull-officer):\n{bull_out}\n\n"
        f"BEAR CASE (from risk-officer):\n{risk_out}\n\n"
        f"Synthesize per your spec. Start output with:\n"
        f"SIGNAL: <BUY|HOLD|TRIM|SELL> | CONFIDENCE: <weak|moderate|strong>\n"
        f"data_as_of: {today}"
    )

    manager_out = _run_claude_text(manager_spec, manager_user_msg, "Read,Bash,Glob,Grep,WebSearch", "portfolio-manager")

    sig_m   = re.search(r"SIGNAL:\s*([A-Z]+)\s*\|", manager_out)
    conf_m  = re.search(r"CONFIDENCE:\s*(\w+)", manager_out)
    signal  = sig_m.group(1).strip() if sig_m else "—"
    conf    = conf_m.group(1).strip() if conf_m else "—"

    # Extract one-sentence manager summary (first non-blank line after signal line)
    summary = ""
    for line in manager_out.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("SIGNAL:") and not stripped.startswith("data_as_of"):
            summary = stripped[:200]
            break

    result = {
        "symbol":           ticker,
        "date":             today,
        "bull":             bull_out,
        "risk":             risk_out,
        "manager":          manager_out,
        "signal":           signal,
        "confidence":       conf,
        "manager_summary":  summary,
        "analysis_cached":  False,
    }
    _save_signal_cache(ticker, today, result)
    return result


def _attach_signal(item: dict, sig: dict) -> dict:
    item = dict(item)
    item["signal"]           = sig.get("signal", "—")
    item["confidence"]       = sig.get("confidence", "—")
    item["manager_summary"]  = sig.get("manager_summary", "")
    item["analysis_cached"]  = True
    return item


def main() -> None:
    try:
        raw    = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    cash_ils    = int(params.get("cash_ils", 120000))
    today       = params.get("date", dt.date.today().isoformat())
    # Default flipped 2026-04-27: Phase 1 only is the cheap default. Pass
    # phase1_only=false explicitly when you want the full bull/risk/manager
    # deep-dive on every candidate (~10x more expensive — only worth it when
    # you have real cash to deploy and want structured debate per name).
    phase1_only = bool(params.get("phase1_only", True))
    positions   = _load_positions()

    import sys as _sys
    if not phase1_only:
        print("Running FULL pipeline (Phase 1 + Phase 2 deep analysis) — expensive. "
              "For routine scans, omit phase1_only or set it to true.", file=_sys.stderr)
    else:
        print("Running Phase 1 only (cheap candidate-list mode). "
              "Pass phase1_only=false for the full bull/risk/manager deep-dive.", file=_sys.stderr)

    out_dir      = ROOT / "research" / "daily" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path     = out_dir / "opportunities.json"
    partial_path = out_dir / "opportunities.partial.json"

    existing: dict = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text())
        except Exception:
            existing = {}

    output: dict = {
        "date":                today,
        "cash_ils":            cash_ils,
        "list1_portfolio_fit": existing.get("list1_portfolio_fit", []),
        "list2_profile_fit":   existing.get("list2_profile_fit",   []),
        "list3_market_picks":  existing.get("list3_market_picks",  []),
        "_status":             existing.get("_status", "running"),
    }

    def _write_partial(status: str) -> None:
        partial = dict(output)
        partial["_status"] = status
        partial_path.write_text(json.dumps(partial, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------ Phase 1
    # Lists run sequentially so each subsequent list can exclude prior tickers.
    # List 1 runs first (portfolio-aware), List 2 excludes List 1 tickers,
    # List 3 excludes both. This prevents the 43% duplication problem.
    def _extract_tickers(items: list[dict]) -> list[str]:
        return [i["ticker"].upper() for i in items
                if i.get("ticker") and i["ticker"] not in ("ERROR", "TIMEOUT", "PARSE_ERROR")]

    seen_tickers: list[str] = []

    # List 1 — portfolio fit
    if not _is_valid(output["list1_portfolio_fit"]):
        _write_partial("phase1:list1_portfolio_fit")
        output["list1_portfolio_fit"] = _run_claude_json(
            build_list1_prompt(positions, cash_ils, today, exclude_tickers=None), "list1_portfolio_fit")
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    seen_tickers.extend(_extract_tickers(output["list1_portfolio_fit"]))

    # List 2 and 3 can run in parallel since both just exclude list 1 tickers
    # Actually list 3 should also exclude list 2 — run list 2 first, then list 3
    if not _is_valid(output["list2_profile_fit"]):
        _write_partial("phase1:list2_profile_fit")
        output["list2_profile_fit"] = _run_claude_json(
            build_list2_prompt(cash_ils, today, exclude_tickers=seen_tickers), "list2_profile_fit")
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    seen_tickers.extend(_extract_tickers(output["list2_profile_fit"]))

    if not _is_valid(output["list3_market_picks"]):
        _write_partial("phase1:list3_market_picks")
        output["list3_market_picks"] = _run_claude_json(
            build_list3_prompt(today, cash_ils, exclude_tickers=seen_tickers), "list3_market_picks")
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    # Post-Phase-1 validation: flag duplicates + overlapping ETFs
    _all_tickers_seen: dict[str, str] = {}
    _dup_count = 0
    for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
        for item in output[key]:
            t = item.get("ticker", "").upper()
            if not t or t in ("ERROR", "TIMEOUT", "PARSE_ERROR"):
                continue
            if t in _all_tickers_seen and _all_tickers_seen[t] != key:
                item["_duplicate_of"] = _all_tickers_seen[t]
                _dup_count += 1
            else:
                _all_tickers_seen[t] = key
    if _dup_count:
        output["_dedup_warning"] = f"{_dup_count} duplicate ticker(s) across lists — flagged with _duplicate_of field"

    # ETF overlap detection across all lists
    etf_overlaps = _detect_etf_overlaps(output)
    if etf_overlaps:
        output["_etf_overlap_warning"] = etf_overlaps

    if phase1_only:
        output["_status"] = "done:phase1_only"
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        if partial_path.exists():
            partial_path.unlink()
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    # ------------------------------------------------------------------ Phase 2
    # Collect unique tickers that need deep analysis
    all_items: list[dict] = (
        output["list1_portfolio_fit"] +
        output["list2_profile_fit"] +
        output["list3_market_picks"]
    )
    unique_tickers = list(dict.fromkeys(
        i["ticker"].upper() for i in all_items
        if i.get("ticker") and i["ticker"] not in ("ERROR", "TIMEOUT", "PARSE_ERROR")
    ))

    # Load agent specs once
    bull_spec    = _load_agent_spec("bull-officer")
    risk_spec    = _load_agent_spec("risk-officer")
    manager_spec = _load_agent_spec("portfolio-manager")
    user_views   = _load_user_views()

    signals: dict[str, dict] = {}

    _write_partial("phase2:starting")

    # Run in batches of 3 concurrent tickers
    batch_size = 3
    for batch_start in range(0, len(unique_tickers), batch_size):
        batch = unique_tickers[batch_start: batch_start + batch_size]
        _write_partial(f"phase2:{','.join(batch)}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as ex:
            futures = {
                ex.submit(
                    _run_deep_analysis_for_ticker,
                    ticker, today, positions, user_views,
                    bull_spec, risk_spec, manager_spec,
                ): ticker
                for ticker in batch
            }
            for fut in concurrent.futures.as_completed(futures):
                ticker = futures[fut]
                try:
                    signals[ticker] = fut.result()
                except Exception as e:
                    signals[ticker] = {"signal": "—", "confidence": "—",
                                       "manager_summary": f"error: {e}",
                                       "analysis_cached": False}
                # Flush after each ticker completes
                out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    # Attach signals back to every list item
    for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
        output[key] = [
            _attach_signal(item, signals[item["ticker"].upper()])
            if item.get("ticker", "").upper() in signals else item
            for item in output[key]
        ]

    output["_status"] = "done"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    if partial_path.exists():
        partial_path.unlink()

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
