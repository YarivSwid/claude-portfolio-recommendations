#!/usr/bin/env python3
"""Stop hook — block a turn that silently reverses a signal from the last 14 days.

When the assistant emits a BUY/SELL/HOLD/TRIM/REDUCE/STRONG_BUY signal for a
ticker that already has a signal file in research/signals/ from within 14 days,
the turn must contain an explicit acknowledgement line:
  - "SIGNAL CHANGE: <prior> → <new> because ..." (reversal)
  - "SIGNAL CONFIRMED: still <signal> because ..." (confirmation)

If a reversal is detected without that line, the turn is blocked.

Only fires when the current turn contains a real trade signal (same triple-check
as enforce_risk_officer.py: signal marker + confidence + data_as_of).

Signal vocabulary, regex, and reversal-opposites map all come from the canonical
scripts/lib/signal_patterns module — see Step 3 of the 2026-05-28 architecture
fix batch. Pre-Step-5 the regex here was a local copy missing KEEP / REDUCE /
STRONG BUY tokens, so broad-review signals slipped past reversal detection.
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT        = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _log import emit, emit_error  # noqa: E402

HOOK_NAME = "enforce_signal_reversal"

from lib.signal_patterns import (
    SIGNAL_MARKER_RE,
    OPPOSITES,
    SIGNAL_REVERSAL_ACK_RE as ACK_RE,
    find_signals,
    normalize_signal,
)

SIGNALS_DIR = ROOT / "research" / "signals"

CONFIDENCE_RE = re.compile(r"\b[Cc]onfidence\s*[:=]\s*\*{0,2}\s*(weak|moderate|strong|0?\.\d+)", re.MULTILINE)
DATA_AS_OF_RE = re.compile(r"\bdata_as_of\s*[:=]", re.IGNORECASE | re.MULTILINE)


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

    # Find current turn (most recent real user message onward).
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

    # Collect all assistant text from current turn.
    full_text = ""
    for entry in current_turn:
        msg = entry.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "text":
                full_text += block.get("text", "") + "\n"

    # Only proceed if this turn contains a real trade signal (triple-check
    # matches enforce_risk_officer.py).
    if not (SIGNAL_MARKER_RE.search(full_text)
            and CONFIDENCE_RE.search(full_text)
            and DATA_AS_OF_RE.search(full_text)):
        sys.exit(0)

    # Canonical extraction: every (ticker, signal) pair via signal_patterns.
    # Pre-Step-5 this hook had its own per-line regex that missed broad-review
    # vocabulary (KEEP / REDUCE / STRONG BUY). Now uses find_signals which
    # handles the per-holding row format ("TEVA — REDUCE — ...") and the
    # table-cell format ("| TEVA | $X | **REDUCE** | ...").
    to_check: set[tuple[str, str]] = set()
    for ticker, sig in find_signals(full_text):
        to_check.add((ticker.upper(), normalize_signal(sig)))

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
            emit(HOOK_NAME, "block", reason=f"unack reversal {prior_signal}->{new_signal} on {ticker}")
            print(json.dumps(out))
            sys.exit(0)

    emit(HOOK_NAME, "pass")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _e:
        emit_error(HOOK_NAME, _e)
        sys.exit(0)
