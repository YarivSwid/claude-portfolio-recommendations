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
Return ONLY a valid JSON array of EXACTLY 12 items. We post-process for same-ticker dedup and weak-evidence rejection, so you must oversupply by ~20% to leave 10 actionable rows. Quality still matters — every item must satisfy the citations + numeric bear-threshold rules below. No markdown, no explanation outside the JSON.
RESPONSE MUST start with the character `[` and end with `]`. No preamble — no "Let me…", "I'll now…", "Selection logic:" — those drain the turn budget and the response gets truncated before the JSON is emitted. Deliberate silently; the array is the entire reply.
Each item must have these exact keys:
{
  "ticker": "NVDA",
  "name": "NVIDIA Corporation",
  "type": "stock",
  "fit": "exceptional",
  "max_invest_ils": 18000,
  "signal": "BUY",
  "when_to_buy": "now — no near-term catalyst risk",
  "cooling_trigger": null,
  "rally_pct_30d": "+18% (from ~$170 on Mar 25 to ~$201 on Apr 25)",
  "price_vs_200dma": "+12% above 200DMA (~$180)",
  "entry_note": "modest extension, not parabolic — reasonable entry",
  "reason_fits": "one sentence why this fits",
  "bull_thesis": "2-3 sentences with at least one inline citation in form (Source — YYYY-MM-DD)",
  "bear_threshold": "If <metric> falls below <value> by <date>, thesis breaks",
  "catalyst": {"type": "earnings|product|regulatory|none", "date": "YYYY-MM-DD or null", "what_to_watch": "one line"},
  "data_as_of": "YYYY-MM-DD"
}
CRITICAL RULES — universe selection:
- Items MUST be drawn from the CANDIDATE UNIVERSE provided in the prompt above (the "clean" and "extended" pools). Do NOT propose a ticker that is not in those pools unless you cite a specific dated catalyst that justifies inclusion (and even then, prefer the pools).
- The "parabolic" pool was excluded upstream for momentum-extension reasons (RSI ≥80, +50% above 200DMA, or 10+ up-day streaks). DO NOT propose any ticker that fits that profile, even if not explicitly in the dropped list.
- List size: EXACTLY 12 items (we post-process for dedup + weak-evidence rejection and need 10 to survive). If the clean pool has fewer than 12 viable picks, fill the remainder from the extended pool with explicit WAIT-FOR-COOLING + numeric cooling_trigger. Never pad with parabolic names.

CRITICAL RULES — signal vocabulary (4 values only):
- "STRONG_BUY" = clean entry (from clean pool), exceptional thesis, deploy NOW. Cooling trigger field must be null.
- "BUY" = clean entry, solid thesis, deploy NOW or on minor dips. Cooling trigger field must be null.
- "WAIT-FOR-COOLING" = ticker is from the EXTENDED pool. Required: a deterministic, numeric cooling trigger in the "cooling_trigger" field, e.g. "RSI <60 for 3 consecutive days" or "pullback to pct_vs_200dma <0.20". Do NOT use vague triggers like "wait for cooling" or "wait for pullback" without a numeric threshold.
- "WAIT-FOR-CATALYST" = ticker has earnings or a dated event within 14 days. Required: catalyst.date populated, catalyst.what_to_watch populated. Cooling_trigger may be null.
- DO NOT use HOLD, AVOID, SELL, WATCH, or any other signal. The four above are exhaustive.

CRITICAL RULES — bull_thesis and bear_threshold (HARD REQUIREMENTS — picks failing these are dropped post-hoc):
- "bull_thesis": 2-3 sentences. MUST include at least one inline citation in the form "(Source — YYYY-MM-DD)". Example: "Q1 revenue +35% YoY (CNBC — 2026-04-16). RPO $523B (+438% YoY) implies multi-year capex/cashflow conversion." Picks with 0 citations are AUTO-REJECTED as weak-evidence — do not waste a slot.
- "bear_threshold": MUST contain a numeric value AND a comparison operator (<, >, <=, >=, below, above, falls below, exceeds, drops below, etc.). Reject vague phrases like "if growth slows" or "geopolitical risk". Example: "If RPO conversion drops below 35% by Q2 FY27 OR capex/RPO ratio exceeds 0.10 for 2 consecutive quarters." Picks without a numeric+operator bear threshold are AUTO-REJECTED as weak-evidence.
- "catalyst": always populate the object even when type is "none". For "none", set what_to_watch to a description of why the thesis is valuation-driven (e.g. "valuation reset; no dated trigger within 90 days").

CRITICAL RULES — other fields:
- "fit": one of exceptional / good / moderate. Justify in "reason_fits".
- "rally_pct_30d" and "price_vs_200dma": you may write "n/a" — these will be deterministically overwritten by yfinance post-processing.
- "entry_note": one sentence on entry quality (pulled-back / consolidating / extended-but-not-parabolic).
- Sort by signal strength: STRONG_BUY → BUY → WAIT-FOR-CATALYST → WAIT-FOR-COOLING. (Post-processing normalizes WAIT-FOR-* to WATCH with a `_watch_subtype` and a concrete `when_to_buy` — you keep emitting the WAIT-FOR-* tokens so the sort still works.)
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


def _load_cash_ready_usd() -> float | None:
    """Read cash_ready_usd from positions.json. Returns None if unset."""
    if not POSITIONS_PATH.exists():
        return None
    try:
        data = json.loads(POSITIONS_PATH.read_text())
        if not isinstance(data, dict):
            return None
        amt = data.get("cash_ready_usd")
        return float(amt) if amt not in (None, "") else None
    except Exception:
        return None


