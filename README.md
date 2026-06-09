# Investing Workbench

A Claude Code-powered personal investing desk: drop in your broker exports, fill in a profile file, and get grounded daily analysis on your portfolio — with a mandatory bull/bear/manager debate before any BUY/SELL/HOLD recommendation.

The workbench is **profile-driven** — none of your data, holdings, or risk preferences are hardcoded. Each user fills in their own `research/user-views.md` and drops their own broker exports into the project root. The agents adapt accordingly.

## What's here

```
CLAUDE.md                            # project rules (always read by Claude Code)
.claude/
  settings.json                      # hooks enforcing the guardrails
  agents/                            # bull-officer, risk-officer, portfolio-manager,
  |                                  # orchestrator, stock-analyst + architecture reviewers
  skills/                            # portfolio analytics + scanner skills:
  |  portfolio-parse/                #   broker exports → positions.json
  |  risk-metrics/                   #   Sharpe, Sortino, max drawdown, beta
  |  sector-allocation/              #   weights by sector/region/currency, HHI
  |  momentum-check/                 #   per-ticker momentum + 150/200DMA trend flags
  |  stops/                          #   Chandelier + 200DMA stop reference levels
  |  early-movers/                   #   150DMA setup scanner (breakouts + pullbacks)
  |  opportunity-scanner/            #   3 ranked buy-candidate lists
  |  market-regime/                  #   Fear & Greed + VIX + SPY-200DMA label
  |  earnings-calendar/              #   upcoming earnings flags
  |  currency-conversion/            #   FX at daily-cached yfinance rate
  |  charts/                         #   browser dashboard + PNG donut
  commands/                          # /daily-report, /daily-scan, /improve-report,
                                     # /architecture-review slash commands
scripts/lib/                         # yfinance wrapper, FX, schema, guardrails
portfolio/                           # parsed broker data — gitignored, never leaves your machine
research/
  user-views.example.md              # template — copy to user-views.md and fill in
  user-views.md                      # YOUR profile (gitignored)
  daily/                             # your daily research outputs (gitignored)
  signals/                           # saved per-ticker signals for reversal detection (gitignored)
learning/                            # glossary + concepts log
```

## One-time setup

```bash
# 1. Install Python deps
pip3 install -r scripts/requirements.txt

# 2. Create your profile from the template
cp research/user-views.example.md research/user-views.md
# Edit research/user-views.md — fill in your age, country, risk band,
# investing horizon, currency, and any stated views about the market.

# 3. Drop your broker exports at the project root
#    Rename them to match what portfolio-parse expects, or pass custom
#    filenames: echo '{"active_xlsx":"MyHoldings.xlsx"}' | python3 ...
#    They are gitignored and never leave your machine.
```

## Everyday use

### Morning dashboard
```
/daily-scan
```
Parses fresh broker exports, refreshes FX, runs risk + allocation skills, and returns a one-screen briefing. **Never recommends trades** — it's a read-only dashboard.

### Full daily report
```
/daily-report
```
Runs the complete analysis: macro context, per-holding analysis with signals (STRONG BUY / ADD / KEEP / HOLD / REDUCE / SELL / EXIT), stop-loss reference tables, 150DMA early-mover setups, and an opportunity bridge against today's scanner output. Writes `research/daily/<date>/report.md`.

### Specific decisions
Ask naturally: *"Should I buy NVDA?"* or *"Trim MSFT?"*

The orchestrator spawns three agents in parallel:
- **bull-officer** — strongest disciplined bull case, with primary-source research
- **risk-officer** — strongest disciplined bear case, with primary-source research
- **portfolio-manager** — synthesis with a lean (or frames the decision if evidence is genuinely mixed)

Every output cites primary sources (earnings releases, 10-Q segments, IR commentary). The `Stop` hook blocks any BUY/SELL/HOLD that skips this pipeline.

## Profile-driven, not hardcoded

The agents do NOT assume any specific age, country, exchange, or risk tolerance. They read `research/user-views.md` at the start of every analysis and calibrate based on what you wrote there. Risk profile bands:

| Band | Drawdown comfort | Typical style |
|---|---|---|
| **Low** | < 10% | Capital preservation, broad ETFs |
| **Medium** | 10–20% | Diversified, trims >7-8% single-name |
| **Medium-high** | 15–25% | Mix of diversified + conviction |
| **High** | 25–30% | Concentration up to 15%+ NAV in conviction names |
| **Very high** | 40%+ | Leverage / options / micro-caps acceptable |

If `user-views.md` is missing, agents fall back to **medium-risk** and note the assumption.

## Exchange support

Any ticker covered by [yfinance](https://github.com/ranaroussi/yfinance) works out of the box. Common suffixes:

| Exchange | Suffix | Example |
|---|---|---|
| US (NYSE/NASDAQ) | _(none)_ | `NVDA` |
| London Stock Exchange | `.L` | `SHEL.L` |
| Tel Aviv Stock Exchange | `.TA` | `TEVA.TA` |
| Toronto | `.TO` | `RY.TO` |
| Frankfurt | `.F` | `SAP.F` |

Configure your home currency and any local-exchange holdings in `research/user-views.md`. The FX layer pulls live rates via yfinance daily.

## Guardrails (enforced by hooks)

- **No trade execution — ever.** The workbench is read-only against the real world.
- **Every BUY/SELL/HOLD requires** bull-officer + risk-officer + portfolio-manager. The `Stop` hook blocks the turn otherwise.
- **No manufactured sell signals.** There is no minimum count of REDUCE/SELL/EXIT — honest output in a strong tape with no fundamental breaks is mostly KEEP/HOLD/ADD.
- **All portfolio math runs in Python skills**, not in Claude's prose.
- **Numbers without citations are forbidden** — every quantitative claim must cite a skill output or a primary-source URL.
- **Broker files never leave the folder** — no uploads, no webhooks, no API posts.

See `CLAUDE.md` for the complete rulebook.

## Privacy

The repo ships with **none of your data**:
- Broker exports and parsed portfolio (`portfolio/`) are gitignored.
- Your profile (`research/user-views.md`) is gitignored — only the blank template ships.
- Daily research outputs, signal history, and the yfinance cache are all gitignored.

You can fork or share this repo without exposing any holdings or personal context.
