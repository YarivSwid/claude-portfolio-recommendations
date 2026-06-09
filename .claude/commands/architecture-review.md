---
description: Multi-reviewer architecture review of the workbench. Spawns three independent reviewer subagents (stock-analyst, equity-research-expert, system-architect) in parallel, then a cross-pollination phase where each reviews the other two's outputs, then a manager subagent that synthesizes everything into a single markdown report, then renders to a standalone HTML at research/architecture-review/<TS>/report.html.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Task
---

# /architecture-review

Multi-reviewer architecture review. Three independent reviewers + cross-pollination + manager synthesis + HTML render.

## Steps

1. **Compute timestamp and create directory:**
   ```bash
   TS=$(date +%Y-%m-%dT%H-%M)
   OUT_DIR="research/architecture-review/$TS"
   mkdir -p "$OUT_DIR"
   echo "Output directory: $OUT_DIR"
   ```

2. **Phase 1 — parallel reviews.** Spawn 3 reviewer subagents in parallel via the Task tool, single message with three Task calls:
   - subagent_type: `stock-analyst-reviewer` (model: opus / inherit recommended)
   - subagent_type: `equity-research-expert` (model: sonnet or composer-2.5-fast for diversity)
   - subagent_type: `system-architect` (model: opus / inherit recommended)

   Pass `$OUT_DIR` to each via the invocation prompt so they know where to write. Each writes `review-<role>.md` to that dir.

3. **Phase 2 — cross-pollination.** After all 3 Phase 1 reviews land on disk, spawn 3 more subagents in parallel. Each cross-reviewer reads the other two reviewers' outputs and writes a "where I agree / where I push back / what I'd add" addendum to `cross-review-<role>.md`. Same subagent_type as Phase 1 but with a different prompt:
   > "You wrote `review-<role>.md`. Now read the other two reviewers' files at `review-<other-role-a>.md` and `review-<other-role-b>.md`, and write a 1-page addendum at `cross-review-<role>.md` covering: (a) where you agree with them and they sharpened your point, (b) where you genuinely disagree and why, (c) what you'd add that they missed."

4. **Phase 3 — manager synthesis.** Spawn the `architecture-review-manager` subagent (model: opus / inherit). It reads all 6 documents and writes `manager-synthesis.md`.

5. **Phase 4 — render HTML.** Run the render script:
   ```bash
   python3 scripts/render_architecture_review.py "$OUT_DIR"
   ```
   This produces `$OUT_DIR/report.html` — standalone, embedded CSS, opens in any browser.

6. **Verify and report path.** Confirm `report.html` exists and is non-empty, then print its absolute path so the user can open it.

## Notes

- All 3 reviewer subagents have hard rules: don't emit BUY/SELL/HOLD on individual tickers (architecture review, not per-ticker advice), don't invoke other subagents, write the file to disk.
- The Stop hooks (`enforce_risk_officer.py`) may complain if a reviewer accidentally emits a signal — that's a bug in the reviewer prompt, not the user's intent.
- The HTML is self-contained — no CDN, no external CSS. Should open offline.
- Re-runnable: every invocation creates a new timestamped directory. Old reviews are preserved.