def _fetch_usdils() -> float | None:
    """Use the currency-conversion skill to fetch today's USDILS rate."""
    fx_script = ROOT / ".claude/skills/currency-conversion/scripts/fx.py"
    if not fx_script.exists():
        return None
    try:
        import subprocess
        proc = subprocess.run(
            ["python3", str(fx_script)], input="{}",
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            d = json.loads(proc.stdout)
            rate = d.get("rate_ils_per_usd") or d.get("usdils") or d.get("rate")
            return float(rate) if rate else None
    except Exception:
        pass
    return None


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


def _extract_json_array(output: str) -> list[dict] | None:
    """Try three strategies to extract a JSON array from agent output.
    Returns None if no array can be parsed (caller decides whether to retry)."""
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
    try:
        result = json.loads(output)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    return None


# Marker we prepend to a retry prompt to force the agent to skip preamble and
# emit the JSON array as the very first characters of its response.
_JSON_FIRST_PREFIX = (
    "RESPONSE FORMAT — read carefully:\n"
    "Your reply must start with the character `[` and end with `]`. No preamble. "
    "No 'Let me…', no 'I'll now…', no narration of your selection logic. "
    "If you need to deliberate, do it silently — only the final JSON array is allowed in your response.\n\n"
)


def _run_claude_json(prompt: str, label: str, max_turns: int = 25, retries: int = 1) -> list[dict]:
    """Run claude CLI with WebSearch only, parse JSON array from output.
    --tools restricts to WebSearch/WebFetch (no Bash/Read to prevent tangents).
    --no-session-persistence avoids writing session files to disk.
    max_turns caps iterations; lists with broad scope (e.g. list 2's whole-market screen)
    should pass a higher value to avoid the agent narrating into the turn budget.
    Retry behavior:
      - empty stdout → retry with same prompt (transient CLI / max-turns blowout)
      - preamble-without-array (agent narrated past the turn budget) → retry once with
        a stricter "emit array first, no preamble" prefix AND a bumped max_turns
    """
    debug_log = ROOT / "research" / "daily" / dt.date.today().isoformat() / "scan-debug.log"
    debug_log.parent.mkdir(parents=True, exist_ok=True)

    def _attempt(used_prompt: str, used_max_turns: int) -> tuple[str, str, int]:
        proc = subprocess.run(
            ["claude", "--dangerously-skip-permissions", "--no-session-persistence",
             "-p", used_prompt,
             "--tools", "WebSearch,WebFetch",
             "--max-turns", str(used_max_turns),
             "--output-format", "text"],
            capture_output=True, text=True, timeout=900, cwd=str(ROOT),
        )
        return proc.stdout.strip(), proc.stderr.strip(), proc.returncode

    def _log(msg: str) -> None:
        with debug_log.open("a") as f:
            f.write(f"[{dt.datetime.now().isoformat()}] {label} {msg}\n")

    try:
        current_prompt    = prompt
        current_max_turns = max_turns
        output, stderr, rc = "", "", 0
        # Two-axis retry loop: empty stdout OR parse failure (preamble) both retry.
        for attempt in range(1, retries + 2):
            output, stderr, rc = _attempt(current_prompt, current_max_turns)
            if not output:
                _log(f"attempt {attempt}: empty stdout, rc={rc}, stderr[:500]={stderr[:500]}")
                # bump max_turns and add the JSON-first prefix for the next try
                current_prompt    = _JSON_FIRST_PREFIX + prompt
                current_max_turns = current_max_turns + 15
                continue
            parsed = _extract_json_array(output)
            if parsed is not None:
                return parsed
            # Output exists but no JSON array — almost always the "I'll now compose
            # the final JSON…" preamble symptom. Retry with stricter framing.
            _log(
                f"attempt {attempt}: preamble-without-array detected "
                f"(len={len(output)}, max_turns={current_max_turns}, rc={rc})"
            )
            _log(f"  output[:500]: {output[:500]}")
            _log(f"  output[-500:]: {output[-500:]}")
            current_prompt    = _JSON_FIRST_PREFIX + prompt
            current_max_turns = current_max_turns + 15
        # Exhausted retries — return a structured error sentinel that downstream
        # callers + the UI can detect.
        if not output:
            return [{"error": f"{label}: no output after {retries+1} attempts. stderr: {stderr[:200]}",
                     "ticker": "ERROR", "fit": "not_recommended"}]
        return [{"error": f"{label}: JSON parse error after {retries+1} attempts (preamble or malformed output)",
                 "ticker": "PARSE_ERROR", "fit": "not_recommended"}]
    except subprocess.TimeoutExpired:
        return [{"error": f"{label}: timed out after 15 min", "ticker": "TIMEOUT", "fit": "not_recommended"}]
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


def build_list1_prompt(
    positions: list[dict],
    cash_ils: int,
    today: str,
    exclude_tickers: list[str] | None = None,
    universe_block: str = "",
    portfolio_context: str = "",
) -> str:
    held_syms = ", ".join(
        p.get("yf_symbol") or p.get("symbol", "")
        for p in positions if p.get("currency") == "USD"
    )
    exclude_note = ""
    if exclude_tickers:
        exclude_note = f"\nCRITICAL: Do NOT include any of these tickers (already in another list): {', '.join(exclude_tickers)}. Pick DIFFERENT names.\n"
    max_per = min(cash_ils // 5, 25000)
    return f"""Today is {today}. You are an investment analyst.

Task: recommend 5-to-10 stocks or ETFs that complement this portfolio and are worth BUYING. Every pick must be actionable — something you would seriously consider purchasing. Do not pad the list with names to avoid or hold. If the clean pool is thin, return fewer picks (down to 5) — quality over count.

Current US holdings: {held_syms}

{_profile_summary()}

{portfolio_context}

{universe_block}

TOTAL cash available: ₪{cash_ils:,} (~${cash_ils//3:,} USD), split across ALL lists. Calibrate sizing to the profile above.
IMPORTANT: Do NOT recommend low-growth defensive sectors (healthcare ETFs, utilities, consumer staples, fixed income) just to "fill a gap." Every dollar has an opportunity cost — only recommend names whose expected return competes with high-growth alternatives at similar risk.
Prefer ETF over single stock ONLY when the thesis is a broad growth sector (e.g. semis, AI infra). For defensive/low-growth sectors, prefer specific high-growth single names over broad ETFs.
ETF OVERLAP RULE: Do NOT include two ETFs that track the same sector, index, or theme. Examples of overlapping pairs: SMH/SOXX (both semis), SPY/VOO (both S&P 500), VGT/XLK (both US tech), IEFA/EFA (both intl developed), EEM/IEMG (both emerging). If two ETFs share >50% of their top holdings, pick the better one — never both.
{exclude_note}
Use WebSearch to fill in bull_thesis with at least one inline (Source — YYYY-MM-DD) citation, and to find concrete bear_threshold metrics. Anchor any fundamentals you cite to the screener row in the candidate universe above (fwd_pe, rev_g, gm) — do NOT invent numbers.
Max invest per position: ₪{max_per:,}. Total across all picks must not exceed ₪{cash_ils:,}.

{OUTPUT_FORMAT}"""


def build_list2_prompt(
    cash_ils: int,
    today: str,
    exclude_tickers: list[str] | None = None,
    universe_block: str = "",
) -> str:
    exclude_note = ""
    if exclude_tickers:
        exclude_note = f"\nCRITICAL: Do NOT include any of these tickers (already in another list): {', '.join(exclude_tickers)}. Pick DIFFERENT names.\n"
    max_per = min(cash_ils // 5, 25000)
    return f"""Today is {today}. You are an investment analyst.

Task: recommend 5-to-10 stocks or ETFs for this investor profile that are worth BUYING. Every pick must be actionable — something you would seriously consider purchasing. Do not pad the list with names to avoid or hold. If the clean pool is thin, return fewer picks (down to 5) — quality over count.
Do NOT consider any specific existing holdings — treat this as a fresh portfolio.

{_profile_summary()}

{universe_block}

TOTAL Cash: ₪{cash_ils:,} (~${cash_ils//3:,} USD) shared across ALL lists — size to the profile above.
Calibration based on the profile:
- For HIGH or VERY-HIGH risk profiles: prefer conviction growth positions over broad defensive diversification. Prefer ETF over single stock ONLY for broad growth themes (semis, AI infra). For other sectors prefer specific high-growth names. Do NOT recommend low-growth defensive names just for diversification.
- For LOW or MEDIUM risk profiles: prefer broad ETFs and dividend/quality names. Diversification across sectors IS a legitimate goal. Avoid stacking concentrated single-name growth bets.
- For MEDIUM-HIGH: balanced — mix of conviction names and diversified ETFs.
ETF OVERLAP RULE: Do NOT include two ETFs that track the same sector, index, or theme. Examples of overlapping pairs: SMH/SOXX (both semis), SPY/VOO (both S&P 500), VGT/XLK (both US tech), IEFA/EFA (both intl developed), EEM/IEMG (both emerging). If two ETFs share >50% of their top holdings, pick the better one — never both.
{exclude_note}
Use WebSearch to fill in bull_thesis with at least one inline (Source — YYYY-MM-DD) citation, and to find concrete bear_threshold metrics. Anchor any fundamentals you cite to the screener row in the candidate universe above — do NOT invent numbers.
Max invest per position: ₪{max_per:,}. Total across all picks must not exceed ₪{cash_ils:,}.

{OUTPUT_FORMAT}"""


def build_list3_prompt(
    today: str,
    cash_ils: int = 120000,
    exclude_tickers: list[str] | None = None,
    universe_block: str = "",
) -> str:
    exclude_note = ""
    if exclude_tickers:
        exclude_note = f"\nCRITICAL: Do NOT include any of these tickers (already in another list): {', '.join(exclude_tickers)}. Pick DIFFERENT names.\n"
    max_per = min(cash_ils // 5, 25000)
    return f"""Today is {today}. You are an investment analyst.

Task: recommend 5-to-10 stocks or ETFs that are the best opportunities in the global market RIGHT NOW and are worth BUYING. Every pick must be actionable. Do not pad the list with names to avoid or hold. If the clean pool is thin, return fewer picks (down to 5).
You know nothing about the investor. Pick purely on merit — highest risk-adjusted return potential.
PRIORITIZE DIVERSITY: include at least 3 non-US or non-tech names (international ETFs, non-tech sectors, commodities). The value of this list is showing opportunities OUTSIDE the AI/tech consensus.
NOTE: List 3 may include picks NOT in the screener universe (which is S&P 500 only) when the diversity goal demands it (international ETFs, commodities). The candidate universe below is your starting point for US large-cap names; you may go beyond it for non-US / non-tech diversifiers, but those picks must still pass the parabolic filter (RSI <80, ≤50% above 200DMA, ≤10 up-day streak).
{exclude_note}
{universe_block}

Use WebSearch to fill in bull_thesis with at least one inline (Source — YYYY-MM-DD) citation, and to find concrete bear_threshold metrics.
Look across US large cap, international, sectors, ETFs — no constraints beyond the parabolic filter.
Max invest per position: ₪{max_per:,}. Total across all picks must not exceed ₪{cash_ils:,}.

{OUTPUT_FORMAT}"""


def _is_valid(items: list) -> bool:
    """A list is valid (skip retry) if:
    - at least 5 entries (was 2 — caller must explicitly tolerate shorter lists),
    - no error sentinels.
    Adaptive list size is enforced in the prompt; this gate only protects against
    catastrophic empty / parse-error returns."""
    if len(items) < 5:
        return False
    return not any(
        i.get("ticker") in ("TIMEOUT", "ERROR", "PARSE_ERROR") for i in items
    )


# ---------------------------------------------------------------- momentum-check
# Post-process every Phase-1 list with deterministic yfinance metrics.
# Overwrites the LLM's WebSearch-guessed rally_pct_30d and price_vs_200dma with
# computed values, attaches a momentum_check object, and downgrades BUY → WATCH
# when streak_flag == "extended".
def _run_momentum_check(tickers: list[str]) -> dict:
    if not tickers:
        return {}
    try:
        proc = subprocess.run(
            ["python3", str(ROOT / ".claude" / "skills" / "momentum-check" / "scripts" / "momentum.py")],
            input=json.dumps({"tickers": tickers}),
            capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return data.get("results", {})
    except Exception:
        return {}


def _format_pct(p: float | None) -> str:
    if p is None:
        return "n/a"
    sign = "+" if p >= 0 else ""
    return f"{sign}{p * 100:.1f}%"


def _yf_symbol_for(ticker: str) -> str:
    """Convert a list-item ticker to the symbol momentum-check expects.
    Most are passthrough (NVDA stays NVDA). TASE names already have .TA suffix."""
    return ticker


# ---------------------------------------------------------------- universe builder
# Increment A — Pre-filter parabolic names BEFORE the LLM sees the universe.
# The screener provides deterministic candidates; momentum-check buckets each as
# clean / extended / parabolic; parabolic is dropped from the universe entirely.

PARABOLIC_RSI = 80.0
PARABOLIC_PCT_VS_200DMA = 0.50
PARABOLIC_UP_DAYS = 10
CLEAN_RSI = 65.0
CLEAN_PCT_VS_200DMA = 0.30
CLEAN_UP_DAYS = 5


def _run_screener_top(top_n: int = 50) -> list[dict]:
    """Invoke the screener skill, return its candidate list (already ranked).
    First run can be slow (~5 min) because it fills yfinance .info cache for
    all S&P 500 names; subsequent runs are <30s while cache is fresh."""
    try:
        proc = subprocess.run(
            ["python3", str(ROOT / ".claude" / "skills" / "screener" / "scripts" / "screen.py")],
            input=json.dumps({"top_n": top_n}),
            capture_output=True, text=True, timeout=600, cwd=str(ROOT),
        )
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
        return data.get("candidates", [])
    except Exception:
        return []


def _bucket_momentum(m: dict) -> str:
    """Return 'clean' | 'extended' | 'parabolic' from a momentum-check result."""
    if not m or "error" in m:
        return "clean"  # no data → don't penalize
    rsi = m.get("rsi_14") or 0
    pct200 = m.get("pct_vs_200dma") or 0
    up_days = m.get("consecutive_up_days") or 0
    if rsi >= PARABOLIC_RSI or pct200 >= PARABOLIC_PCT_VS_200DMA or up_days >= PARABOLIC_UP_DAYS:
        return "parabolic"
    if m.get("streak_flag") == "extended" or rsi >= CLEAN_RSI or pct200 >= CLEAN_PCT_VS_200DMA or up_days >= CLEAN_UP_DAYS:
        return "extended"
    return "clean"


def _prefilter_universe(candidates: list[dict]) -> dict:
    """Run momentum-check on the screener's top-N, bucket into clean/extended/parabolic.
    Returns: {clean: [tickers], extended: [tickers], parabolic_dropped: [tickers],
              metrics: {ticker -> momentum-check dict}}.
    Parabolic is dropped from the LLM universe entirely.
    """
    tickers = [c.get("ticker") for c in candidates if c.get("ticker")]
    metrics = _run_momentum_check(tickers)

    clean: list[str] = []
    extended: list[str] = []
    parabolic: list[str] = []

    for t in tickers:
        m = metrics.get(t, {})
        bucket = _bucket_momentum(m)
        if bucket == "parabolic":
            parabolic.append(t)
        elif bucket == "extended":
            extended.append(t)
        else:
            clean.append(t)

    return {
        "clean": clean,
        "extended": extended,
        "parabolic_dropped": parabolic,
        "metrics": metrics,
        "candidates_by_ticker": {c.get("ticker"): c for c in candidates if c.get("ticker")},
    }


def _format_universe_block(universe: dict, max_per_pool: int = 30) -> str:
    """Render the clean+extended pools as a compact block for the LLM prompt.
    Includes screener fundamentals (fwd_pe, rev_growth, score) for grounding."""
    cands = universe.get("candidates_by_ticker", {})

    def _fmt_pct(v, sign=False):
        if not isinstance(v, (int, float)):
            return "n/a"
        if sign:
            return f"{v*100:+.0f}%"
        return f"{v*100:.0f}%"

    def _fmt_num(v, fmt="{:.1f}"):
        if not isinstance(v, (int, float)):
            return "n/a"
        return fmt.format(v)

    def _row(t: str) -> str:
        c = cands.get(t, {})
        m = universe.get("metrics", {}).get(t, {})
        sec = (c.get("sector") or "")[:20]
        return (
            f"  {t:6s} sector={sec:20s} "
            f"score={_fmt_num(c.get('score'), '{:.2f}')} "
            f"fwd_pe={_fmt_num(c.get('fwd_pe'), '{:.1f}')} "
            f"rev_g={_fmt_pct(c.get('rev_growth_yoy'), sign=True)} "
            f"gm={_fmt_pct(c.get('gross_margin'))} "
            f"rsi={_fmt_num(m.get('rsi_14'), '{:.0f}')} "
            f"vs200={_fmt_pct(m.get('pct_vs_200dma'), sign=True)}"
        )

    clean_rows = [_row(t) for t in universe.get("clean", [])[:max_per_pool]]
    ext_rows = [_row(t) for t in universe.get("extended", [])[:max_per_pool]]
    drop_list = ", ".join(universe.get("parabolic_dropped", [])[:30])

    return (
        "CANDIDATE UNIVERSE (pre-filtered by deterministic momentum gate):\n"
        f"\nCLEAN POOL (RSI<65, ≤30% above 200DMA, ≤5 up-day streak — primary buy candidates):\n"
        + "\n".join(clean_rows or ["  (empty — universe is hot today)"])
        + f"\n\nEXTENDED POOL (one momentum flag, NOT parabolic — only via WAIT-FOR-COOLING with numeric trigger):\n"
        + "\n".join(ext_rows or ["  (empty)"])
        + f"\n\nPARABOLIC DROPPED (excluded — DO NOT propose any of these): {drop_list or '(none)'}\n"
    )


_CITATION_RE = re.compile(r"\([^)]+?\s—\s\d{4}-\d{2}-\d{2}\)")
_BEAR_NUMERIC_RE = re.compile(r"\d")
_BEAR_OP_RE = re.compile(r"(<|>|≤|≥|below|above|drops?|exceeds?|under|over)", re.IGNORECASE)
_MGMT_QUOTE_RE = re.compile(r"\b(said|stated|guided|commentary|management|CEO|CFO)\b", re.IGNORECASE)


def _score_evidence_strength(item: dict) -> str:
    """Deterministic post-processor: weak | moderate | strong.

    weak     = 0 citations OR no numeric+operator in bear_threshold
    moderate = 1-2 citations + numeric+operator in bear_threshold
    strong   = 3+ citations + numeric+operator + management-quote marker
    """
    bull = item.get("bull_thesis") or item.get("bull_point") or ""
    bear = item.get("bear_threshold") or item.get("risk_point") or ""
    citations = len(_CITATION_RE.findall(bull))
    bear_has_numeric = bool(_BEAR_NUMERIC_RE.search(bear))
    bear_has_op = bool(_BEAR_OP_RE.search(bear))
    has_mgmt_quote = bool(_MGMT_QUOTE_RE.search(bull))

    if citations == 0 or not (bear_has_numeric and bear_has_op):
        return "weak"
    if citations >= 3 and bear_has_numeric and bear_has_op and has_mgmt_quote:
        return "strong"
    return "moderate"


def _apply_evidence_strength_to_list(items: list[dict]) -> list[dict]:
    """Annotate each item with evidence_strength, then DROP weak BUY-side rows.

    Weak = 0 citations OR no numeric+operator in bear_threshold. Those picks
    aren't actionable and have no place in an opportunity list — we drop them
    at the source rather than rendering them dimmed in the UI.
    WAIT-FOR-* signals are kept regardless of evidence so the user still sees
    the watchlist with their numeric trigger.
    """
    out = []
    for item in items:
        item = dict(item)
        if item.get("ticker") in ("ERROR", "TIMEOUT", "PARSE_ERROR"):
            out.append(item)
            continue
        item["evidence_strength"] = _score_evidence_strength(item)
        sig = (item.get("signal") or "").upper()
        if item["evidence_strength"] == "weak" and sig in ("STRONG_BUY", "BUY"):
            # Drop entirely. Caller can inspect _weak_evidence_dropped to debug.
            continue
        out.append(item)
    return out


def _entry_quality_label(m: dict) -> str:
    """Compute entry quality from momentum metrics. Returns one of:
    pulled_back | consolidating | rallying | extended | parabolic.
    """
    if not m or "error" in m:
        return "consolidating"
    bucket = _bucket_momentum(m)
    if bucket == "parabolic":
        return "parabolic"
    if bucket == "extended":
        return "extended"
    p5 = m.get("pct_change_5d") or 0
    p10 = m.get("pct_change_10d") or 0
    down = m.get("consecutive_down_days") or 0
    if down >= 3 or p5 <= -0.03:
        return "pulled_back"
    if abs(p10) <= 0.03:
        return "consolidating"
    return "rallying"


def _build_readable_when_to_buy(
    subtype: str,
    m: dict,
    cat_date: str | None = None,
    cat_what: str | None = None,
    days_until_catalyst: int | None = None,
) -> str:
    """Turn momentum metrics into a single concrete trigger string.

    Returns one human-readable sentence with the most binding trigger for the
    given subtype. For cooling, picks among RSI/pullback/200DMA depending on
    which is most actionable. For catalyst, names the date and what to watch.

    Examples:
      cooling, RSI-driven   -> "RSI cools to ≤65 (currently 78)"
      cooling, price-driven -> "Pullback to ~$172 (-7% from $185)"
      cooling, 200DMA       -> "Within +10% of 200DMA $158 (currently +28%)"
      catalyst, earnings    -> "After 2026-06-04 earnings (7 days); confirm guide holds"
    """
    rsi = m.get("rsi_14")
    pct200 = m.get("pct_vs_200dma")
    last_close = m.get("last_close")

    if subtype == "catalyst" and cat_date:
        days_str = f" ({days_until_catalyst} days)" if isinstance(days_until_catalyst, int) else ""
        what = (cat_what or "confirm thesis post-print before deploying").strip().rstrip(".")
        return f"After {cat_date} earnings{days_str}; {what}."

    # Cooling: pick the single most binding trigger.
    # Priority: deep extension (RSI≥80 or vs200>40%) → RSI; moderate → price target;
    # mild → 200DMA reference.
    triggers: list[str] = []

    if rsi is not None and rsi >= 70:
        rsi_target = 65 if rsi < 80 else 70
        triggers.append(f"RSI cools to ≤{rsi_target} (currently {rsi:.0f})")

    if last_close is not None:
        try:
            pullback_pct = 0.07 if (rsi is None or rsi < 80) else 0.10
            target = last_close * (1 - pullback_pct)
            if target >= 100:
                tgt_s = f"${target:,.0f}"
                cur_s = f"${last_close:,.0f}"
            elif target >= 10:
                tgt_s = f"${target:.1f}"
                cur_s = f"${last_close:.1f}"
            else:
                tgt_s = f"${target:.2f}"
                cur_s = f"${last_close:.2f}"
            triggers.append(f"pullback to ~{tgt_s} (-{pullback_pct*100:.0f}% from {cur_s})")
        except Exception:
            pass

    if pct200 is not None and pct200 > 0.25:
        try:
            dma200 = last_close / (1 + pct200) if last_close is not None else None
            if dma200 is not None and dma200 >= 100:
                dma_s = f"${dma200:,.0f}"
            elif dma200 is not None:
                dma_s = f"${dma200:.1f}"
            else:
                dma_s = "200DMA"
            triggers.append(f"within +10% of 200DMA {dma_s} (currently {pct200*100:+.0f}%)")
        except Exception:
            pass

    if not triggers:
        # No actionable trigger derivable — fall back to a clearly-labelled stub.
        return "Wait for a 5–7% pullback or RSI ≤65 before deploying (no specific level computed)."

    # Use the first two triggers joined with " OR " — gives the user a choice of entry.
    return " OR ".join(triggers[:2]).capitalize() + "."


def _apply_momentum_check_to_list(items: list[dict]) -> list[dict]:
    """Enrich each list item with deterministic momentum metrics. Force the
    signal to one of STRONG_BUY / BUY / WATCH (with _watch_subtype: cooling |
    catalyst) based on momentum bucket and the LLM's catalyst field.
    """
    tickers = [_yf_symbol_for(i["ticker"]) for i in items
               if i.get("ticker") and i["ticker"] not in ("ERROR", "TIMEOUT", "PARSE_ERROR")]
    metrics = _run_momentum_check(tickers)

    out = []
    for item in items:
        item = dict(item)
        t = item.get("ticker", "")
        m = metrics.get(_yf_symbol_for(t))
        if not m or "error" in (m or {}):
            item["momentum_check"] = {"status": "unavailable"}
            item["entry_quality"] = "consolidating"
            out.append(item)
            continue

        item["rally_pct_30d"] = (
            f"{_format_pct(m.get('pct_change_30d'))} (computed from yfinance close on {m.get('as_of')})"
        )
        pct_200 = m.get("pct_vs_200dma")
        if pct_200 is not None:
            direction = "above" if pct_200 >= 0 else "below"
            item["price_vs_200dma"] = (
                f"{_format_pct(pct_200)} {direction} 200DMA (last close {m.get('last_close')})"
            )
        else:
            item["price_vs_200dma"] = "n/a (insufficient history)"

        item["momentum_check"] = {
            "status": "ok",
            "as_of": m.get("as_of"),
            "consecutive_up_days": m.get("consecutive_up_days"),
            "consecutive_down_days": m.get("consecutive_down_days"),
            "rsi_14": m.get("rsi_14"),
            "pct_change_5d": m.get("pct_change_5d"),
            "pct_change_10d": m.get("pct_change_10d"),
            "pct_change_30d": m.get("pct_change_30d"),
            "pct_vs_50dma": m.get("pct_vs_50dma"),
            "pct_vs_200dma": m.get("pct_vs_200dma"),
            "dist_from_52w_high_pct": m.get("dist_from_52w_high_pct"),
            "streak_flag": m.get("streak_flag"),
            "flags": m.get("flags", []),
        }
        item["entry_quality"] = _entry_quality_label(m)

        # ---- Signal split: WAIT-FOR-CATALYST takes precedence over WAIT-FOR-COOLING.
        # If the LLM populated catalyst with a near-term dated event, that wins;
        # otherwise momentum bucket decides.
        bucket = _bucket_momentum(m)
        catalyst = item.get("catalyst") or {}
        cat_type = (catalyst.get("type") or "").lower() if isinstance(catalyst, dict) else ""
        cat_date = catalyst.get("date") if isinstance(catalyst, dict) else None

        days_until_catalyst = None
        if cat_date:
            try:
                days_until_catalyst = (dt.date.fromisoformat(cat_date) - dt.date.today()).days
            except Exception:
                days_until_catalyst = None

        original_signal = item.get("signal")
        original_when = item.get("when_to_buy", "")

        catalyst_window_open = (
            cat_type == "earnings"
            and isinstance(days_until_catalyst, int)
            and 0 <= days_until_catalyst <= 14
        )

        def _to_watch_cooling(reason_tag: str) -> None:
            item["signal"] = "WATCH"
            item["_watch_subtype"] = "cooling"
            item["cooling_trigger"] = item.get("cooling_trigger") or (
                f"RSI <60 for 3 consecutive days OR pct_vs_200dma <0.20 "
                f"(currently RSI {(m.get('rsi_14') or 0):.0f}, "
                f"vs200 {(m.get('pct_vs_200dma') or 0)*100:+.0f}%)"
            )
            item["when_to_buy"] = _build_readable_when_to_buy("cooling", m)
            item["_signal_overridden"] = reason_tag

        def _to_watch_catalyst() -> None:
            item["signal"] = "WATCH"
            item["_watch_subtype"] = "catalyst"
            item["when_to_buy"] = _build_readable_when_to_buy(
                "catalyst", m,
                cat_date=cat_date,
                cat_what=catalyst.get("what_to_watch") if isinstance(catalyst, dict) else None,
                days_until_catalyst=days_until_catalyst,
            )
            item["_signal_overridden"] = "earnings"

        if bucket == "parabolic":
            _to_watch_cooling("parabolic")
        elif bucket == "extended" and original_signal in ("STRONG_BUY", "BUY"):
            _to_watch_cooling("extended")
        elif catalyst_window_open and original_signal in ("STRONG_BUY", "BUY"):
            _to_watch_catalyst()
        elif original_signal in ("WATCH", "WAIT-FOR-COOLING", "WAIT-FOR-CATALYST"):
            # LLM (or older cached output) emitted legacy/raw vocab — coerce.
            if catalyst_window_open or original_signal == "WAIT-FOR-CATALYST":
                _to_watch_catalyst()
            elif bucket == "extended" or original_signal == "WAIT-FOR-COOLING":
                _to_watch_cooling("vocab_coercion")
            else:
                # Clean momentum, no catalyst — upgrade WATCH to BUY.
                item["signal"] = "BUY"
                item["_signal_overridden"] = "vocab_coercion_to_buy"

        out.append(item)
    return out


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

    # Cash resolution priority:
    # 1. Explicit cash_ils in params (caller override — highest priority)
    # 2. cash_ready_usd from positions.json × FX (user-set via dashboard)
    # 3. Hard-coded 120000 ILS default (last resort)
    cash_source = "default"
    if "cash_ils" in params:
        cash_ils    = int(params["cash_ils"])
        cash_source = "params.cash_ils"
    else:
        cash_usd = _load_cash_ready_usd()
        rate     = _fetch_usdils() if cash_usd is not None else None
        if cash_usd is not None and rate:
            cash_ils    = int(cash_usd * rate)
            cash_source = f"positions.cash_ready_usd ({cash_usd:.0f} USD × {rate:.4f})"
        else:
            cash_ils    = 120000
    print(f"[scan] cash_ils={cash_ils:,} from {cash_source}", file=sys.stderr)
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

    # Crash-recovery: if the last run died mid-phase (status like "phase1:listN"),
    # the listN that was being generated is unreliable. Drop it so it regenerates.
    # Without this, an empty-list-3 from a killed process would persist as
    # "valid empty result" and the scanner would never retry it.
    prev_status = existing.get("_status", "")
    crash_recovered_list = None
    if prev_status.startswith("phase1:"):
        crashed_list = prev_status.split(":", 1)[1]
        if crashed_list in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
            existing[crashed_list] = []
            crash_recovered_list = crashed_list
            print(f"crash-recovery: previous run died during {crashed_list}; "
                  f"will regenerate that list", file=_sys.stderr)

    output: dict = {
        "date":                today,
        "cash_ils":            cash_ils,
        "list1_portfolio_fit": existing.get("list1_portfolio_fit", []),
        "list2_profile_fit":   existing.get("list2_profile_fit",   []),
        "list3_market_picks":  existing.get("list3_market_picks",  []),
        "_status":             "running",
    }
    if crash_recovered_list:
        output["_crash_recovered_list"] = crash_recovered_list

    def _write_partial(status: str) -> None:
        partial = dict(output)
        partial["_status"] = status
        partial_path.write_text(json.dumps(partial, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------ Universe
    # Increment A — build a deterministic candidate universe before any LLM call.
    # Pre-filter parabolic names so the LLM never sees them. Reused by all 3 lists.
    print("Building candidate universe (screener + momentum pre-filter)…", file=_sys.stderr)
    screener_top = _run_screener_top(top_n=50)
    universe = _prefilter_universe(screener_top)
    universe_block = _format_universe_block(universe, max_per_pool=30)
    output["_universe_quality"] = {
        "data_as_of": today,
        "screener_count": len(screener_top),
        "clean_count": len(universe.get("clean", [])),
        "extended_count": len(universe.get("extended", [])),
        "parabolic_dropped": universe.get("parabolic_dropped", []),
    }
    print(
        f"Universe built: {len(universe.get('clean', []))} clean, "
        f"{len(universe.get('extended', []))} extended, "
        f"{len(universe.get('parabolic_dropped', []))} parabolic dropped.",
        file=_sys.stderr,
    )

    # Compute lightweight portfolio context for List 1 (top sector weights).
    # If sector-allocation is unavailable, list 1 still works — it just won't
    # have the factor-aware fit nudge.
    portfolio_context = ""
    try:
        sa_proc = subprocess.run(
            ["python3", str(ROOT / ".claude" / "skills" / "sector-allocation" / "scripts" / "sectors.py")],
            input="{}",
            capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        )
        if sa_proc.returncode == 0:
            sa_data = json.loads(sa_proc.stdout)
            by_sector = sa_data.get("by_sector", {})
            top_sectors = sorted(by_sector.items(), key=lambda kv: -kv[1])[:5]
            top_str = ", ".join(f"{k} {v*100:.0f}%" for k, v in top_sectors)
            hhi = sa_data.get("hhi")
            portfolio_context = (
                f"PORTFOLIO FACTOR WEIGHTS (data_as_of {sa_data.get('data_as_of')}): {top_str}. "
                f"HHI {hhi:.4f} ({sa_data.get('hhi_interpretation', 'unknown')}). "
                "Use this to assess factor concentration: avoid recommendations that push any single "
                "sector above 50%, and prefer diversifying picks when a sector is already >25%."
            )
    except Exception:
        pass

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
            build_list1_prompt(
                positions, cash_ils, today,
                exclude_tickers=None,
                universe_block=universe_block,
                portfolio_context=portfolio_context,
            ),
            "list1_portfolio_fit",
        )
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    seen_tickers.extend(_extract_tickers(output["list1_portfolio_fit"]))

    # List 2 and 3 can run in parallel since both just exclude list 1 tickers
    # Actually list 3 should also exclude list 2 — run list 2 first, then list 3
    if not _is_valid(output["list2_profile_fit"]):
        _write_partial("phase1:list2_profile_fit")
        # Lists 2 and 3 have the broadest scope (no portfolio anchor / no profile
        # anchor) and historically narrate into the turn budget. Give them more
        # headroom and one extra retry on top of the per-call preamble-retry.
        output["list2_profile_fit"] = _run_claude_json(
            build_list2_prompt(
                cash_ils, today,
                exclude_tickers=seen_tickers,
                universe_block=universe_block,
            ),
            "list2_profile_fit",
            max_turns=40,
            retries=2,
        )
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    seen_tickers.extend(_extract_tickers(output["list2_profile_fit"]))

    if not _is_valid(output["list3_market_picks"]):
        _write_partial("phase1:list3_market_picks")
        output["list3_market_picks"] = _run_claude_json(
            build_list3_prompt(
                today, cash_ils,
                exclude_tickers=seen_tickers,
                universe_block=universe_block,
            ),
            "list3_market_picks",
            max_turns=40,
            retries=2,
        )
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    # Apply momentum-check: overwrite LLM-guessed rally_pct_30d / price_vs_200dma
    # with deterministic yfinance metrics, split signal into
    # STRONG_BUY / BUY / WAIT-FOR-COOLING / WAIT-FOR-CATALYST.
    # Then drop weak-evidence BUY rows entirely (0 citations OR no numeric
    # bear threshold) — they aren't actionable.
    _weak_dropped: list[str] = []
    for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
        if _is_valid(output[key]):
            output[key] = _apply_momentum_check_to_list(output[key])
            pre  = [(it.get("ticker"), it.get("signal")) for it in output[key]]
            output[key] = _apply_evidence_strength_to_list(output[key])
            post = {(it.get("ticker"), it.get("signal")) for it in output[key]}
            for t, s in pre:
                if (t, s) not in post:
                    _weak_dropped.append(f"{key}:{t} ({s}, weak-evidence)")
    if _weak_dropped:
        output["_weak_evidence_dropped"] = _weak_dropped

    # Same-list dedup: the LLM occasionally returns the same ticker twice in the
    # same list (e.g. GOOG STRONG_BUY + GOOG BUY/weak). Keep the strongest entry
    # per (list, ticker) and drop the rest. Cross-list duplicates are still
    # allowed and only flagged informationally below.
    _SIGNAL_RANK = {
        "STRONG_BUY": 4, "BUY": 3,
        # WATCH-catalyst ranks above WATCH-cooling: a dated near-term event is
        # usually more actionable than a "wait for pullback" trigger.
        "WATCH": 2,
        # Legacy aliases preserved so any older cached items rank correctly
        # if a downstream consumer ever re-reads them pre-formatter.
        "WAIT-FOR-CATALYST": 2, "WAIT-FOR-COOLING": 1,
    }
    _EVID_RANK   = {"strong": 3, "moderate": 2, "weak": 1}
    def _conviction_key(it: dict) -> tuple:
        sig = (it.get("signal") or "").upper()
        subtype_bonus = 0
        if sig == "WATCH":
            # Catalyst-subtype WATCH ranks slightly above cooling-subtype.
            subtype_bonus = 1 if it.get("_watch_subtype") == "catalyst" else 0
        return (
            _SIGNAL_RANK.get(sig, 0),
            subtype_bonus,
            _EVID_RANK.get((it.get("evidence_strength") or "").lower(), 0),
            int(it.get("max_invest_ils") or 0),
        )
    _same_list_dropped: list[str] = []
    for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks"):
        if not _is_valid(output[key]):
            continue
        best_by_ticker: dict[str, dict] = {}
        order: list[str] = []
        for item in output[key]:
            t = (item.get("ticker") or "").upper()
            if not t or t in ("ERROR", "TIMEOUT", "PARSE_ERROR"):
                # Pass error sentinels through with a unique key so they aren't merged.
                order.append(f"__err__{len(order)}")
                best_by_ticker[order[-1]] = item
                continue
            if t in best_by_ticker:
                prev = best_by_ticker[t]
                if _conviction_key(item) > _conviction_key(prev):
                    _same_list_dropped.append(f"{key}:{t} (kept stronger entry)")
                    best_by_ticker[t] = item
                else:
                    _same_list_dropped.append(f"{key}:{t} (dropped weaker duplicate)")
            else:
                best_by_ticker[t] = item
                order.append(t)
        output[key] = [best_by_ticker[k] for k in order if k in best_by_ticker]
    if _same_list_dropped:
        output["_same_list_dedup"] = _same_list_dropped

    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))

    # Post-Phase-1 validation: flag CROSS-list duplicates + overlapping ETFs
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
        output["_dedup_warning"] = (
            f"{_dup_count} ticker(s) appear on more than one list — informational, "
            f"high-conviction signal. See _duplicate_of field per item. NOT a bug; "
            f"do not remove."
        )

    # ETF overlap detection across all lists
    etf_overlaps = _detect_etf_overlaps(output)
    if etf_overlaps:
        output["_etf_overlap_warning"] = etf_overlaps

    # Loud-fail check: if any list came back empty (LLM call failed silently
    # after retries), mark the run as failed and exit non-zero so the caller
    # knows immediately instead of getting silently-truncated output.
    failed_lists = [
        key for key in ("list1_portfolio_fit", "list2_profile_fit", "list3_market_picks")
        if not output[key]
    ]
    if failed_lists:
        output["_status"] = f"phase1_failed:{','.join(failed_lists)}"
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        print(f"FAIL: {len(failed_lists)} list(s) empty after retries: {failed_lists}. "
              f"See research/daily/{today}/scan-debug.log", file=_sys.stderr)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        _sys.exit(2)

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
