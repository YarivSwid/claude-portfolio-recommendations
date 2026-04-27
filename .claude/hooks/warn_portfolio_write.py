#!/usr/bin/env python3
"""PreToolUse hook on Write|Edit — warn when writing into portfolio/.
Non-blocking; surfaces a reminder so the assistant confirms with the user
before mutating canonical portfolio data outside the parse skill."""
import json
import sys

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input = payload.get("tool_input", {}) or {}
    path = tool_input.get("file_path", "") or tool_input.get("path", "") or ""
    if "/portfolio/" in path:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    "Writing to portfolio/. Only portfolio-parse should write here "
                    "automatically. Confirm this is a user-requested edit."
                ),
            }
        }
        print(json.dumps(out))
        sys.exit(0)

    sys.exit(0)

if __name__ == "__main__":
    main()
