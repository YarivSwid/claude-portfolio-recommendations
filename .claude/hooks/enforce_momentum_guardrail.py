#!/usr/bin/env python3
"""Stop hook — warn (or block) on BUY/ADD signals for momentum-extended tickers.

When the assistant emits a BUY or ADD signal for a ticker, this hook calls the
momentum-check skill to see if streak_flag == "extended". If yes, and the
assistant's output does NOT contain a "MOMENTUM OVERRIDE: <reason>" line,
the turn gets a warning (or, in blocking mode, is blocked).

Default mode is "warn" — emits a stderr warning but lets the turn pass.
Set MOMENTUM_GUARDRAIL_MODE=block in the environment (or edit MODE below)
once you've watched the warnings for a few runs and are confident the
thresholds are right.

The skill ships with the hook in WARN mode for the first 3+ runs so the
user can sanity-check the metrics before any blocking behavior kicks in.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOMENTUM_SCRIPT = ROOT / ".claude" / "skills" / "momentum-check" / "scripts" / "momentum.py"

MODE = os.environ.get("MOMENTUM_GUARDRAIL_MODE", "warn")  # "warn" | "block"

# Same signal regex family as the other enforcement hooks. Captures BUY / ADD
# signals from prose or tables. We deliberately do NOT flag SELL / TRIM / HOLD /
# WATCH — momentum extension only matters for new buying.
BUY_LINE_RE = re.compile(
    r"(?:\*{0,2}\s*(?:signal|recommendation|action|verdict|trade\s*signal)\s*\*{0,2}\s*[:=]\s*\*{0,2}\s*"
    r"(BUY|ADD|STRONG_BUY)\b"
    r"|\|\s*\*{2}(BUY|ADD|STRONG_BUY)\*{2}\s*\|)",
    re.IGNORECASE,
)
TICKER_NEAR_BUY_RE = re.compile(
    r"\b([A-Z]{1,5}(?:[.-][A-Z]{1,3})?)\b.{0,80}?\b(?:BUY|ADD|STRONG_BUY)\b"
    r"|\b(?:BUY|ADD|STRONG_BUY)\b.{0,400}?\b([A-Z]{1,5}(?:[.-][A-Z]{1,3})?)\b",
    re.DOTALL,
)
OVERRIDE_RE = re.compile(r"MOMENTUM\s+OVERRIDE\s*:", re.IGNORECASE)

# Tickers we never flag (cash-equivalents, broad index ETFs the user buys
# routinely). Add more as needed.
SAFE_TICKERS = {"SPY", "VOO", "IVV", "BIL", "SHV", "SHY", "AGG", "BND"}

# Common false-positive English words that match the ticker regex.
SKIP_WORDS = {
    "BUY", "SELL", "HOLD", "TRIM", "ADD", "EXIT", "CUT", "WATCH",
    "STRONG", "WEAK", "MODERATE", "BULL", "RISK", "MANAGER", "OFFICER",
    "AI", "ML", "API", "URL", "JSON", "CSV", "USD", "ILS", "TASE",
    "ETF", "NAV", "ATH", "RSI", "DMA", "FX", "PE", "EPS", "YOY", "QOQ",
    "CEO", "CFO", "CTO", "IPO", "SEC", "SPY", "OK",
}


def _extract_buy_tickers(text: str) -> list[str]:
    """Find tickers that appear within ~80 chars of a BUY/ADD/STRONG_BUY token."""
    found: list[str] = []
    for m in TICKER_NEAR_BUY_RE.finditer(text):
        t = (m.group(1) or m.group(2) or "").upper()
        if not t or t in SKIP_WORDS or t in SAFE_TICKERS:
            continue
        if len(t) < 2:
            continue
        if t not in found:
            found.append(t)
    return found


def _check_momentum(tickers: list[str]) -> dict:
    if not tickers:
        return {}
    try:
        proc = subprocess.run(
            ["python3", str(MOMENTUM_SCRIPT)],
            input=json.dumps({"tickers": tickers}),
            capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        )
        if proc.returncode != 0:
            return {}
        return json.loads(proc.stdout).get("results", {})
    except Exception:
        return {}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0

    transcript = payload.get("transcript") or payload.get("output") or ""
    if isinstance(transcript, list):
        transcript = "\n".join(str(p) for p in transcript)

    if not BUY_LINE_RE.search(transcript):
        return 0

    if OVERRIDE_RE.search(transcript):
        return 0

    tickers = _extract_buy_tickers(transcript)
    if not tickers:
        return 0

    metrics = _check_momentum(tickers)
    extended = []
    for t, m in metrics.items():
        if not m or "error" in m:
            continue
        if m.get("streak_flag") == "extended":
            extended.append((t, m.get("flags", []), m.get("rsi_14"),
                             m.get("pct_vs_200dma"), m.get("consecutive_up_days")))

    if not extended:
        return 0

    msg_lines = ["", "=" * 60,
                 "MOMENTUM GUARDRAIL — extended-rally BUY detected",
                 "=" * 60]
    for t, flags, rsi, pct200, up_days in extended:
        rsi_s = f"{rsi:.1f}" if rsi is not None else "?"
        pct_s = f"{pct200*100:+.1f}%" if pct200 is not None else "?"
        msg_lines.append(
            f"  {t}: flags={flags} | RSI(14)={rsi_s} | "
            f"vs 200DMA={pct_s} | up_streak={up_days}"
        )
    msg_lines.append("")
    msg_lines.append(
        "These names are momentum-extended (RSI≥75, >30% above 200DMA, "
        "or 7+ consecutive up days). Buying here means chasing the rally."
    )
    msg_lines.append(
        "If you genuinely want to BUY despite this, add a line to your output:"
    )
    msg_lines.append(
        "  MOMENTUM OVERRIDE: <ticker> — <why this is still the right entry>"
    )
    msg_lines.append("=" * 60)

    print("\n".join(msg_lines), file=sys.stderr)

    if MODE == "block":
        # Stop hook returns exit 2 to block the turn (per Claude Code Stop-hook
        # contract — same convention used by enforce_signal_reversal.py).
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
