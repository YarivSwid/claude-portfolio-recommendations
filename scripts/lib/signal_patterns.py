"""Canonical signal/stop-language regex module — single source of truth.

Every Stop-hook that needs to detect signals in assistant output should import
from this module rather than defining its own regex. Pre-Step 3, each hook had
its own copy of the regex, and they had drifted:

    persist_signals.py        : BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD          (no KEEP, no REDUCE, no STRONG BUY)
    enforce_risk_officer.py   : BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD          (same gap)
    enforce_signal_reversal.py: BUY|SELL|HOLD|TRIM|CUT|EXIT|ADD          (same gap)
    enforce_signal_consistency.py: BUY|SELL|HOLD|KEEP|ADD|TRIM|EXIT|FLAG|REDUCE|AVOID|SKIP|WATCH   (richer)

Net effect: broad-review prose emitting "REDUCE TEVA" or "STRONG BUY NVDA" was
silently dropped by persist_signals/risk-officer/reversal — never logged to the
decision-log, never checked for reversal. This module fixes that by exposing
one vocabulary, one regex shape, used everywhere.

Conventions:
- Tokens are matched case-sensitively (default). The workbench convention is that
  emitted signals are upper-case; casual prose ("hold steady", "keep an eye on")
  is lower-case and does not match. This keeps false-positives low without an
  expensive context check.
- "STRONG BUY" and "STRONG_BUY" are both accepted (orchestrator uses space form,
  some agent outputs use underscore form).
- `find_signals` is intentionally simple: it scans for signal tokens and pairs
  each with the nearest ticker on the same or previous line. Hooks that need
  more sophisticated pairing (signal_consistency does table-cell parsing) should
  keep their own logic and just use SIGNAL_MARKER_RE / TICKER_RE from here.
"""
from __future__ import annotations

import re
from typing import Iterable

SIGNAL_TOKENS: tuple[str, ...] = (
    "STRONG BUY",
    "STRONG_BUY",
    "STRONG SELL",
    "STRONG_SELL",
    "BUY",
    "ADD",
    "KEEP",
    "HOLD",
    "REDUCE",
    "TRIM",
    "CUT",
    "SELL",
    "EXIT",
    "AVOID",
    "WATCH",
    "FLAG",
    "SKIP",
    # WATCH-class aliases the scanner historically emitted. Kept so older
    # cached signal files in research/signals/ still parse. New scanner runs
    # normalize these to WATCH at the formatter stage.
    "WAIT-FOR-COOLING",
    "WAIT-FOR-CATALYST",
)

NEGATIVE_SIGNAL_TOKENS: tuple[str, ...] = ("REDUCE", "TRIM", "CUT", "SELL", "EXIT")
POSITIVE_SIGNAL_TOKENS: tuple[str, ...] = ("STRONG BUY", "STRONG_BUY", "BUY", "ADD")
NEUTRAL_SIGNAL_TOKENS: tuple[str, ...] = (
    "KEEP", "HOLD", "WATCH", "WAIT-FOR-COOLING", "WAIT-FOR-CATALYST",
)

_SIGNAL_INNER = (
    r"STRONG[\s_]BUY|STRONG[\s_]SELL|"
    r"BUY|ADD|KEEP|HOLD|REDUCE|TRIM|CUT|SELL|EXIT|AVOID|"
    r"WAIT-FOR-COOLING|WAIT-FOR-CATALYST|WATCH|FLAG|SKIP"
)

SIGNAL_MARKER_RE: re.Pattern[str] = re.compile(
    r"(?:"
    r"\*{2}(?:" + _SIGNAL_INNER + r")\*{2}"
    r"|"
    r"\b(?:" + _SIGNAL_INNER + r")\b"
    r"|"
    r"\|\s*\*{2}(?:" + _SIGNAL_INNER + r")\*{2}\s*\|"
    r")"
)

SIGNAL_VALUE_RE: re.Pattern[str] = re.compile(
    r"\b(?:signal|recommendation|action|verdict|call)\s*[:=]\s*\*{0,2}\s*"
    r"(" + _SIGNAL_INNER + r")\b",
    re.IGNORECASE,
)

TICKER_RE: re.Pattern[str] = re.compile(
    r"\b(?:IL\d{7}|HRL\.[A-Z0-9]+|[A-Z]{1,5}(?:[.\-/][A-Z]{1,3})?)\b"
)

STOP_LANGUAGE_RE: re.Pattern[str] = re.compile(
    r"\b("
    r"chandelier|atr\s*\(\s*1[45]\s*\)|atr\(?\d+\)?|"
    r"trailing\s+stop|stop\s+(?:level|price|loss)|stop[-\s]out|"
    r"200\s*[-/]?\s*day(?:\s+moving\s+average)?|200\s*dma|"
    r"150\s*[-/]?\s*day(?:\s+moving\s+average)?|150\s*dma|"
    r"50\s*[-/]?\s*day(?:\s+moving\s+average)?|50\s*dma|"
    r"trigger(?:ed)?|breach(?:ed|ing)?|broke(?:n)?\s+below|"
    r"below\s+stop|stop\s+breached"
    r")\b",
    re.IGNORECASE,
)

