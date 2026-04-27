#!/usr/bin/env python3
"""UserPromptSubmit hook — inject today's date and a citation reminder
as additionalContext visible to the model this turn.
"""
import datetime as dt
import json
import sys

def main():
    today = dt.date.today().isoformat()
    msg = (
        f"[Workbench context] Today is {today}. "
        "All numeric claims must cite a skill output or a URL. "
        "Use dual-currency (ILS + USD) for any monetary figure. "
        "If recommending BUY/SELL/HOLD, invoke the risk-officer subagent."
    )
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }
    }
    print(json.dumps(out))
    sys.exit(0)

if __name__ == "__main__":
    main()
