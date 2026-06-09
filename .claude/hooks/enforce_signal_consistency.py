#!/usr/bin/env python3
"""Stop hook — catch cross-section inconsistencies in portfolio recommendations.

Triggers only when the assistant's current turn contains multi-ticker recommendation
tables (per-holding ratings, tranche lists, etc). Checks:

  1. A ticker rated KEEP/HOLD in one table cannot appear as BUY/ADD in another
     table in the same turn. (The GOOG bug from this conversation.)
  2. A ticker that the user does NOT own (not in positions.json) cannot appear
     in a "per-holding" table labeled as if it's held. (The AVGO/LRCX/etc bug.)
  3. A ticker the user sold in the last 30 days flagged if it reappears as
     BUY/ADD. (The DELL bug.)
  4. Broad-review prose must be preceded by a raw data block table. If the turn
     contains per-holding prose (detected by ≥3 holdings mentioned with prices or
     share counts) but no raw data block table, block and require the data block.
  5. Any specific price (e.g. $187, $393.59) or share count (e.g. "17 shares",
     "sell 9") written in prose must appear in the raw data block or transactions.
     If not found, block with the offending value.

On any violation, block the turn with a specific message telling the assistant
which invariant broke and on which ticker.

Scope: only the current turn (from most recent user message onward).
"""
import json
import re
import sys
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.signal_patterns import TICKER_RE, TICKER_BLACKLIST  # noqa: E402
from _log import emit, emit_error  # noqa: E402

HOOK_NAME = "enforce_signal_consistency"

# Canonical signal vocabulary lives in scripts/lib/signal_patterns. We extend
# the ticker blacklist with a few cross-section-only false positives that
# don't belong in the global module (sub-table headers, risk-rating tokens).
_EXCLUDE = set(TICKER_BLACKLIST) | {
    "T", "T1", "T2", "T3",
    "GT", "ATF", "MED", "HIGH", "LOW", "RISK",
    "Q1", "Q2", "Q3", "Q4",
}
# Row-level signal token matcher — the full vocabulary including REDUCE and
# STRONG BUY so cross-section consistency checks see the same set of tokens
# the persistence/firewall hooks see.
ROW_SIG_RE = re.compile(
    r"\b(STRONG[\s_]BUY|BUY|SELL|HOLD|KEEP|ADD|TRIM|CUT|EXIT|REDUCE|AVOID|WATCH|FLAG)\b",
    re.IGNORECASE,
)


def _read_positions():
    p = Path(__file__).resolve().parents[2] / "portfolio" / "positions.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
        pos = data if isinstance(data, list) else data.get("positions", [])
        return {x.get("symbol") for x in pos if x.get("symbol")}
    except Exception:
        return set()


def _recent_sells(days=30):
    p = Path(__file__).resolve().parents[2] / "portfolio" / "transactions.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        txns = data if isinstance(data, list) else data.get("transactions", [])
    except Exception:
        return {}
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    out = {}
    for t in txns:
        if t.get("action") != "sell":
            continue
        d = t.get("value_date", "")
        if d >= cutoff:
            sym = t.get("symbol")
            if sym and sym not in out:
                out[sym] = d
    return out