THESIS_BREAK_RE: re.Pattern[str] = re.compile(
    r"\bthesis\s+break(?:s)?\s+at\s*[:=]\s*([^\n]+)",
    re.IGNORECASE,
)

SIGNAL_REVERSAL_ACK_RE: re.Pattern[str] = re.compile(
    r"\bSIGNAL\s+(CHANGE|CONFIRMED)\s*:",
    re.IGNORECASE,
)

OPPOSITES: dict[str, set[str]] = {
    "BUY":        {"SELL", "REDUCE", "TRIM", "EXIT", "CUT"},
    "STRONG BUY": {"SELL", "REDUCE", "TRIM", "EXIT", "CUT"},
    "STRONG_BUY": {"SELL", "REDUCE", "TRIM", "EXIT", "CUT"},
    "ADD":        {"SELL", "REDUCE", "TRIM", "EXIT", "CUT"},
    "SELL":       {"BUY", "STRONG BUY", "STRONG_BUY", "ADD"},
    "REDUCE":     {"BUY", "STRONG BUY", "STRONG_BUY", "ADD"},
    "TRIM":       {"BUY", "STRONG BUY", "STRONG_BUY", "ADD"},
    "EXIT":       {"BUY", "STRONG BUY", "STRONG_BUY", "ADD"},
    "CUT":        {"BUY", "STRONG BUY", "STRONG_BUY", "ADD"},
    "HOLD":       set(),
    "KEEP":       set(),
    "WATCH":      set(),
    # WATCH-class aliases: same neutral semantics as WATCH.
    "WAIT-FOR-COOLING":  set(),
    "WAIT-FOR-CATALYST": set(),
}

TICKER_BLACKLIST: frozenset[str] = frozenset(
    {
        "BUY", "SELL", "HOLD", "KEEP", "ADD", "TRIM", "CUT", "EXIT",
        "REDUCE", "AVOID", "WATCH", "FLAG", "SKIP", "STRONG",
        "NAV", "USD", "ILS", "FX", "ETF", "PE", "PEG",
        "YOY", "QOQ", "CC", "GAAP", "EBIT", "EBITDA", "FCF",
        "API", "AI", "ML", "GPU", "CPU", "IP", "OS",
        "US", "UK", "EU", "TASE", "NYSE", "NASDAQ", "SP",
        "CEO", "CFO", "COO", "CTO",
        "RSI", "MA", "DMA", "VIX", "ATH", "ATL", "ATR",
        "OK", "NO", "YES", "TBD", "TLDR", "DYOR",
        "I", "A",
    }
)


def normalize_signal(token: str) -> str:
    """Normalize 'STRONG_BUY' / 'strong buy' / 'Strong  Buy' -> 'STRONG BUY'."""
    s = re.sub(r"\s+", " ", token.strip()).upper()
    s = s.replace("_", " ")
    return s


def find_signal_tokens(text: str) -> list[str]:
    """Return all normalized signal tokens found in `text` (no ticker pairing)."""
    out: list[str] = []
    for m in SIGNAL_MARKER_RE.finditer(text):
        raw = m.group(0).strip("*| ").strip()
        out.append(normalize_signal(raw))
    return out


