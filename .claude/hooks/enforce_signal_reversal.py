#!/usr/bin/env python3
"""Stop hook — block a turn that silently reverses a signal from the last 14 days.

When the assistant emits a BUY/SELL/HOLD/TRIM signal for a ticker that already has
a signal file in research/signals/ from within 14 days, the turn must contain an
explicit acknowledgement line:
  - "SIGNAL CHANGE: <prior> → <new> because ..." (reversal)
  - "SIGNAL CONFIRMED: still <signal> because ..." (confirmation)

If a reversal is detected without that line, the turn is blocked.

Only fires when the current turn contains a trade signal (same triple-check as
enforce_risk_officer.py: signal marker + confidence + data_as_of).
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT        = Path(__file__).resolve().parents[2]
SIGNALS_DIR = ROOT / "research" / "signals"

SIGNAL_MARKER_RE = re.compile(
    r"(?:\*{0,2}\s*(?:signal|recommendation|action|verdict|trade\s*signal)\s*\*{0,2}\s*[:=]\s*\*{0,2}\s*"
    r"(?:BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\b"
    r"|\|\s*\*{2}(?:BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\*{2}\s*\|)",
    re.IGNORECASE,
)
CONFIDENCE_RE = re.compile(r"\b[Cc]onfidence\s*[:=]\s*\*{0,2}\s*(weak|moderate|strong|0?\.\d+)", re.MULTILINE)
DATA_AS_OF_RE = re.compile(r"\bdata_as_of\s*[:=]", re.IGNORECASE | re.MULTILINE)

# Matches "SIGNAL: BUY | CONFIDENCE: ..." or "SIGNAL: SELL ..."
SIGNAL_VALUE_RE = re.compile(r"\bSIGNAL\s*:\s*(BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\b", re.IGNORECASE)
TICKER_RE       = re.compile(r"\b([A-Z]{1,5}(?:[.-][A-Z]{1,3})?)\b")
ACK_RE          = re.compile(r"SIGNAL\s+(CHANGE|CONFIRMED)\s*:", re.IGNORECASE)

OPPOSITES = {
    "BUY":  {"SELL", "TRIM", "EXIT", "CUT"},
    "ADD":  {"SELL", "TRIM", "EXIT", "CUT"},
    "SELL": {"BUY", "ADD"},
    "TRIM": {"BUY", "ADD"},
    "EXIT": {"BUY", "ADD"},
    "CUT":  {"BUY", "ADD"},
    "HOLD": set(),
}


def _load_prior_signal(ticker: str) -> dict | None:
    """Return the most recent signal file for ticker if within 14 days."""
    if not SIGNALS_DIR.exists():
        return None
    today = date.today()
    files = sorted(SIGNALS_DIR.glob(f"{ticker}_*.json"), reverse=True)
    for f in files:
        try:
            stem_date = date.fromisoformat(f.stem.split("_", 1)[-1])
            if (today - stem_date).days > 14:
                break
            if stem_date == today:
                continue  # skip today's own file being written
            data = json.loads(f.read_text())
            if data.get("signal") and data["signal"] not in ("—", ""):
                return {"date": stem_date.isoformat(), "signal": data["signal"].upper(), "file": f.name}
        except Exception:
            continue
    return None


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

    # Find current turn
    last_user_idx = 0
    for i in range(len(entries) - 1, -1, -1):
        msg = entries[i].get("message", {}) or {}
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in (content if isinstance(content, list) else [])):
                last_user_idx = i
                break

    current_turn = entries[last_user_idx:]

    # Collect all assistant text from current turn
    full_text = ""
    for entry in current_turn:
        msg = entry.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                full_text += block.get("text", "") + "\n"

    # Only proceed if this turn contains a real trade signal
    if not (SIGNAL_MARKER_RE.search(full_text)
            and CONFIDENCE_RE.search(full_text)
            and DATA_AS_OF_RE.search(full_text)):
        sys.exit(0)

    # Extract (ticker, signal) pairs from SIGNAL: <value> lines
    # Look for patterns like "XLV ... SIGNAL: BUY" nearby
    signal_blocks = re.findall(
        r"([A-Z]{1,5}(?:[.\-][A-Z]{1,3})?)[^\n]*\n(?:[^\n]*\n){0,5}?"
        r"SIGNAL\s*:\s*(BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)",
        full_text, re.IGNORECASE
    )
    # Also catch "SIGNAL: BUY" standalone and pair with the most-mentioned ticker nearby
    standalone = SIGNAL_VALUE_RE.findall(full_text)

    # Build set of (ticker, new_signal) to check
    to_check = set()
    for ticker, sig in signal_blocks:
        to_check.add((ticker.upper(), sig.upper()))

    # For standalone signals, extract tickers from nearby context
    if standalone and not signal_blocks:
        # Find tickers mentioned in the turn (filter noise words)
        _EXCLUDE = {"BUY","SELL","HOLD","TRIM","EXIT","CUT","ADD","NAV","USD","ILS",
                    "FX","AI","ETF","YoY","QoQ","LOW","HIGH","MED"}
        tickers_in_turn = [t for t in TICKER_RE.findall(full_text) if t not in _EXCLUDE and len(t) >= 2]
        from collections import Counter
        top = Counter(tickers_in_turn).most_common(3)
        for ticker, _ in top:
            for sig in standalone:
                to_check.add((ticker.upper(), sig.upper()))

    has_ack = bool(ACK_RE.search(full_text))

    for ticker, new_signal in to_check:
        prior = _load_prior_signal(ticker)
        if not prior:
            continue
        prior_signal = prior["signal"]
        is_reversal = new_signal in OPPOSITES.get(prior_signal, set())
        if is_reversal and not has_ack:
            out = {
                "decision": "block",
                "reason": (
                    f"Guardrail: this turn signals {new_signal} on {ticker} but the prior signal "
                    f"from {prior['date']} ({prior['file']}) was {prior_signal}. "
                    f"Reversing a signal within 14 days requires an explicit acknowledgement line:\n"
                    f"  SIGNAL CHANGE: {prior_signal} → {new_signal} because <specific new evidence>\n"
                    f"Add this line to the portfolio-manager output before the turn can proceed. "
                    f"If the prior signal is stale due to new material news, cite it specifically."
                ),
            }
            print(json.dumps(out))
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