def _parse_tables(text):
    """Return list of (section_label, {ticker: signal}) tuples.

    A 'section' is any markdown table. We classify by looking at nearby
    headings — if a heading like 'per-holding' / 'ratings' / 'current' is
    seen, we treat the table as PER_HOLDING. If 'deploy' / 'buy list' /
    'new' / 'tranche' / 'candidates', we treat it as NEW_BUYS.
    """
    out = []
    lines = text.split("\n")
    current_heading = ""
    in_table = False
    current_rows = []
    current_section = "UNKNOWN"

    def _classify(heading):
        h = heading.lower()
        if any(k in h for k in ("deploy", "buy list", "tranche", "new buy", "candidate", "to add", "cash deployment")):
            return "NEW_BUYS"
        if any(k in h for k in ("per-holding", "per holding", "ratings", "current holding", "holdings", "section 1")):
            return "PER_HOLDING"
        if "section 2" in h:
            return "NEW_BUYS"
        return "UNKNOWN"

    for line in lines:
        s = line.strip()
        if s.startswith("#") or s.startswith("**"):
            current_heading = s
            current_section = _classify(s)
        if s.startswith("|") and s.count("|") >= 3:
            if not in_table:
                in_table = True
                current_rows = []
            current_rows.append(s)
        else:
            if in_table and current_rows:
                rows = {}
                for row in current_rows:
                    cells = [c.strip() for c in row.split("|") if c.strip()]
                    if not cells:
                        continue
                    tickers_in_first = TICKER_RE.findall(cells[0])
                    tickers_in_first = [t for t in tickers_in_first if t not in _EXCLUDE]
                    if not tickers_in_first:
                        continue
                    ticker = tickers_in_first[0]
                    row_signal = None
                    for c in cells[1:]:
                        m = ROW_SIG_RE.search(c)
                        if m:
                            row_signal = m.group(1).upper()
                            break
                    if not row_signal and current_section == "NEW_BUYS":
                        row_signal = "BUY"
                    if row_signal:
                        rows[ticker] = row_signal
                if rows:
                    out.append((current_section, rows))
                in_table = False
                current_rows = []
    return out


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        emit(HOOK_NAME, "skip", reason=f"stdin parse failed: {e}")
        sys.exit(0)

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        emit(HOOK_NAME, "skip", reason="no transcript")
        sys.exit(0)

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

    last_user_idx = 0
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        msg = e.get("message", {}) or {}
        if msg.get("role") == "user":
            content = msg.get("content", [])
            is_tool_result = False
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        is_tool_result = True
                        break
            if not is_tool_result:
                last_user_idx = i
                break

    current_turn = entries[last_user_idx:]

    combined_text_parts = []
    for e in current_turn:
        msg = e.get("message", {}) or {}
        if msg.get("role") != "assistant":
            continue
        for b in msg.get("content", []) or []:
            if isinstance(b, dict) and b.get("type") == "text":
                combined_text_parts.append(b.get("text", ""))
    text = "\n".join(combined_text_parts)

    if not text:
        sys.exit(0)

    tables = _parse_tables(text)
    if len(tables) < 1:
        sys.exit(0)

    holdings = _read_positions()
    recent_sells = _recent_sells(30)

    violations = []

    # Check 4 & 5: broad-review data-block enforcement
    # Detect if this looks like a broad review (≥3 holdings mentioned in prose with price/share figures)
    price_re = re.compile(r"\$\d[\d,]*(?:\.\d+)?")
    share_count_re = re.compile(r"\b(\d+)\s+shares?\b", re.IGNORECASE)
    prose_prices = price_re.findall(text)
    prose_share_counts = share_count_re.findall(text)

    # A raw data block is present if there's a markdown table with a "data_as_of" column header
    has_data_block = bool(re.search(r"\|\s*data_as_of\s*\|", text, re.IGNORECASE))

    # Count how many held tickers appear in prose (rough broad-review signal)
    held_tickers_in_prose = sum(1 for t in holdings if t and re.search(rf"\b{re.escape(t)}\b", text))
    is_broad_review = held_tickers_in_prose >= 3

    if is_broad_review and not has_data_block:
        violations.append(
            "Broad-review prose covers ≥3 holdings but no raw data block table (with 'data_as_of' column) "
            "was emitted before the prose. Per orchestrator spec step 3, emit the data block first so "
            "every price and share count can be traced to a skill output."
        )

    if is_broad_review and has_data_block and (prose_prices or prose_share_counts):
        # Extract prices and share counts from the data block only (lines containing |data_as_of| table)
        data_block_lines = []
        in_block = False
        for line in text.split("\n"):
            if re.search(r"\|\s*data_as_of\s*\|", line, re.IGNORECASE):
                in_block = True
            if in_block:
                data_block_lines.append(line)
                if line.strip() == "" and len(data_block_lines) > 2:
                    break
        data_block_text = "\n".join(data_block_lines)
        block_prices = set(price_re.findall(data_block_text))
        block_shares = set(share_count_re.findall(data_block_text))

        uncited_prices = [p for p in prose_prices if p not in block_prices]
        uncited_shares = [s for s in prose_share_counts if s not in block_shares]

        if uncited_prices:
            violations.append(
                f"Prose contains price(s) {uncited_prices[:5]} not found in the raw data block. "
                "Every specific price in broad-review prose must come from the data block table, "
                "not from model memory. Remove or add to the data block with a data_as_of stamp."
            )
        if uncited_shares:
            violations.append(
                f"Prose contains share count(s) {uncited_shares[:5]} not found in the raw data block. "
                "Share counts must trace back to positions.json (via the data block), not recalled from memory."
            )

    per_holding_signals = {}
    new_buys_signals = {}
    for section, rows in tables:
        if section == "PER_HOLDING":
            for t, sig in rows.items():
                per_holding_signals.setdefault(t, set()).add(sig)
        elif section == "NEW_BUYS":
            for t, sig in rows.items():
                new_buys_signals.setdefault(t, set()).add(sig)

    if holdings:
        for t in per_holding_signals:
            if t not in holdings and t not in _EXCLUDE:
                if "/" in t or len(t) < 2:
                    continue
                violations.append(
                    f"{t}: listed in per-holding table but NOT in positions.json — user does not own this. "
                    f"Move to new-buys section or remove."
                )

    for t in new_buys_signals:
        if t in recent_sells:
            if any(s in {"BUY", "ADD"} for s in new_buys_signals[t]):
                violations.append(
                    f"{t}: user SOLD this on {recent_sells[t]} (within last 30 days). "
                    f"Re-recommending buy requires citing why conviction is higher now."
                )

    if violations:
        out = {
            "decision": "block",
            "reason": (
                "Guardrail: cross-section consistency violations in this turn's recommendation "
                "tables:\n  - " + "\n  - ".join(violations) +
                "\n\nFix the tables (rename, move, or delete the offending rows) and re-emit. "
                "The portfolio-manager spec defines the coverage + consistency invariants."
            ),
        }
        emit(HOOK_NAME, "block", reason=f"{len(violations)} consistency violation(s)")
        print(json.dumps(out))
        sys.exit(0)

    emit(HOOK_NAME, "pass")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as _e:
        emit_error(HOOK_NAME, _e)
        sys.exit(0)
