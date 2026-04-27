#!/usr/bin/env python3
"""Stop hook — block the turn if it contains a BUY/SELL/HOLD trade
recommendation without all three required subagents having been invoked:
  - risk-officer (bear case)
  - bull-officer (bull case)
  - portfolio-manager (profile-aware synthesis with bullish lean)

This enforces the 3-agent architecture: every trade signal gets stress-tested
on both sides (bull + bear) AND synthesized by the portfolio-manager against
the user's profile and current market regime.

Scope: only the CURRENT turn (from the most recent user message to the end).
Past turns' signals are not re-checked — past advice already lived through
whatever guardrails were active at the time.

We scan the current-turn slice of the transcript for:
  - any of BUY/SELL/HOLD tokens in assistant text
  - any Agent/Task tool call with subagent_type='risk-officer'
  - any Agent/Task tool call with subagent_type='bull-officer'
If a signal is present in this turn and either officer is absent from
this turn, we block with a specific message telling the assistant which
one is missing.
"""
import json
import re
import sys
from pathlib import Path

# A real trade recommendation requires ALL THREE simultaneously:
#   1. An explicit recommendation marker with a signal token
#      (e.g., "**Signal: BUY**", "Recommendation: SELL", "Action: TRIM")
#   2. A Confidence: 0.X field (per CLAUDE.md rule #2)
#   3. A data_as_of: stamp (per CLAUDE.md rule #2)
# Meta-discussion may cite skill output with Confidence + data_as_of but
# will NOT also carry an explicit signal marker. This triple-check avoids
# false positives from configuration/help discussions.
SIGNAL_MARKER_RE = re.compile(
    r"(?:\*{0,2}\s*(?:signal|recommendation|action|verdict|trade\s*signal)\s*\*{0,2}\s*[:=]\s*\*{0,2}\s*"
    r"(?:BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\b"
    r"|"
    # Also catch table-cell style: "| **BUY** |" or "| Signal | **BUY** |"
    r"\|\s*\*{2}(?:BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD)\*{2}\s*\|"
    r")",
    re.IGNORECASE,
)
CONFIDENCE_RE = re.compile(r"\b[Cc]onfidence\s*[:=]\s*\*{0,2}\s*(weak|moderate|strong|severe|0?\.\d+)", re.MULTILINE)
DATA_AS_OF_RE = re.compile(r"\bdata_as_of\s*[:=]", re.IGNORECASE | re.MULTILINE)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        sys.exit(0)

    # Load all transcript entries
    entries = []
    try:
        with open(transcript_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        sys.exit(0)

    # Find the index of the most recent user message.
    # Everything from that index onward is "the current turn."
    last_user_idx = 0
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        msg = entry.get("message", {}) or {}
        if msg.get("role") == "user":
            # Skip tool_result messages (those are "user" role but not real user input)
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

    current_turn = entries[last_user_idx:]

    has_signal = False
    has_risk_officer = False
    has_bull_officer = False
    has_portfolio_manager = False
    has_web_fetch = False
    has_web_search = False

    for entry in current_turn:
        msg = entry.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "") or ""
                if (SIGNAL_MARKER_RE.search(text)
                        and CONFIDENCE_RE.search(text)
                        and DATA_AS_OF_RE.search(text)):
                    has_signal = True
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                if tool_name in ("Task", "Agent"):
                    ti = block.get("input", {}) or {}
                    sub = ti.get("subagent_type")
                    if sub == "risk-officer":
                        has_risk_officer = True
                    elif sub == "bull-officer":
                        has_bull_officer = True
                    elif sub == "portfolio-manager":
                        has_portfolio_manager = True
                elif tool_name == "WebFetch":
                    has_web_fetch = True
                elif tool_name == "WebSearch":
                    has_web_search = True

    if has_signal:
        missing = []
        if not has_risk_officer:
            missing.append("risk-officer (bear case)")
        if not has_bull_officer:
            missing.append("bull-officer (bull case)")
        if not has_portfolio_manager:
            missing.append("portfolio-manager (synthesis)")
        if missing:
            out = {
                "decision": "block",
                "reason": (
                    "Guardrail: this turn contains a BUY/SELL/HOLD recommendation "
                    f"but the following required subagent(s) were NOT invoked THIS TURN: "
                    f"{', '.join(missing)}. Invoke each missing officer to produce "
                    "their respective case, then re-issue the recommendation with "
                    "confidence + data_as_of + sources. Both officers should be "
                    "called in parallel when the signal is first formed."
                ),
            }
            print(json.dumps(out))
            sys.exit(0)

        # Primary-source audit: bull + bear officers must have fetched real sources.
        # If both officers ran but neither WebFetch nor WebSearch was called this turn,
        # the theses were written from model memory — block it.
        if has_bull_officer and has_risk_officer and not has_web_fetch and not has_web_search:
            out = {
                "decision": "block",
                "reason": (
                    "Guardrail: bull-officer and risk-officer both ran this turn but "
                    "neither WebFetch nor WebSearch was called. Per agent specs, every "
                    "thesis requires ≥1 primary source fetch (earnings release, 10-Q, IR, "
                    "credible press). If real sources were unavailable, the officers must "
                    "return INSUFFICIENT DATA rather than writing from model memory. "
                    "Re-run with actual primary-source research or return INSUFFICIENT DATA."
                ),
            }
            print(json.dumps(out))
            sys.exit(0)

    sys.exit(0)

if __name__ == "__main__":
    main()
