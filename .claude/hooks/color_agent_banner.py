#!/usr/bin/env python3
"""PreToolUse:Agent hook — prints a colored banner to the terminal when a subagent starts."""
import json
import sys

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
RED    = "\033[91m"
BLUE   = "\033[94m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"

# subagent_type → (color, emoji, display label, role blurb)
KNOWN = {
    "bull-officer":      (GREEN,  "🟢", "BULL OFFICER",      "primary-source bull case · running in parallel"),
    "risk-officer":      (RED,    "🔴", "RISK OFFICER",      "primary-source bear case · running in parallel"),
    "portfolio-manager": (BLUE,   "🔵", "PORTFOLIO MANAGER", "synthesizing both cases → signal + lean"),
    "orchestrator":      (YELLOW, "🟡", "ORCHESTRATOR",      "routes · parallelizes · enforces guardrails"),
    "Explore":           (CYAN,   "🔍", "EXPLORE",           "codebase exploration"),
    "general-purpose":   (CYAN,   "🤖", "GENERAL PURPOSE",   "multi-step research"),
}

WIDTH = 62

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool_input  = payload.get("tool_input", {})
    subagent    = tool_input.get("subagent_type", "") or ""
    description = (tool_input.get("description", "") or "").strip()

    color, emoji, label, role = KNOWN.get(
        subagent,
        (CYAN, "🤖", subagent.upper() if subagent else "AGENT", description or "subagent"),
    )

    # If there's a description (task summary), show it instead of the generic role
    display_role = description if description else role

    title    = f" {emoji}  {label} "
    pad      = max(0, WIDTH - len(title))
    left_pad = pad // 2
    right_pad = pad - left_pad

    line1 = f"{'─' * left_pad}{title}{'─' * right_pad}"
    line2 = f"  {display_role}"

    sys.stderr.write(f"\n{color}{BOLD}{line1}{RESET}\n{DIM}{line2}{RESET}\n")
    sys.stderr.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
