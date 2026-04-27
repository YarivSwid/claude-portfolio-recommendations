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
SIGNALS_DIR = ROOT / "research" / "signals"

SIGNAL_MARKER_RE = re.compile(
    r"(?:\*{0,2}\s*(?:signal|recommendation|action|verdict|trade\s*signal)\s*\*{0,2}\s*[:=]\s*\*{0,2}\s*"
    r"(?:BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\b"
    r"|\|\s*\*{2}(?:BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\*{2}\s*\|)",
    re.IGNORECASE,
)
CONFIDENCE_RE = re.compile(
    r"\b[Cc]onfidence\s*[:=]\s*\*{0,2}\s*(weak|moderate|strong|severe|0?\.\d+)",
    re.MULTILINE,
)
DATA_AS_OF_RE = re.compile(r"\bdata_as_of\s*[:=]\s*(\S+)", re.IGNORECASE | re.MULTILINE)
SIGNAL_VALUE_RE = re.compile(
    r"\b(?:signal|recommendation|action|verdict)\s*[:=]\s*\*{0,2}\s*(BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\b",
    re.IGNORECASE,
)
TICKER_RE = re.compile(r"\b([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)\b")
_EXCLUDE = {
    "BUY", "SELL", "HOLD", "KEEP", "ADD", "TRIM", "EXIT", "FLAG",
    "REDUCE", "AVOID", "SKIP", "WATCH",
    "NAV", "USD", "ILS", "FX", "IL", "EU", "US", "UK", "AI",
    "GT", "ATF", "TA", "ETF", "CC", "YoY", "QoQ",
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

    signal_blocks = re.findall(
        r"([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)[^\n]*\n(?:[^\n]*\n){0,5}?"
        r"(?:signal|recommendation|action|verdict)\s*[:=]\s*\*{0,2}\s*(BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)",
        full_text, re.IGNORECASE,
    )

    written = []
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

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
        }
        try:
            path.write_text(json.dumps(record, indent=2))
            written.append(ticker)
        except Exception:
            continue

    sys.exit(0)


if __name__ == "__main__":
    main()
