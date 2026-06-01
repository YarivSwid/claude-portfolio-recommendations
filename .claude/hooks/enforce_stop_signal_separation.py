#!/usr/bin/env python3
"""Stop hook — STOP-SIGNAL FIREWALL.

Enforces CLAUDE.md "What NOT to do" rule:
    "Derive REDUCE/SELL/EXIT from stop status (Chandelier trigger, 200DMA
     break on a single stock) alone."

A negative signal (REDUCE/TRIM/CUT/SELL/EXIT) emitted on the same line as
stop-language vocabulary (Chandelier, ATR(14), trailing stop, 200/150/50DMA
break, triggered, breached, broke below) is presumed to be cited from the
stop and is blocked unless a fundamental cause appears nearby (±3 lines)
or an explicit STOPS_OVERRIDE acknowledgement is present.

Modes (controlled by env STOPS_FIREWALL_MODE, default "warn"):
    warn  : print structured warning to stderr, exit 0 (turn passes)
    block : emit {"decision": "block", "reason": "..."} JSON to stdout

Designed for a soft-launch: start in WARN to surface false-positives in
real broad-review output, then promote to BLOCK once Step 6 (template
fix) has landed and a clean turn has been observed.

Scope: only the CURRENT turn (from the most recent real user message to
the end), matching enforce_risk_officer.py's slicing convention so the
hooks behave consistently.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.signal_patterns import (
    SIGNAL_MARKER_RE,
    STOP_LANGUAGE_RE,
    NEGATIVE_SIGNAL_TOKENS,
)
from _log import emit, emit_error  # noqa: E402

HOOK_NAME = "enforce_stop_signal_separation"

MODE = (os.environ.get("STOPS_FIREWALL_MODE") or "warn").strip().lower()
if MODE not in {"warn", "block"}:
    MODE = "warn"

FUNDAMENTAL_CAUSE_RE = re.compile(
    r"\b("
    r"revenue|earnings|guide|guidance|EPS|margin(?:s)?|"
    r"growth|grow(?:s|ing|n)?|"
    r"thesis|fundamentals?|"
    r"cash[\s-]?flow|FCF|free[\s-]cash[\s-]flow|"
    r"miss(?:ed|es|ing)?|beat(?:s|en)?|"
    r"competit(?:ion|or|ive)|"
    r"regulat(?:ory|ion|or)|"
    r"bankrupt(?:cy)?|insolven(?:cy|t)|"
    r"debt|leverage|covenant|"
    r"buyback|dividend|payout|"
    r"CEO|CFO|management|board|"
    r"spend(?:ing)?|capex|opex|"
    r"acquisition|M&A|merger|spin[\s-]?off|"
    r"restructur(?:ing|e|ed)|layoff|headcount|"
    r"writedown|write[\s-]down|impair(?:ment|ed)|goodwill|"
    r"recession|macro(?:economic)?|inflation|tariff|sanction|"
    r"guide\s+cut|cut(?:s)?\s+(?:to|the)\s+guide|lowered\s+guidance|"
    r"share\s+loss|market\s+share|share\s+gain|"
    r"churn|retention|ARR|MRR|NRR|"
    r"FDA|approval|trial|recall|litigation|lawsuit|"
    r"production|supply\s+chain|inventory|demand|"
    r"contract|customer|partner(?:ship)?|"
    r"insider\s+(?:sale|sell|buy)|insider\s+selling"
    r")\b",
    re.IGNORECASE,
)

OVERRIDE_RE = re.compile(r"^\s*STOPS[_\s]OVERRIDE\s*:", re.IGNORECASE | re.MULTILINE)

NEAR_LINES_ABOVE = 2
NEAR_LINES_BELOW = 0


def _negative_with_stop_on_line(line: str) -> str | None:
    """Return the negative token if `line` has BOTH a stop-language match AND
    a negative-signal token (bare or markdown-bold). Else None.
    """
    if not STOP_LANGUAGE_RE.search(line):
        return None
    for token in NEGATIVE_SIGNAL_TOKENS:
        if re.search(r"\b" + re.escape(token) + r"\b", line):
            return token
        if re.search(r"\*{2}\s*" + re.escape(token) + r"\s*\*{2}", line):
            return token
    return None


def _has_cause_near(lines: list[str], idx: int) -> bool:
    """Look on the offending line itself + NEAR_LINES_ABOVE lines above.

    Deliberately asymmetric: causes for the next holding's signal often appear
    BELOW the current line and would otherwise leak across blocks (the AAPL
    'thesis intact' contaminating TEVA's REDUCE — see contamination self-test).
    """
    lo = max(0, idx - NEAR_LINES_ABOVE)
    hi = min(len(lines), idx + NEAR_LINES_BELOW + 1)
    block = "\n".join(lines[lo:hi])
    return bool(FUNDAMENTAL_CAUSE_RE.search(block))


def _scan_text(text: str) -> list[dict[str, object]]:
    """Return a list of offenders. Each offender is:
       {'line_no': int, 'line': str, 'token': str}
    """
    if OVERRIDE_RE.search(text):
        return []
    lines = text.splitlines()
    out: list[dict[str, object]] = []
    for i, line in enumerate(lines):
        token = _negative_with_stop_on_line(line)
        if token is None:
            continue
        if _has_cause_near(lines, i):
            continue
        out.append({"line_no": i + 1, "line": line.strip(), "token": token})
    return out


def _load_transcript_slice(transcript_path: str) -> list[str]:
    """Return assistant text blocks from the CURRENT turn (since most-recent
    real user message). Matches enforce_risk_officer.py's slicing logic.
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
        return []

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

    texts: list[str] = []
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
                    texts.append(t)
    return texts


def _format_block_reason(offenders: list[dict[str, object]]) -> str:
    lines = [
        "STOP-SIGNAL FIREWALL — negative signal(s) cited from stop-language alone.",
        "",
        "CLAUDE.md 'Daily report quality rules' 3 & 7 and 'What NOT to do' forbid",
        "deriving REDUCE/TRIM/CUT/SELL/EXIT from stops alone. Stops are technical",
        "reference levels (Trend & Stops Reference block at the bottom). Negative",
        "signals require a fundamental cause: revenue/guide/margin/thesis break/",
        "competitive/regulatory/management/etc.",
        "",
        f"Offending line(s) (this turn, {len(offenders)} total):",
    ]
    for o in offenders[:10]:
        lines.append(f"  L{o['line_no']:>4}  [{o['token']}]  {o['line']}")
    if len(offenders) > 10:
        lines.append(f"  ... and {len(offenders) - 10} more")
    lines.extend([
        "",
        "To unblock, rewrite each offending line with ONE of:",
        "  (a) Add a fundamental cause to the same line. Example:",
        "      'TEVA - REDUCE - Q4 revenue miss and 2026 guide cut; Chandelier",
        "       stop at $13.80 confirms the technical break.'",
        "  (b) Change the signal to KEEP/HOLD and move the stop reference to",
        "      the bottom 'Trend & Stops Reference' block.",
        "  (c) If the fundamental cause is real but my keyword list missed it,",
        "      add a line:  STOPS_OVERRIDE: <one-sentence specific cause>",
    ])
    return "\n".join(lines)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        emit(HOOK_NAME, "skip", reason=f"stdin parse failed: {e}")
        sys.exit(0)

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        emit(HOOK_NAME, "skip", reason="no transcript")
        sys.exit(0)

    texts = _load_transcript_slice(transcript_path)
    if not texts:
        emit(HOOK_NAME, "skip", reason="empty current turn")
        sys.exit(0)

    offenders: list[dict[str, object]] = []
    for t in texts:
        offenders.extend(_scan_text(t))
    if not offenders:
        emit(HOOK_NAME, "pass")
        sys.exit(0)

    msg = _format_block_reason(offenders)
    detail = f"{len(offenders)} offender(s) mode={MODE}"

    if MODE == "block":
        emit(HOOK_NAME, "block", reason=detail)
        print(json.dumps({"decision": "block", "reason": msg}))
        sys.exit(0)

    emit(HOOK_NAME, "warn", reason=detail)
    sys.stderr.write("\n=== STOPS-FIREWALL [WARN MODE, would have blocked] ===\n")
    sys.stderr.write(msg + "\n")
    sys.stderr.write("=== set STOPS_FIREWALL_MODE=block to enforce ===\n\n")
    sys.exit(0)


def _self_test() -> None:
    """Run with `python3 .claude/hooks/enforce_stop_signal_separation.py --selftest`"""
    cases: list[tuple[str, str, int, str]] = [
        (
            "stop-justified REDUCE no cause -> 1 offender",
            "TEVA - REDUCE - Chandelier 3xATR stop triggered at $13.80\n",
            1, "REDUCE",
        ),
        (
            "stop-justified REDUCE with fundamental on same line -> 0",
            "TEVA - REDUCE - Q4 revenue miss; Chandelier 3xATR stop triggered at $13.80\n",
            0, "",
        ),
        (
            "stop-justified REDUCE with cause on nearby line -> 0",
            "TEVA fundamentals: Q4 revenue miss and 2026 guide cut.\n"
            "Technical: Chandelier 3xATR triggered at $13.80.\n"
            "TEVA - REDUCE - both fundamentals and stop confirm.\n",
            0, "",
        ),
        (
            "KEEP with stop language -> 0 (informational mention allowed)",
            "TEVA - KEEP - thesis intact; Chandelier stop sits at $13.80 for reference.\n",
            0, "",
        ),
        (
            "STOPS_OVERRIDE escape hatch -> 0",
            "STOPS_OVERRIDE: insider selling cluster from CEO and CFO Q1.\n"
            "TEVA - REDUCE - 200DMA breach + management exit signal.\n",
            0, "",
        ),
        (
            "two REDUCE offenders in same text -> 2",
            "TEVA - REDUCE - Chandelier triggered.\n"
            "AAPL - SELL - 200DMA broken below.\n",
            2, "",
        ),
        (
            "clean fundamental REDUCE no stop language -> 0 (not in scope)",
            "TEVA - REDUCE - Q4 miss and competitive pressure on generics.\n",
            0, "",
        ),
        (
            "STRONG BUY with stop language -> 0 (positive signal not in scope)",
            "NVDA - STRONG BUY - guide raised; Chandelier stop at $880 for risk reference.\n",
            0, "",
        ),
        (
            "cross-holding contamination must NOT save TEVA -> 1 offender",
            "TEVA - REDUCE - Chandelier 3xATR stop triggered at $13.80\n"
            "AAPL - KEEP - thesis intact.\n",
            1, "REDUCE",
        ),
        (
            "cause line above + signal line below -> 0",
            "Q4 revenue miss and 2026 guidance cut.\n"
            "TEVA - REDUCE - Chandelier 3xATR stop triggered at $13.80\n",
            0, "",
        ),
    ]
    failed = 0
    for label, text, expected_count, expected_first_token in cases:
        offs = _scan_text(text)
        if len(offs) != expected_count:
            print(f"FAIL [{label}]: got {len(offs)} expected {expected_count} (offs={offs})")
            failed += 1
            continue
        if expected_count and expected_first_token and offs[0]["token"] != expected_first_token:
            print(f"FAIL [{label}]: first token {offs[0]['token']} expected {expected_first_token}")
            failed += 1
            continue
        print(f"OK   [{label}] -> {len(offs)} offender(s)")

    if failed:
        print(f"\n{failed} test(s) failed")
        raise SystemExit(1)
    print("\nAll self-tests passed.")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _self_test()
    else:
        try:
            main()
        except SystemExit:
            raise
        except BaseException as _e:
            emit_error(HOOK_NAME, _e)
            sys.exit(0)
