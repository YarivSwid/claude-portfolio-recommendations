# How to Use the Workbench

A practical guide to everything we built. Read top-to-bottom the first time; after that use it as a reference.

---

## 1. The big picture

```
    ┌──────────────┐
    │    YOU       │  (ask questions, drop broker files, review briefings)
    └──────┬───────┘
           │
    ┌──────▼──────────────────────────────────────────────┐
    │                   Claude Code                        │
    │     reads CLAUDE.md on every turn (the rulebook)     │
    │     runs hooks (the guardrails)                      │
    └──────┬──────────────────────────────────────────────┘
           │
    ┌──────▼──────────┐        ┌──────────────────────┐
    │  Orchestrator   │──────▶ │  Specialist subagents │
    │  (main analyst) │        │  (only risk-officer   │
    └──────┬──────────┘        │   exists in Phase 1)  │
           │                    └──────────────────────┘
           │
    ┌──────▼──────────────────────────────────┐
    │   Python Skills (deterministic math)    │
    │   portfolio-parse · currency-conversion │
    │   sector-allocation · risk-metrics      │
    └──────┬──────────────────────────────────┘
           │
    ┌──────▼──────────┐
    │ yfinance data   │   (cached daily in .cache/)
    │ + broker files  │
    └─────────────────┘
```

**Rule of thumb:** Claude writes *words*. Skills (Python) compute *numbers*. Hooks *enforce rules*. Subagents bring a *second perspective*.

---

## 2. The four building blocks, in plain English

### 🧠 Subagents (`.claude/agents/*.md`)
A second Claude instance with its own prompt and tool allow-list. The main Claude asks it for something and gets back a focused answer. Ours:
- **`orchestrator`** — your main desk. Reads your ask, runs the right skills, formats the answer, enforces the cite-sources rule. It plays the *bull*.
- **`risk-officer`** — the mandatory *bear*. Forbidden from agreeing. Its job is "what could go wrong."

### 🛠 Skills (`.claude/skills/*/SKILL.md` + `scripts/*.py`)
A named Python tool the assistant can invoke. Each is stdin-JSON → stdout-JSON, does one thing deterministically, and returns a `data_as_of` stamp. Ours:
- **`portfolio-parse`** — Hebrew broker files → `portfolio/positions.json`
- **`currency-conversion`** — today's USDILS rate (cached)
- **`sector-allocation`** — weights by region/currency/sector + concentration (HHI)
- **`risk-metrics`** — Sharpe, Sortino, max drawdown, beta vs SPY & TA-35

### 🛡 Hooks (`.claude/settings.json` + `.claude/hooks/*.py`)
Shell scripts the harness runs on events. They enforce rules *outside* the model's control. Ours:
| Event | Hook | What it does |
|---|---|---|
| `PreToolUse` on Bash | `deny_dangerous_bash.py` | Blocks `rm -rf /`, `git push`, broker domains, etc. |
| `PreToolUse` on Write/Edit | `warn_portfolio_write.py` | Asks before mutating `portfolio/` outside the parse skill |
| `PostToolUse` on Bash | `log_skill_freshness.py` | Appends to `research/daily/<today>/data-freshness.jsonl` |
| `UserPromptSubmit` | `inject_context.py` | Injects today's date + "cite your sources" reminder |
| `Stop` | `enforce_risk_officer.py` | **Blocks any turn with BUY/SELL/HOLD that didn't invoke the risk-officer** |

### ⚡ Slash commands (`.claude/commands/*.md`)
Reusable prompts you invoke with `/`. Ours:
- **`/daily-scan`** — read-only dashboard. NAV, top positions, allocation, risk, flags.

### 📖 Rulebook (`CLAUDE.md`)
Loaded on every turn. Your profile, the floor rules (no crypto adds, no micro-caps, 10% cap), the dual-currency rule, the Learning-note convention.

---

## 3. Your everyday flows

### Flow A — "Show me where I stand" (morning briefing)
```
/daily-scan
```
What happens:
1. Orchestrator checks if `ActivePortfolio.csv.xlsx` is newer than `portfolio/positions.json`. If yes, runs `portfolio-parse` first.
2. Runs `currency-conversion`, `sector-allocation`, `risk-metrics` in parallel.
3. Prints a one-screen briefing (NAV, top 5 positions, allocation, Sharpe/DD/β, flags).
4. Ends with a Learning note.

Important: **this command never recommends BUY/SELL/HOLD**. It's a dashboard.

### Flow B — After you trade
1. In your broker, re-export the portfolio + transactions.
2. Drop the two files at the project root (overwrite the existing ones).
3. Run `/daily-scan` — the parse skill detects the newer mtime, re-parses, and shows a diff against the last snapshot ("added: …, removed: …, qty changed: …").

### Flow C — "What do you think about X?"
Just ask in natural language:
- "What's my exposure to AI?"
- "Am I over-concentrated?"
- "What's my Sharpe vs the S&P?"
- "How did NVDA do over 2 years?"

The orchestrator picks the right skills. You don't have to invoke skills by name.

### Flow D — "Should I sell META?" (triggers the bear)
Any BUY/SELL/HOLD token in the response triggers the **Stop hook**. If the orchestrator didn't call the `risk-officer` subagent this turn, the turn is **blocked** — Claude has to go back, invoke the risk-officer for the bear case, and only then emit the recommendation (with confidence, `data_as_of`, sources, and the disclaimer).

