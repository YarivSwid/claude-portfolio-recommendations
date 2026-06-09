---
name: opportunity-scanner
description: Generates three ranked lists of stock/ETF opportunities using the bull-officer + risk-officer + portfolio-manager pipeline. List 1 knows the full portfolio. List 2 knows only the user profile. List 3 is pure market. Runs daily as part of the report. Results saved to research/daily/<date>/opportunities.json.
---

# opportunity-scanner

## When to use
- Automatically as part of the daily report run (step 5)
- When the user asks "what should I buy?" or "any opportunities today?"

## ETF-vs-single-name rule (teach this to agents)

When the investment thesis is a BROAD GROWTH sector (e.g. "AI infrastructure", "semiconductors"),
recommend an ETF over a single stock UNLESS the single stock has a specific catalyst the ETF
would dilute. Reason: an ETF gives the thesis exposure with lower single-company risk.

When the thesis is for a DEFENSIVE or LOWER-GROWTH sector (healthcare, utilities, staples),
prefer a specific high-growth single name within that sector over a broad sector ETF.
Reason: a broad defensive ETF (XLV at 5% annual return) drags portfolio returns for a high-risk investor.
Only recommend defensive ETFs if the investor explicitly asks for reduced volatility.

Examples:
- "AI infrastructure growth" → SOXX or SMH (broad growth thesis = ETF)
- "Specific earnings catalyst on GOOG" → GOOG stock (company-specific thesis = single name)
- "Healthcare with a growth catalyst" → LLY or specific biotech (high-growth pick, not XLV)

Always state whether it's stock or ETF and WHY that form was chosen.

## User profile (always available to all lists)

The scanner reads `research/user-views.md` at runtime and injects the user's stated profile (age, risk tolerance, horizon, currency, preferences) into every list's prompt via `_profile_summary()` in `scan.py`. Cash size is passed in via the `cash_ils` input parameter. There is no hardcoded profile — each user calibrates the scanner by editing their own `user-views.md`.

If `user-views.md` is missing, the scanner falls back to a medium-risk, 10-year-horizon, balanced-growth default and notes the assumption in its output.

## Fit rubric (portfolio-manager assigns, word-form — no manufactured precision)
- **Exceptional fit**: strong bull case, low risk, fills a real gap, high conviction
- **Good fit**: solid thesis, manageable risk, adds value to portfolio
- **Neutral**: interesting but uncertain or overlapping
- **Weak fit**: high risk, speculative, or duplicates existing exposure
- **Not recommended**: avoid

Assessment factors (weigh qualitatively, not as points):
1. Quality of bull case (primary sources, real catalysts)
2. Risk-adjusted return potential (Sharpe-like reasoning)
3. How well it fits the user's profile/portfolio
4. Valuation reasonableness (not already fully priced in)

## Input
```bash
# DEFAULT — Phase 1 only (cheap, ~$2-4 on Sonnet, ~5 min):
echo '{"cash_ils": 120000, "date": "2026-04-21"}' | python3 .claude/skills/opportunity-scanner/scripts/scan.py

# Full pipeline (Phase 1 + Phase 2 deep dive — ~$30-50 on Sonnet, ~25 min):
# Use only when you have real cash to deploy and want bull/risk/manager debate per name.
# For routine deep-dives on 1-3 specific tickers, ask Claude in chat instead — it's cheaper.
echo '{"cash_ils": 120000, "date": "2026-04-21", "phase1_only": false}' | python3 .claude/skills/opportunity-scanner/scripts/scan.py
```

## Cross-list overlap
Lists run sequentially: List 1 first, then List 2 (told to *prefer* tickers not on List 1), then List 3 (told to *prefer* tickers not on Lists 1+2). The "exclude" hint is a *preference*, not a hard filter — if a name is genuinely the right pick from three independent framings (portfolio-fit, profile-fit, market), it can and should appear on more than one list. The scanner flags overlaps via `_dedup_warning` and `_duplicate_of` for visibility, but does NOT remove them. A ticker on all three lists is a high-conviction signal, not a bug.

## Signal variety
Not everything is BUY. Signals include: BUY / WATCH / HOLD / AVOID.
- Stocks reporting earnings within 7 days default to WATCH (binary event risk).
- At least 2 of 10 items per list must NOT be BUY.

## Cash-aware sizing
`max_invest_ils` per ticker is capped at `cash_ils / 5` (max ₪25,000) and total per list cannot exceed `cash_ils`.

## Output
Writes `research/daily/<date>/opportunities.json` and returns it on stdout:
```json
{
  "date": "2026-04-21",
  "list1_portfolio_fit": [ ...10 items... ],
  "list2_profile_fit":   [ ...10 items... ],
  "list3_market_picks":  [ ...10 items... ],
  "_status": "done"
}
```

`_status` values: `done` (full pipeline), `done:phase1_only` (screening only), `running` (in progress).

Each item:
```json
{
  "ticker": "SOXX",
  "name": "iShares Semiconductor ETF",
  "type": "ETF",
  "fit": "good",
  "max_invest_ils": 18000,
  "signal": "BUY",
  "reason_fits": "Broad AI-infrastructure exposure at lower single-name risk than stacking 4 semi names.",
  "bull_point": "AI capex cycle driving record semiconductor demand through 2027+.",
  "risk_point": "Capex digestion cycle could compress semi multiples near-term.",
  "data_as_of": "2026-04-21"
}
```
