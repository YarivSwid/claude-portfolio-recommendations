#!/usr/bin/env python3
"""PostToolUse hook on Bash — if the command invoked a skill script,
append a row to research/daily/<today>/data-freshness.jsonl.
"""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Capture skill name and script filename only — stop at whitespace, quotes, or
# shell punctuation. Previously this was r"([^\s]+)" which captured trailing
# `';` and `',` from shell command shapes like `python3 .../scripts/foo.py' )`.
SKILL_SCRIPT_RE = re.compile(r"\.claude/skills/([^/]+)/scripts/([A-Za-z0-9_.-]+\.py)")


def _read_exit_code(payload: dict) -> int | None:
    """The PostToolUse:Bash payload has shifted shape across Claude Code versions.
    Try every known field name before giving up. None means we genuinely could
    not find an exit code; the caller decides what to do with that.
    """
    tr = payload.get("tool_response", {}) or {}
    if isinstance(tr, dict):
        for k in ("exitCode", "exit_code", "code", "exit_status", "returncode"):
            v = tr.get(k)
            if v is not None:
                return v
        # Some versions nest under .result
        result = tr.get("result")
        if isinstance(result, dict):
            for k in ("exitCode", "exit_code", "code", "returncode"):
                v = result.get(k)
                if v is not None:
                    return v
    # Top-level fallback (rare)
    for k in ("exitCode", "exit_code", "code"):
        v = payload.get(k)
        if v is not None:
            return v
    return None


def _classify_outcome(payload: dict, exit_code: int | None) -> str:
    """Best-effort outcome classification.

    Claude Code's PostToolUse:Bash payload omits exit_code entirely — it only
    provides stdout/stderr/interrupted/isImage/noOutputExpected. We infer:
      - interrupted=True              -> "interrupted"
      - non-empty stderr only          -> "warn" (skill ran, wrote stderr)
      - stdout present, no stderr     -> "ok"
      - empty stdout AND empty stderr -> "empty"
    This is necessarily best-effort; a skill that exits non-zero silently is
    indistinguishable from one that exits 0 silently.
    """
    if exit_code is not None:
        return "ok" if exit_code == 0 else "fail"
    tr = payload.get("tool_response", {}) or {}
    if not isinstance(tr, dict):
        return "unknown"
    if tr.get("interrupted") is True:
        return "interrupted"
    stdout = tr.get("stdout") or ""
    stderr = tr.get("stderr") or ""
    if stdout.strip() and not stderr.strip():
        return "ok"
    if stderr.strip() and not stdout.strip():
        return "fail"
    if stdout.strip() and stderr.strip():
        return "warn"
    return "empty"


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        sys.exit(0)

    cmd = payload.get("tool_input", {}).get("command", "") or ""
    m = SKILL_SCRIPT_RE.search(cmd)
    if not m:
        sys.exit(0)

    today = dt.date.today().isoformat()
    logdir = ROOT / "research" / "daily" / today
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / "data-freshness.jsonl"

    exit_code = _read_exit_code(payload)
    outcome = _classify_outcome(payload, exit_code)
    duration_ms = payload.get("duration_ms")

    entry = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "skill": m.group(1),
        "script": m.group(2),
        # PostToolUse:Bash payload omits exit_code in this Claude Code version;
        # outcome is inferred from stdout/stderr/interrupted. exit_code stays
        # in the schema so future Claude Code versions that surface it Just Work.
        "exit_code": exit_code,
        "outcome": outcome,
        "duration_ms": duration_ms,
    }
    with open(logfile, "a") as f:
        f.write(json.dumps(entry) + "\n")

    sys.exit(0)


if __name__ == "__main__":
    main()
