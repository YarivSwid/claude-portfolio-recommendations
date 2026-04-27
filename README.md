# Investing Workbench

A Claude-Code-powered personal investing desk: drop in your broker exports, fill in a profile file, and get grounded analysis on your portfolio with a mandatory bull/bear/manager debate before any BUY/SELL/HOLD recommendation.

The workbench is **profile-driven** — none of your data, holdings, or risk preferences are hardcoded. Each user fills in their own `research/user-views.md` and drops their own broker exports into the project root. The agents adapt accordingly.

**Status:** Phase 1+ — foundation, guardrails, and three-agent decision pipeline.

## What's here

```
CLAUDE.md                            # project rules (always read by Claude Code)
ActivePortfolio.csv.xlsx             # drop zone — your broker holdings export (gitignored)
AllStockTransactions.csv.xls         # drop zone — your broker transaction history (gitignored)
.claude/
  settings.json                      # hooks enforcing the guardrails
  agents/                            # bull-officer, risk-officer, portfolio-manager, orchestrator
  skills/                            # portfolio-parse + analytics + scanner skills
  commands/                          # /daily-scan and other slash commands
scripts/lib/                         # yfinance wrapper, FX, schema, guardrails
portfolio/                           # parsed broker data — gitignored, never leaves your machine
research/
  user-views.example.md              # template — copy to user-views.md
  user-views.md                      # YOUR profile (gitignored)
  daily/                             # your daily research outputs (gitignored)
  signals/                           # your saved per-ticker signals (gitignored)
learning/                            # glossary + concepts list
```

## One-time setup

```bash
# 1. Install Python deps
pip3 install -r scripts/requirements.txt

# 2. Create your profile from the template
cp research/user-views.example.md research/user-views.md
# Edit research/user-views.md and fill in your age, country, risk band,
# horizon, and any stated views about the market.

# 3. Drop your broker exports at the project root
#    Default expected filenames:
#    - ActivePortfolio.csv.xlsx        (current holdings)
#    - AllStockTransactions.csv.xls    (transaction history)
#    These are gitignored; they never leave your machine.
```

## Everyday use

1. When you trade, re-export the two broker files from your broker and drop them at the root (overwrite the existing ones).
2. In Claude Code, run `/daily-scan` — this parses the fresh export, refreshes FX, runs the risk + allocation skills, and returns a one-screen briefing. **The daily scan never recommends trades** — it's a dashboard.
3. When you want an opinion ("should I buy X?", "trim Y?"), ask normally. The orchestrator spawns:
   - `bull-officer` (strongest disciplined bull case)
   - `risk-officer` (strongest disciplined bear case)
   - `portfolio-manager` (synthesis with a lean — or framing of the decision if the evidence is genuinely mixed)
   Each output must cite primary sources (earnings releases, 10-Q segments, IR commentary). Each output ends with a Learning note.

## Profile-driven, not hardcoded

The agents do NOT assume any specific age, country, exchange, or risk tolerance. They read `research/user-views.md` at the start of every analysis and calibrate based on what you wrote there. The risk profile bands available are:

- **Low** — capital preservation, single-digit drawdowns
- **Medium** — accepts 10-20 % drawdowns, prefers diversification
- **Medium-high** — accepts 15-25 % drawdowns, mix of diversified + conviction
- **High** — accepts 25-30 % drawdowns, concentration up to 15 %+ NAV in conviction names
- **Very high** — accepts 40 %+ drawdowns, uses leverage / options / micro-caps

Pick the band that matches you (or write a custom description). The portfolio-manager calibrates: e.g., volatility bear cases are weak evidence for high-risk profiles but legitimate for low/medium profiles.

If `user-views.md` is missing, the agents fall back to a **medium-risk** default and explicitly note the assumption.

## Regional support

- **US equities** — first-class support via yfinance. Drop a US broker export at the root.
- **TASE (Tel Aviv Stock Exchange) / Israeli mutual funds** — supported. The `.TA` suffix resolves via yfinance; IL-numeric mutual fund codes resolve via `maya.tase.co.il` and `bizportal.co.il` public scrapes.
- **Other exchanges** — should work for any ticker yfinance covers (LSE `.L`, Toronto `.TO`, Frankfurt `.F`, etc.). The dual-currency display assumes USD as one leg; configure your home currency in `user-views.md`.

## Non-negotiables (enforced by hooks)

- No trade execution — ever. The workbench is read-only against the real world.
- Every BUY/SELL/HOLD must carry a bear case from `risk-officer` and a synthesis from `portfolio-manager`. The `Stop` hook blocks the turn otherwise.
- All portfolio math runs in Python skills, not in prose.
- Numbers without citations are forbidden — agents must call a tool or cite a skill output for every quantitative claim.
- Broker files never leave the folder — no uploads, no webhooks, no API posts.

See `CLAUDE.md` for the complete rulebook.

## Privacy / sharing

The repo ships with **none of your data**:
- Your broker exports (root `.xlsx` / `.xls`) are gitignored.
- Your parsed portfolio (`portfolio/`) is gitignored.
- Your profile (`research/user-views.md`) is gitignored — only the example ships.
- Your daily research outputs and signal history (`research/daily/`, `research/signals/`, `research/theses/`, `research/regime/`) are gitignored.
- The yfinance cache (`.cache/`) is gitignored.

You can fork or share this repo without exposing any holdings or personal context.
