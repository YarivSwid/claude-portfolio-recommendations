---
description: Run the report-improvement chain — stock-analyst + frontend-developer in parallel, then report-reviewer for synthesis. Writes ranked proposals to research/daily/<today>/report-improvements.json. Does NOT mutate the report, the dashboard, or any agent specs — the user reviews the JSON and the main session implements the approved items afterward.
allowed-tools: Read, Bash, Glob, Grep, Task, Write
---

# /improve-report

**Purpose:** Three-agent chain that proposes ways to make the daily report better-looking and less noisy. Read-only against the report itself. Outputs proposals to `research/daily/<today>/report-improvements.json` for user approval.

## Steps

1. **Locate the most recent report.** Run `find research/daily -name "report.md" | sort -r | head -1`. If empty, abort with the message: *"No daily report found. Generate one first via the dashboard's ▶ Generate Daily Report button, or run /daily-report."*

2. **Read context for the agents** (so each agent gets identical grounding):
   - `research/user-views.md`
   - `CLAUDE.md` (specifically the "Daily report quality rules" and "What NOT to do" sections)
   - The report.md found in step 1
   - The most recent prior `research/daily/*/report-improvements.json` if one exists, so the chain doesn't re-propose what the user has already rejected

3. **Launch `stock-analyst` and `frontend-developer` IN PARALLEL** via the Task tool (single message, two tool calls). Pass each agent:
   - The full path to the report.md being reviewed
   - The path to `research/user-views.md`
   - The path to the prior improvements JSON if one exists
   - The user-confirmed default noise targets (these are encoded in the agent specs but pass them again as a sanity check):
     1. Raw per-holding data table → collapsible
     2. Inline URL citations → footnoted
     3. `data_as_of` / `Sources:` headers → consolidated footer
     4. Hook-internal warnings → collapsible "Workbench notes"

4. **Validate each agent's output.** Each should return a single JSON object. If either is malformed, log the raw output to `research/daily/<today>/improve-debug.log` and continue with whichever is well-formed. If both are malformed, abort with the raw outputs in the error message.

5. **Launch `report-reviewer`** with both validated outputs concatenated into the user message. The reviewer ranks, rejects, surfaces tensions, and emits the final JSON.

6. **Write the consolidated proposal** to `research/daily/<today>/report-improvements.json` with this shape:
   ```json
   {
     "generated_at": "<ISO-8601 with seconds>",
     "report_reviewed": "research/daily/<date>/report.md",
     "analyst_proposals":  "<full JSON from stock-analyst>",
     "frontend_proposals": "<full JSON from frontend-developer>",
     "reviewer":           "<full JSON from report-reviewer>"
   }
   ```
   The dashboard's `/report/improve/result` endpoint reads this file directly.

7. **Print a one-line success summary** to stdout in this format (the dashboard polls for completion, but a stdout summary helps debugging):
   `improve-report: wrote N proposals (ranked: M, rejected: K) to research/daily/<today>/report-improvements.json`

## Hard rules

- Do NOT modify the report.md being reviewed.
- Do NOT modify `dashboard.py`, any agent spec, or any slash command spec — your output is a proposal file, not code.
- Do NOT emit BUY/SELL/HOLD trade signals in the chain output. Quoting them from the report being reviewed is fine.
- Do NOT skip the parallel-launch in step 3 — running the analyst and frontend serially wastes wall time and they have no shared state to coordinate.

## Output

A single JSON file at `research/daily/<today>/report-improvements.json` plus a one-line stdout summary. No prose explanation in stdout beyond that line.

## After this command finishes

The user reviews the JSON in the dashboard's Report tab (under the new ✨ panel) and decides which proposals to apply. When they're ready, they ask the main Claude session to "apply the report improvements," and that session reads the JSON and executes the writes — this command does NOT do that itself.
