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
SKILL_SCRIPT_RE = re.compile(r"\.claude/skills/([^/]+)/scripts/([^\s]+)")

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

    entry = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "skill": m.group(1),
        "script": m.group(2),
        "exit_code": payload.get("tool_response", {}).get("exitCode"),
    }
    with open(logfile, "a") as f:
        f.write(json.dumps(entry) + "\n")

    sys.exit(0)

if __name__ == "__main__":
    main()
