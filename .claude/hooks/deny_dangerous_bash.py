#!/usr/bin/env python3
"""PreToolUse hook for Bash — deny destructive and exfil commands."""
import json
import re
import sys

DENY_PATTERNS = [
    (r"\brm\s+-rf?\s+/", "rm -rf on absolute path"),
    (r"\bgit\s+push\b", "git push (this project isn't git-backed by design)"),
    (r"\bcurl\b[^|;&]*-X\s*(POST|PUT|DELETE)", "curl write to network"),
    (r"\bwget\s", "wget (use yfinance or cached data)"),
    (r"(interactivebrokers|ibkr|robinhood|etoro|psagot|discount|poalim|leumi|hapoalim|alpaca)\.",
     "broker domain"),
    (r"\bpip\s+install[^|]*--target", "pip install outside project"),
    (r":\(\)\s*\{", "fork bomb"),
]

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    cmd = payload.get("tool_input", {}).get("command", "") or ""
    for pat, reason in DENY_PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Blocked by guardrail: {reason}",
                }
            }
            print(json.dumps(out))
            sys.exit(0)

    sys.exit(0)

if __name__ == "__main__":
    main()
