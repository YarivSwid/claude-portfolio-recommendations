#!/usr/bin/env python3
"""decision-log/append.py — append one signal row to research/decisions.jsonl.

Called by the persist_signals.py Stop hook after a per-ticker signal JSON has been
written. Idempotent — re-running on the same signal_file is a no-op.

Input (stdin JSON):
  {"signal_file": "research/signals/APP_2026-05-27.json"}

OR (direct fields, for backfill from non-standard sources):
  {"ticker": "APP", "signal": "BUY", "confidence": "moderate",
   "entry_price": 533.87, "date": "2026-05-27", ...}

Output (stdout JSON): the appended row, or {"skipped": "reason"} on no-op.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

DECISIONS_PATH = ROOT / "research" / "decisions.jsonl"

THESIS_BREAKS_RE = re.compile(
    r"thesis\s*breaks?\s*at[:\s]+([^\n]+)", re.IGNORECASE
)
RR_BASE_RE = re.compile(
    r"(?:base[-\s]case|base)\s*(?:r/r|r\W*r|risk[-\s]*reward)\s*(?:ratio)?\s*[:\s≈~]+\s*"
    r"(\d+(?:\.\d+)?)\s*[:×x]\s*1",
    re.IGNORECASE,
)
RR_TAIL_RE = re.compile(
    r"(?:tail|severe)\s*(?:r/r|r\W*r|risk[-\s]*reward)\s*(?:ratio)?\s*[:\s≈~]+\s*"
    r"(\d+(?:\.\d+)?)\s*[:×x]\s*1",
    re.IGNORECASE,
)
# Fallback for older signal files that don't use the new "base-case R/R" wording.
# Catches plain "R/R ratio: ~1.8:1", "R/R approximately **0.75:1**", "R/R is ~1.2:1", etc.
# Tolerates markdown bolding (**), filler words (is/approximately/about/roughly), and
# the usual prefix punctuation. Numbers like 0.75, 1.2, 10 are all accepted.
RR_PLAIN_RE = re.compile(
    r"\bR/R\b[^\n]{0,40}?(\d+(?:\.\d+)?)\s*\**\s*[:×x]\s*\**\s*1\b",
    re.IGNORECASE,
)
# Recover signal token from "SIGNAL: BUY" style headers in the manager text
SIGNAL_HEADER_RE = re.compile(
    r"\bSIGNAL\s*[:=]\s*\*{0,2}\s*(BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD|STRONG[_\s-]?BUY|PASS)\b",
    re.IGNORECASE,
)

# Detect non-signals: failed subagent runs (API errors, timeouts, rate limits, empty
# output). Logging these as "—" pollutes the decision-log with non-decisions.
FAILED_RUN_RE = re.compile(
    r"\bAPI Error\b|ConnectionRefused|Unable to connect|"
    r"timed out after|hit your limit|\(no output\)|rate[- ]?limit",
    re.IGNORECASE,
)

# Standard yfinance symbol → currency (TASE tickers end in .TA = ILS, everything else USD)
def _currency_for(ticker: str) -> str:
    return "ILS" if ticker.upper().endswith(".TA") else "USD"


def _fetch_entry_price(ticker: str, date_str: str | None = None) -> float | None:
    """Return the *historical* close on `date_str` (signal emit date), not today's price.

    Why this matters: decision-log is *prospective* — it records what the price was
    when the signal was issued, so future review.py runs can compute return-since-entry.
    Using today's currentPrice would make entry == current always, which produces
    return_pct = 0% for every row (a bug we hit during initial integration testing).
    """
    try:
        from scripts.lib import data as pdata
        if not date_str:
            # Fallback to current price only when no date provided.
            info = pdata.fetch_info(ticker)
            return float(info.get("currentPrice") or info.get("regularMarketPrice") or 0) or None
        hist = pdata.fetch_history(ticker, period="2y")
        if hist is None or hist.empty:
            return None
        target = dt.date.fromisoformat(date_str)
        # Find the closest trading day on or BEFORE the signal date.
        if hasattr(hist.index, "date"):
            mask = hist.index.date <= target
        else:
            mask = [d.date() <= target for d in hist.index]
        eligible = hist[mask] if hasattr(mask, "__iter__") else hist.loc[mask]
        if len(eligible) == 0:
            # If the signal is older than 2y of history, fall back to current price.
            info = pdata.fetch_info(ticker)
            return float(info.get("currentPrice") or info.get("regularMarketPrice") or 0) or None
        return float(eligible["Close"].iloc[-1])
    except Exception:
        return None


def _fetch_tier(ticker: str) -> str | None:
    """Read tier from momentum-check skill output (Option B labelling)."""
    try:
        from scripts.lib import data as pdata
        # Read price history directly and reuse momentum-check classifier
        sys.path.insert(0, str(ROOT / ".claude" / "skills" / "momentum-check" / "scripts"))
        import momentum as mom  # type: ignore
        result, _ = mom._analyze_one(ticker)
        return result.get("tier")
    except Exception:
        return None


def _already_logged(signal_file: str) -> bool:
    """Idempotency check — was this signal_file already appended?"""
    if not DECISIONS_PATH.exists():
        return False
    try:
        for line in DECISIONS_PATH.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if row.get("source_signal_file") == signal_file:
                    return True
            except json.JSONDecodeError:
                continue
    except Exception:
        return False
    return False


def main() -> None:
    raw = sys.stdin.read().strip() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({"error": "invalid JSON on stdin"}))
        sys.exit(1)

    # Path 1: signal_file provided — read the per-ticker signal JSON
    signal_file = payload.get("signal_file")
    if signal_file:
        rel_path = signal_file if not signal_file.startswith("/") else \
            str(Path(signal_file).relative_to(ROOT))
        if _already_logged(rel_path):
            print(json.dumps({"skipped": f"already logged: {rel_path}"}))
            return
        try:
            sf = ROOT / signal_file if not Path(signal_file).is_absolute() else Path(signal_file)
            sig = json.loads(sf.read_text())
        except Exception as e:
            print(json.dumps({"error": f"could not read signal file: {e}"}))
            sys.exit(1)

        ticker = (sig.get("ticker") or sig.get("symbol") or "").upper()
        signal = (sig.get("signal") or "").upper().strip()
        date_str = sig.get("date") or sig.get("data_as_of") or dt.date.today().isoformat()
        confidence = sig.get("confidence") or "unknown"
        full_text = " ".join(
            str(v) for k, v in sig.items() if k in ("bull", "risk", "manager", "raw")
        )
        # Skip rows where the manager subagent never produced a real recommendation
        # (API error, timeout, rate-limit, empty output). These are non-decisions
        # and would pollute the log with "—" rows.
        manager_text = str(sig.get("manager", ""))
        if (
            (not signal or signal in ("—", "-", "N/A", "UNKNOWN"))
            and (FAILED_RUN_RE.search(manager_text) or len(manager_text.strip()) < 200)
        ):
            print(json.dumps({"skipped": f"manager run failed or empty: {rel_path}"}))
            return
        # Fall back to parsing "SIGNAL: BUY" from the manager text if the top-level
        # field is empty or a placeholder dash (older signal files do this).
        if not signal or signal in ("—", "-", "N/A", "UNKNOWN"):
            m = SIGNAL_HEADER_RE.search(full_text)
            if m:
                signal = m.group(1).upper().replace(" ", "_").replace("-", "_")
    else:
        # Path 2: direct fields for backfill
        ticker = (payload.get("ticker") or "").upper()
        signal = (payload.get("signal") or "").upper()
        date_str = payload.get("date") or dt.date.today().isoformat()
        confidence = payload.get("confidence") or "unknown"
        rel_path = payload.get("source_signal_file") or "manual-backfill"
        full_text = payload.get("raw_text", "")
        if _already_logged(rel_path) and rel_path != "manual-backfill":
            print(json.dumps({"skipped": f"already logged: {rel_path}"}))
            return

    if not ticker or not signal:
        print(json.dumps({"error": "missing ticker or signal"}))
        sys.exit(1)

    # Extract enrichment fields from the signal's manager text
    rr_base = None
    rr_tail = None
    thesis_breaks_at = None
    m = RR_BASE_RE.search(full_text)
    if m:
        rr_base = float(m.group(1))
    m = RR_TAIL_RE.search(full_text)
    if m:
        rr_tail = float(m.group(1))
    # Fallback for older signal files: take the first plain "R/R: X:1" as rr_base.
    if rr_base is None:
        m = RR_PLAIN_RE.search(full_text)
        if m:
            rr_base = float(m.group(1))
    m = THESIS_BREAKS_RE.search(full_text)
    if m:
        thesis_breaks_at = m.group(1).strip()[:200]

    entry_price = payload.get("entry_price") or _fetch_entry_price(ticker, date_str)
    # Prefer the tier captured at signal-emit time (in the signal file), then the
    # payload override, then re-compute as a last resort. The emit-time value is
    # the historically-accurate one for backtesting; today's value drifts.
    tier = None
    if signal_file:
        tier = sig.get("tier")
    tier = tier or payload.get("tier") or _fetch_tier(ticker)
    currency = _currency_for(ticker)

    row = {
        "date": date_str,
        "ticker": ticker,
        "signal": signal,
        "confidence": confidence,
        "entry_price": entry_price,
        "currency": currency,
        "rr_base": rr_base,
        "rr_tail": rr_tail,
        "thesis_breaks_at": thesis_breaks_at,
        "tier": tier,
        "data_as_of": sig.get("data_as_of") if signal_file else date_str,
        "source_signal_file": rel_path,
        "logged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }

    DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_PATH.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps(row, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
