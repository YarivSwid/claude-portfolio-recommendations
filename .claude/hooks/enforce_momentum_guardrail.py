#!/usr/bin/env python3
"""Stop hook — warn (or block) on BUY/ADD signals for momentum-extended tickers.

When the assistant emits a BUY or ADD signal for a ticker, this hook calls the
momentum-check skill and reads the `tier` field (normal / yellow / red).

Behavior (Option B — labels inform, never silently filter):
  - Normal: no action.
  - Yellow: always warn (label exists for visibility). MODE governs nothing here.
  - Red:    warn in MODE=warn; block in MODE=block. Override via
            "MOMENTUM OVERRIDE: <reason>" line in the assistant output.

The legacy binary `streak_flag == "extended"` is kept as a backup signal so the
hook still fires if a downstream change to momentum-check ever drops `tier`.

Set MOMENTUM_GUARDRAIL_MODE=block once the warnings have been validated; ships
in "warn" mode by default.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MOMENTUM_SCRIPT = ROOT / ".claude" / "skills" / "momentum-check" / "scripts" / "momentum.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _log import emit, emit_error  # noqa: E402

HOOK_NAME = "enforce_momentum_guardrail"

MODE = os.environ.get("MOMENTUM_GUARDRAIL_MODE", "warn")  # "warn" | "block"

# Same signal regex family as the other enforcement hooks. Captures BUY / ADD
# signals from prose or tables. We deliberately do NOT flag SELL / TRIM / HOLD /
# WATCH — momentum extension only matters for new buying.
_POSITIVE = r"STRONG[\s_]BUY|BUY|ADD"

BUY_LINE_RE = re.compile(
    r"(?:\*{0,2}\s*(?:signal|recommendation|action|verdict|trade\s*signal)\s*\*{0,2}\s*[:=]\s*\*{0,2}\s*"
    r"(" + _POSITIVE + r")\b"
    r"|\|\s*\*{2}(" + _POSITIVE + r")\*{2}\s*\|"
    r"|\*{2}(" + _POSITIVE + r")\*{2})",
    re.IGNORECASE,
)
TICKER_NEAR_BUY_RE = re.compile(
    r"\b([A-Z]{1,5}(?:[.-][A-Z]{1,3})?)\b.{0,80}?\b(?:" + _POSITIVE + r")\b"
    r"|\b(?:" + _POSITIVE + r")\b.{0,400}?\b([A-Z]{1,5}(?:[.-][A-Z]{1,3})?)\b",
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


def _load_current_turn_text(transcript_path: str) -> str:
    """Match the slicing pattern enforce_risk_officer.py uses: collect assistant
    text blocks from the most recent real user message onward.
    """
    entries: list[dict] = []
    try:
        with open(transcript_path, "r") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    entries.append(json.loads(ln))
                except Exception:
                    continue
    except Exception:
        return ""

    last_user_idx = 0
    for i in range(len(entries) - 1, -1, -1):
        msg = entries[i].get("message", {}) or {}
        if msg.get("role") != "user":
            continue
        content = msg.get("content", [])
        is_tool_result = False
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    is_tool_result = True
                    break
        if not is_tool_result:
            last_user_idx = i
            break

    out: list[str] = []
    for entry in entries[last_user_idx:]:
        msg = entry.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                t = block.get("text", "") or ""
                if t:
                    out.append(t)
    return "\n".join(out)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        emit(HOOK_NAME, "skip", reason=f"stdin parse failed: {e}")
        return 0

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        emit(HOOK_NAME, "skip", reason="no transcript")
        return 0
    transcript = _load_current_turn_text(transcript_path)
    if not transcript:
        emit(HOOK_NAME, "skip", reason="empty current turn")
        return 0

    if not BUY_LINE_RE.search(transcript):
        emit(HOOK_NAME, "pass", reason="no BUY/ADD line")
        return 0

    if OVERRIDE_RE.search(transcript):
        emit(HOOK_NAME, "pass", reason="MOMENTUM OVERRIDE present")
        return 0

    tickers = _extract_buy_tickers(transcript)
    if not tickers:
        emit(HOOK_NAME, "pass", reason="no extractable tickers")
        return 0

    metrics = _check_momentum(tickers)
    yellow: list[tuple] = []
    red: list[tuple] = []
    for t, m in metrics.items():
        if not m or "error" in m:
            continue
        tier = (m.get("tier") or "").lower()
        legacy_extended = m.get("streak_flag") == "extended"
        row = (t, m.get("flags", []), m.get("rsi_14"),
               m.get("pct_vs_200dma"), m.get("consecutive_up_days"), tier or "?")
        if tier == "red" or (not tier and legacy_extended):
            red.append(row)
        elif tier == "yellow":
            yellow.append(row)

    if not yellow and not red:
        emit(HOOK_NAME, "pass", reason="no extended tickers")
        return 0

    msg_lines = ["", "=" * 60,
                 "MOMENTUM GUARDRAIL — extended-rally BUY detected",
                 "=" * 60]
    for label, group in (("RED", red), ("YELLOW", yellow)):
        if not group:
            continue
        msg_lines.append(f"[{label}]")
        for t, flags, rsi, pct200, up_days, tier in group:
            rsi_s = f"{rsi:.1f}" if rsi is not None else "?"
            pct_s = f"{pct200*100:+.1f}%" if pct200 is not None else "?"
            msg_lines.append(
                f"  {t}: tier={tier} | RSI(14)={rsi_s} | "
                f"vs 200DMA={pct_s} | up_streak={up_days} | flags={flags}"
            )
    msg_lines.append("")
    msg_lines.append(
        "Yellow = one criterion extended (RSI 70–79, 20–40% above 200DMA, or 7–10 up-days). "
        "Manager should already be half-sizing the starter (portfolio-manager spec)."
    )
    msg_lines.append(
        "Red = severely extended (RSI ≥ 80, >40% above 200DMA, or 11+ up-days). "
        "Manager should be quarter-sizing the starter or waiting for a pullback."
    )
    msg_lines.append(
        "If you genuinely want to BUY a Red-tier name despite this, add a line to your output:"
    )
    msg_lines.append(
        "  MOMENTUM OVERRIDE: <ticker> — <why this is still the right entry>"
    )
    msg_lines.append("=" * 60)

    print("\n".join(msg_lines), file=sys.stderr)

    # Only Red triggers blocking. Yellow is always a warn-only signal.
    if MODE == "block" and red:
        red_syms = ",".join(r[0] for r in red)
        emit(HOOK_NAME, "block", reason=f"red-tier momentum on {red_syms}")
        return 2
    detail = f"yellow={len(yellow)} red={len(red)} mode={MODE}"
    emit(HOOK_NAME, "warn", reason=detail)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as _e:
        emit_error(HOOK_NAME, _e)
        sys.exit(0)