You don't have to do anything. The hook enforces it. That's the guardrail.

### Flow E — Run a skill directly (debug / curiosity)
All skills are plain Python. Run them from the shell:
```bash
# Re-parse portfolio
echo '{}' | python3 .claude/skills/portfolio-parse/scripts/parse.py

# Just the FX rate
echo '{}' | python3 .claude/skills/currency-conversion/scripts/fx.py

# Risk metrics for a single ticker
echo '{"ticker":"NVDA","period":"2y"}' | python3 .claude/skills/risk-metrics/scripts/sharpe_dd.py

# Full allocation breakdown with top 10 positions
echo '{"top_n_positions":10}' | python3 .claude/skills/sector-allocation/scripts/sectors.py
```

---

## 4. What's in each folder

```
ClaudeLearning/
├── CLAUDE.md                      ← rulebook (always read by Claude)
├── README.md                      ← project overview
├── USAGE.md                       ← this file
├── ActivePortfolio.csv.xlsx       ← DROP ZONE: broker snapshot
├── AllStockTransactions.csv.xls   ← DROP ZONE: broker history
│
├── .claude/
│   ├── settings.json              ← hook wiring + permissions
│   ├── agents/                    ← subagent definitions (2 so far)
│   ├── skills/                    ← skills (4 so far)
│   ├── commands/                  ← slash commands (1 so far)
│   └── hooks/                     ← hook scripts (5)
│
├── scripts/
│   └── lib/                       ← shared Python (yfinance cache, FX, schema)
│
├── portfolio/                     ← YOUR DATA — private
│   ├── positions.json             ← canonical current holdings
│   ├── transactions.json          ← full trade history
│   └── snapshots/YYYY-MM-DD.json  ← daily point-in-time copies
│
├── research/                      ← cached analyst outputs
│   └── daily/YYYY-MM-DD/
│       └── data-freshness.jsonl   ← audit log of every skill run
│
├── learning/                      ← your curriculum
│   ├── glossary.md                ← definitions of concepts we use
│   └── concepts-seen.md           ← running list of what you've learned
│
└── .cache/                        ← yfinance + FX cache (safe to delete)
```

---

## 5. The Learning note — your curriculum

Every non-trivial answer ends with a 3-line block like:
```
---
Learning note
Concept: Herfindahl–Hirschman Index (HHI)
Why it mattered here: it's how we summarized your portfolio's concentration — 0.07 = diversified.
Go deeper: learning/glossary.md#hhi
```

- **Each concept is new** (or explicitly deepens a prior one).
- The orchestrator appends it to `learning/concepts-seen.md`.
- Concepts span investing **and** Claude Code ("subagent", "hook" etc. are fair game).

After ~30 days of use, `concepts-seen.md` is a real personalized curriculum. Revisit it.

---

## 6. The guardrails — why you can trust this

- **Never trades.** No tool in this workbench can place an order. Hard rule in `CLAUDE.md` + deny-list in `settings.json`.
- **No recommendation without a bear case.** Stop hook blocks the turn.
- **Numbers must cite a skill or URL.** Rule in `CLAUDE.md`; reinforced by the prompt-submit hook.
- **No new crypto.** Rule in `CLAUDE.md`. (Your existing IBIT holding is described factually but never sized up.)
- **No single position > 10% NAV.** Flagged automatically in sector-allocation output.
- **No micro-caps.** < $300M US / < ₪500M TASE.
- **Data freshness logged.** Every skill call appends to `data-freshness.jsonl`. If cached data is > 24h old, output says so.
- **Math happens in Python, not prose.** LLMs hallucinate numbers; Python doesn't.

---

## 7. When something feels off

| Symptom | Likely cause | Fix |
|---|---|---|
| `/daily-scan` unknown | Commands load at session start | Restart Claude Code in the project folder |
| Skill output has `"warnings": ["no positions"]` | Broker files not parsed yet | Run `portfolio-parse` (or `/daily-scan`, which does it) |
| `rate_ils_per_usd: null` | yfinance network hiccup | Re-run; it'll try again. Don't make decisions on stale FX. |
| Israeli mutual fund shows `Unknown` sector | yfinance doesn't index Israeli fund security numbers | Expected. Covered by `asset_class = mutual_fund` instead. |
| Stale cache | `.cache/yfinance/` keyed by date | Delete `.cache/` to force refresh |
| Recommendation went through without risk-officer | Hook missed it | Check that `.claude/settings.json` has the `Stop` hook wired. Report it — I'll fix. |

---

## 8. What's coming (you approve when ready)

- **Phase 2** — news scan, earnings calendar, macro regime detector, position-sizing skill, `macro-news-scanner` subagent, `/new-idea TICKER`, `/weekly-review`, `/loop` daily cadence.
- **Phase 3** — tech specialist, growth-aggressive specialist, Israel-market specialist, fundamentals analyst, bull/bear researcher debate, trader-memory for outcome tracking.

Use Phase 1 for a week. Then tell me you're ready for Phase 2.

---

## 9. Cheat sheet — copy/paste these to try

```
/daily-scan
```
```
What's my exposure to Big Tech right now?
```
```
What's my Sharpe over the last 2 years?
```
```
Show me the 5-year drawdown profile of NVDA.
```
```
Is my portfolio too concentrated?
```
```
Should I trim META?        ← triggers the risk-officer via Stop hook
```
