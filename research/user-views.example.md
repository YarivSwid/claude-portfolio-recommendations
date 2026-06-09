# User Views — Context, NOT Input to Decisions

> **Setup:** Copy this file to `research/user-views.md` and fill in the bracketed sections below.
> The agents read `research/user-views.md` (gitignored) — never your example file.
>
> **Holdings go elsewhere — not in this file:**
> - Easiest: copy `portfolio/portfolio-template.csv` → fill it in → tell Claude to load it
> - Broker export: drop `ActivePortfolio.csv.xlsx` at the project root → run `/daily-scan`
> - Plain English: just tell Claude "I hold X shares of NVDA at $Y cost" — it will build the file
>
> **Cash:** Tell Claude "I have $X ready to invest" or add a CASH row to your CSV.
> This is used by the opportunity scanner to size recommendations.

This file describes the user as a person and the views they've articulated. It is **reference context** for agents, not a set of constraints that should drive recommendations.

**How agents must use this file:**
- READ it before forming a thesis, so you understand the user's frame.
- DO NOT weight your analysis toward agreeing with these views. Your job is independent thinking.
- When your independent analysis **contradicts** a user view, say so explicitly in a "Tension with user views" note at the end of your case — do not hide the contradiction, do not resolve it in the user's favor.
- When your independent analysis **happens to align** with a user view, that's fine — but note it as "aligns with user-stated view," not "amplified because user agrees."
- NEVER lower your confidence in a thesis just because it contradicts a user view. The user values being challenged; tell them what you actually think.

**How portfolio-manager must use this file:**
- When synthesizing, surface tensions between bull, bear, and user views as **open questions for the user** — not verdicts.
- Do not use user views as tie-breakers. Use real analysis.

---

## Profile

Fill in the items below. Items marked _required_ are used directly by the agents to calibrate analysis.

- Age: [e.g., 30]  _(required — affects horizon-based calibration)_
- Country / tax residence: [e.g., United States, Israel, UK]  _(required — affects FX, broker rules, taxable-event flags)_
- Employment / income source: [e.g., software engineer, retired, business owner] _(optional)_
- Investing horizon: [e.g., 30+ years, 10 years, 5 years]  _(required — used to weight transient vs structural risks)_
- Risk tolerance: **[low / medium / medium-high / high / very-high]** ([1-10 scale, e.g., 8/10]).  _(required — see "Risk profile definitions" below)_
  - Comfort with portfolio drawdown: [e.g., tolerates 25-30 % drawdowns / wants <10 % drawdowns]
  - Leverage: [yes / no — recommend "no" unless you know what you're doing]
  - Penny stocks / micro-caps: [yes / no]
- Currency / exchanges: [e.g., USD only / USD + ILS dual / GBP + USD]  _(required — affects FX display and ticker resolution)_
- Goal: [e.g., maximize long-term return per unit risk / smooth-ride income / capital preservation]  _(required)_

### Risk profile definitions (used by portfolio-manager)

The agents use these definitions verbatim — pick the one that fits and the manager will calibrate accordingly:

- **Low risk** — capital preservation primary, comfort with single-digit drawdowns, prefers fixed income / dividend equity / broad ETFs, no concentrated single-name bets.
- **Medium risk** — accepts 10-20 % drawdowns, prefers diversification, trims single positions >7-8 % of NAV for safety, broad ETFs over concentrated single-name bets.
- **Medium-high risk** — accepts 15-25 % drawdowns, mix of diversified and conviction positions, tolerates 8-12 % single-name weights.
- **High risk** — accepts 25-30 % drawdowns, higher concentration tolerance (lets winners run up to 15 %+ NAV), accepts factor-stacking in conviction areas, prefers single-name conviction over diversified wrappers, accepts higher vol for higher expected return.
- **Very high risk** — accepts 40 %+ drawdowns, willing to use leverage / options / crypto / micro-caps, runs concentrated bets >20 % NAV.

## Stated beliefs about the market (as of [YYYY-MM-DD])

The user has articulated the following views. Agents should engage with them critically, not defer to them:

- [Belief 1 — e.g., "AI is real and will keep growing. The capex cycle has durable multi-year tailwind."]
- [Belief 2 — e.g., "I think rates will stay higher for longer."]
- [Belief 3 — e.g., "I'm skeptical of meme/retail-driven rallies."]
- [Belief 4 — e.g., "Geopolitical X is a live risk to my Y exposure."]
- [Add or remove rows as relevant. If you have no strong views, write "no strong directional views — open to evidence on all sides."]

## Preferences about portfolio management

- Concentration cap behavior: [e.g., "10 % is informational only — I size myself, do NOT auto-recommend trim on weight alone" / "10 % is a hard cap, recommend trim on breach"]
- Crypto: [e.g., "Hold IBIT, do not propose new crypto" / "No crypto exposure" / "Open to crypto"]
- Micro-caps: [e.g., "Respect the workbench floor (<$300M US / <₪500M TASE)" / "open to micro-caps"]
- Position sizing actions: agent default is no TRIM on positions <₪10,000 (~$3,300). Override here if you want different sizing.
- Sector bias instruction: [e.g., "Don't bias toward AI even though I believe in it — give fair competition to all sectors"]
- Diversification stance: [e.g., "Diversification is not a goal in itself — every dollar has opportunity cost" / "I want broad diversification across sectors"]

## Behavioral patterns observed

[Fill in over time as agents learn how you actually act vs how you say you act. Examples:]

- [Pattern: "Sold 70 % of NAME on YYYY-MM-DD after a momentum extension — willing to trim winners on parabolic moves."]
- [Pattern: "Flags momentum stretches (e.g., notices when names are >50 % above 200DMA)."]
- [Pattern: "Cares about real research, not template number-filling."]

(Leave blank initially; the agents and you will fill this in over the first few months of use.)

## What the user expects from agents

- Real research, not templated number-filling. Quote primary sources (earnings releases, 10-Q excerpts, IR commentary, credible financial press). Cite every claim.
- Honest disagreement. If the analysis contradicts a user view, say so.
- Position-aware advice. Hold sizes matter (see size gate above).
- No fake precision. Words like "strong/moderate/weak" evidence are more honest than confidence scores to two decimals.

## Gaps — user has NOT explicitly stated views on:

[List axes where you don't yet have a view — agents will flag these as open questions when they matter. Examples:]

- Fixed income tolerance (TLT/BIL/IEF) — open question
- Gold / real assets (GLD) — open question
- International ex-US preference (EFA/VEA/EEM) — open question
- Emerging markets — open question
- Preferred max concentration ceiling — open question

When these gaps matter for a recommendation, **flag them as open questions for the user**, don't assume.
