"""Shared structured logger for Stop/PreToolUse/PostToolUse hooks.

Every hook should call emit() at the end with one of:
  - pass:        the hook ran and the turn is allowed
  - block:       the hook ran and is blocking the turn (Stop hook exit 2)
  - error:       the hook crashed; the parent should treat the firewall as
                 ineffective for this turn
  - skip:        the hook ran but determined it didn't apply

The log is research/hooks.jsonl, append-only. Each row is one JSON object.
This converts "I think the guardrails are working" into "I can prove which
ones ran and what they decided." Without this, hook exceptions are swallowed
to sys.exit(0) and the firewall vanishes silently.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "research" / "hooks.jsonl"


def emit(hook: str, outcome: str, reason: str = "", extra: dict | None = None) -> None:
    """Append one row to research/hooks.jsonl. Never raises."""
    try:
        row = {
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "hook": hook,
            "outcome": outcome,
            "reason": reason[:500] if reason else "",
        }
        if extra:
            row.update(extra)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        # Logging must never break a hook. Swallow.
        pass


def emit_error(hook: str, exc: BaseException) -> None:
    """Convenience wrapper for the top-level exception handler in each hook."""
    emit(hook, "error", reason=f"{type(exc).__name__}: {exc}", extra={"stderr_hint": True})
    # Also write to stderr so the hook itself stays observable in the terminal.
    try:
        print(f"[{hook}] error: {type(exc).__name__}: {exc}", file=sys.stderr)
    except Exception:
        pass