def find_signals(text: str) -> list[tuple[str, str]]:
    """Extract (ticker, signal) pairs.

    Algorithm: for each signal marker, pair it with the LEFTMOST ticker on the
    same line. If the line has no ticker, fall back to the previous non-empty
    line. This matches the workbench's per-holding row format ("TEVA — REDUCE
    — ...") and table-cell format ("| TEVA | $14.20 | **REDUCE** | ...") where
    the ticker is always the row label.

    Returns deduped list preserving order of first appearance. Hooks needing
    table-cell structure parsing (signal_consistency) keep their own logic and
    just reuse SIGNAL_MARKER_RE / TICKER_RE from this module.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    lines = text.splitlines(keepends=True)
    line_starts: list[int] = []
    pos = 0
    for ln in lines:
        line_starts.append(pos)
        pos += len(ln)

    def _line_index(offset: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _tickers_in(line: str) -> list[str]:
        return [
            t for t in TICKER_RE.findall(line)
            if t not in TICKER_BLACKLIST and len(t) >= 2
        ]

    for m in SIGNAL_MARKER_RE.finditer(text):
        signal = normalize_signal(m.group(0).strip("*| ").strip())
        idx = _line_index(m.start())
        tickers = _tickers_in(lines[idx])
        if not tickers:
            for prev in range(idx - 1, -1, -1):
                tickers = _tickers_in(lines[prev])
                if tickers:
                    break
        if not tickers:
            continue
        leftmost = tickers[0]
        key = (leftmost, signal)
        if key not in seen:
            seen.add(key)
            pairs.append(key)
    return pairs


def line_has_signal(line: str) -> bool:
    return bool(SIGNAL_MARKER_RE.search(line))


def line_has_stop_language(line: str) -> bool:
    return bool(STOP_LANGUAGE_RE.search(line))


def line_negative_with_stop_justification(line: str) -> str | None:
    """If `line` contains a NEGATIVE signal token AND stop-language vocabulary,
    return the matched negative token; else None. Used by the stops-firewall
    hook in Step 4.
    """
    if not line_has_stop_language(line):
        return None
    for token in NEGATIVE_SIGNAL_TOKENS:
        if re.search(r"\b" + re.escape(token) + r"\b", line):
            return token
        if re.search(r"\*{2}" + re.escape(token) + r"\*{2}", line):
            return token
    return None


def _self_test() -> None:
    """Run on `python3 scripts/lib/signal_patterns.py` to catch regex regressions."""
    cases: list[tuple[str, str, bool, str]] = [
        ("bare upper signal",         "TEVA — REDUCE — thesis weakening",                              True,  "REDUCE"),
        ("markdown bold signal",      "TEVA: **STRONG BUY** based on Q4 beat",                          True,  "STRONG BUY"),
        ("underscore strong buy",     "Recommendation: STRONG_BUY",                                     True,  "STRONG BUY"),
        ("table cell signal",         "| TEVA | $14.20 | **REDUCE** | -8% |",                          True,  "REDUCE"),
        ("KEEP catches broad-review", "AAPL — **KEEP** — thesis intact",                                True,  "KEEP"),
        ("casual prose ignored",      "We should hold steady on this and buy lunch later",              False, ""),
        ("REDUCE pre-Step3 was lost", "TEVA REDUCE because debt overhang persists",                     True,  "REDUCE"),
        ("HOLD upper",                "HOLD on META until guidance is clearer",                         True,  "HOLD"),
        ("hold lower (false-pos check)", "we hold our weighting",                                       False, ""),
        ("wait-for-cooling alias",     "MU — **WAIT-FOR-COOLING** — RSI 78",                            True,  "WAIT-FOR-COOLING"),
        ("wait-for-catalyst alias",    "AVGO — WAIT-FOR-CATALYST — Jun 4 earnings",                     True,  "WAIT-FOR-CATALYST"),
    ]
    failed = 0
    for label, text, should_match, expected_token in cases:
        tokens = find_signal_tokens(text)
        matched = len(tokens) > 0
        if matched != should_match:
            print(f"FAIL [{label}]: match={matched} expected={should_match} (text={text!r}, tokens={tokens})")
            failed += 1
            continue
        if should_match and expected_token and expected_token not in tokens:
            print(f"FAIL [{label}]: did not contain {expected_token!r} (tokens={tokens})")
            failed += 1
            continue
        print(f"OK   [{label}] → {tokens}")

    pairs_text = (
        "Per-holding analysis\n"
        "TEVA — **REDUCE** — Q4 weak\n"
        "AAPL — **KEEP** — services growth steady\n"
        "NVDA — **STRONG BUY** — guide raised\n"
    )
    pairs = find_signals(pairs_text)
    print(f"\nfind_signals → {pairs}")
    expected_pairs = {("TEVA", "REDUCE"), ("AAPL", "KEEP"), ("NVDA", "STRONG BUY")}
    missing = expected_pairs - set(pairs)
    if missing:
        print(f"FAIL [find_signals]: missing {missing}")
        failed += 1
    else:
        print("OK   [find_signals]")

    firewall_cases: list[tuple[str, str, str | None]] = [
        ("stop-justified REDUCE",
         "TEVA — REDUCE — Chandelier 3xATR stop triggered at $13.80",
         "REDUCE"),
        ("clean fundamental REDUCE",
         "TEVA — REDUCE — Q4 revenue miss and 2026 guide cut",
         None),
        ("stop reference without negative",
         "TEVA — KEEP — Chandelier stop sits at $13.80 for reference only",
         None),
        ("200DMA break + SELL",
         "AAPL — SELL — broke below 200DMA and trend has flipped",
         "SELL"),
    ]
    for label, line, expected in firewall_cases:
        got = line_negative_with_stop_justification(line)
        if got != expected:
            print(f"FAIL [{label}]: got={got} expected={expected} (line={line!r})")
            failed += 1
        else:
            print(f"OK   [firewall:{label}] → {got}")

    if failed:
        print(f"\n{failed} test(s) failed")
        raise SystemExit(1)
    print(f"\nAll self-tests passed.")


if __name__ == "__main__":
    _self_test()
