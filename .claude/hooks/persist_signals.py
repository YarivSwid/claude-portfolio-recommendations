#!/usr/bin/env python3
"""Stop hook — persist trade signals emitted this turn to research/signals/.

Runs AFTER the blocking hooks (enforce_risk_officer, enforce_signal_consistency,
enforce_signal_reversal).  If those passed, the signal is legitimate and we
write a small JSON file so future turns can detect reversals via
enforce_signal_reversal.py.

File naming: research/signals/<TICKER>_<YYYY-MM-DD>.json
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.signal_patterns import (
    SIGNAL_MARKER_RE,
    TICKER_BLACKLIST,
    find_signals,
)
from _log import emit, emit_error  # noqa: E402

HOOK_NAME = "persist_signals"

SIGNALS_DIR = ROOT / "research" / "signals"

CONFIDENCE_RE = re.compile(
    r"\b[Cc]onfidence\s*[:=]\s*\*{0,2}\s*(weak|moderate|strong|severe|0?\.\d+)",
    re.MULTILINE,
)
DATA_AS_OF_RE = re.compile(r"\bdata_as_of\s*[:=]\s*(\S+)", re.IGNORECASE | re.MULTILINE)

_EXCLUDE = set(TICKER_BLACKLIST) | {
    "IL", "EU", "GT", "ATF", "TA", "CC", "YoY", "QoQ",
    "MED", "HIGH", "LOW", "RISK", "Q1", "Q2", "Q3", "Q4",
}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        sys.exit(0)

    entries = []
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        sys.exit(0)

    last_user_idx = 0
    for i in range(len(entries) - 1, -1, -1):
        msg = entries[i].get("message", {}) or {}
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if not any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in (content if isinstance(content, list) else [])
            ):
                last_user_idx = i
                break

    full_text = ""
    for entry in entries[last_user_idx:]:
        msg = entry.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                full_text += block.get("text", "") + "\n"

    if not (
        SIGNAL_MARKER_RE.search(full_text)
        and CONFIDENCE_RE.search(full_text)
        and DATA_AS_OF_RE.search(full_text)
    ):
        sys.exit(0)

    today = date.today().isoformat()
    confidence_match = CONFIDENCE_RE.search(full_text)
    confidence = confidence_match.group(1) if confidence_match else "unknown"
    data_as_of_match = DATA_AS_OF_RE.search(full_text)
    data_as_of = data_as_of_match.group(1).strip("*").strip() if data_as_of_match else today

    signal_blocks = find_signals(full_text)

    written = []
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch momentum tier per unique BUY/ADD ticker at signal-emit time, so the
    # decision-log captures the tier as-of-decision-date (Red today may be Normal
    # in 3 weeks; backtesting needs the historical value). One call per ticker,
    # cached daily by momentum-check via the yfinance cache.
    unique_tickers = []
    for t, _ in signal_blocks:
        tu = t.upper()
        if tu in _EXCLUDE or len(tu) < 2 or tu in unique_tickers:
            continue
        unique_tickers.append(tu)
    tier_by_ticker: dict = {}
    if unique_tickers:
        try:
            import subprocess
            MOM_SCRIPT = ROOT / ".claude" / "skills" / "momentum-check" / "scripts" / "momentum.py"
            if MOM_SCRIPT.exists():
                proc = subprocess.run(
                    ["python3", str(MOM_SCRIPT)],
                    input=json.dumps({"tickers": unique_tickers}),
                    capture_output=True, text=True, timeout=60, cwd=str(ROOT),
                )
                if proc.returncode == 0:
                    res = json.loads(proc.stdout).get("results", {})
                    for t, m in res.items():
                        if isinstance(m, dict):
                            tier_by_ticker[t] = m.get("tier")
        except Exception:
            pass

    for ticker, signal in signal_blocks:
        ticker = ticker.upper()
        if ticker in _EXCLUDE or len(ticker) < 2:
            continue
        signal = signal.upper()
        path = SIGNALS_DIR / f"{ticker}_{today}.json"
        record = {
            "ticker": ticker,
            "signal": signal,
            "confidence": confidence,
            "data_as_of": data_as_of,
            "persisted_at": today,
            "tier": tier_by_ticker.get(ticker),
            # Embed the most recent assistant text so decision-log/append.py can
            # extract R/R + thesis-breaks-at without re-reading the transcript.
            "raw": full_text[-12000:],
        }
        try:
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            written.append(ticker)
        except Exception:
            continue

    # Append each persisted signal to research/decisions.jsonl via decision-log skill.
    # Fire-and-forget — non-fatal on errors so signal persistence never blocks on
    # the log write. The append script is idempotent (skips if source_signal_file
    # is already logged).
    import subprocess
    APPEND_SCRIPT = ROOT / ".claude" / "skills" / "decision-log" / "scripts" / "append.py"
    if APPEND_SCRIPT.exists():
        for ticker in written:
            try:
                signal_file_rel = f"research/signals/{ticker}_{today}.json"
                subprocess.run(
                    ["python3", str(APPEND_SCRIPT)],
                    input=json.dumps({"signal_file": signal_file_rel}),
                    capture_output=True, text=True, timeout=30,
                )
            except Exception:
                continue

    emit(HOOK_NAME, "pass", reason=f"persisted {len(written)} signal(s)" if written else "no signals to persist")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _e:
        emit_error(HOOK_NAME, _e)
        sys.exit(0)
