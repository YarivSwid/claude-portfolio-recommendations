# Glossary

Living reference for terms the workbench introduces. Phase 1 seed — grows organically via the Learning note convention.

## Investing

**Sharpe ratio** — Excess return per unit of total volatility. `(annual return − risk-free rate) / annual vol`. Higher is better. A Sharpe of 1 is good, 2 is very good, 3+ is suspicious. Limitation: treats upside and downside vol the same, which is why we also compute Sortino.

**Sortino ratio** — Like Sharpe, but uses *downside* vol only. Better measure when returns are asymmetric.

**Maximum drawdown (MDD)** — The worst peak-to-trough decline in the NAV over the window. Tells you the worst pain you'd have had to sit through. More visceral than vol.

**Beta (β)** — Sensitivity to a benchmark. β=1.0 means you move 1:1 with it; β>1 means amplified; β<1 means damped. We compute β vs SPY (US market) and TA-35 (Israeli market) because the portfolio is dual.

**Herfindahl–Hirschman Index (HHI)** — Sum of squared position weights. Measures concentration. <0.10 = diversified; 0.10–0.18 = moderate; >0.18 = concentrated. A single position at 100% = HHI 1.0.

**Annualized volatility** — Daily std × √252. Rough size of the typical yearly swing in returns.

**Risk-free rate** — The yield on "safe" cash (3M T-bill in USD, Bank of Israel overnight in ILS). The baseline your risky returns have to beat to justify the risk.

**Micro-cap** — US convention: market cap under ~$300M. Thinner liquidity, larger bid-ask, more idiosyncratic risk. User's floor rule excludes these. TASE analog: under ~₪500M.

**Taxable event** — A transaction that realizes a capital gain or loss for tax purposes (selling for more than cost basis, most dividends). Israel's capital-gains rate is 25% on real (inflation-adjusted) gains. Tax handling is out of scope for this workbench.

## Claude Code

**Skill** — A named capability the assistant can invoke. Lives at `.claude/skills/<name>/SKILL.md` with optional helper scripts. The SKILL.md frontmatter's `description` is what the assistant reads to decide when to use it.

**Subagent** — A separately-prompted Claude instance the main assistant can delegate to. Lives at `.claude/agents/<name>.md`. Frontmatter includes `name`, `description`, `tools`. The main assistant spawns one via the `Task` tool with `subagent_type: <name>`.

**Hook** — A shell command the harness runs in response to an event (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, …). Hooks return JSON to deny / ask / allow tool calls or to inject context. Configured in `.claude/settings.json`.

**Slash command** — A reusable prompt the user invokes with `/<name>`. Lives at `.claude/commands/<name>.md`. Can restrict `allowed-tools` in frontmatter.

**`/loop`** — A Claude Code skill that schedules a prompt to re-run on an interval. Used here for the daily scan and EOD snapshot (Phase 2).
