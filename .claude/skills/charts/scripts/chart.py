#!/usr/bin/env python3
"""Portfolio charts — PoC: sector allocation donut."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # no display; write PNGs only
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.lib import io as pio  # noqa: E402


def _run_sector_allocation(top_n: int) -> dict:
    """Shell out to the sector-allocation skill so numbers stay single-sourced."""
    script = ROOT / ".claude/skills/sector-allocation/scripts/sectors.py"
    proc = subprocess.run(
        ["python3", str(script)],
        input=json.dumps({"top_n_positions": top_n}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sector-allocation failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _group_top_n(by_sector: dict[str, float], top_n: int) -> list[tuple[str, float]]:
    items = sorted(by_sector.items(), key=lambda kv: -kv[1])
    if len(items) <= top_n:
        return items
    head = items[:top_n]
    other_weight = sum(w for _, w in items[top_n:])
    head.append(("Other", other_weight))
    return head


def _fmt_nav(alloc: dict) -> str:
    ils = alloc.get("nav_ils")
    usd = alloc.get("nav_usd")
    fx_as_of = alloc.get("fx_as_of") or "?"
    ils_s = f"₪{ils:,.0f}" if isinstance(ils, (int, float)) else "₪?"
    usd_s = f"~${usd:,.0f}" if isinstance(usd, (int, float)) else "~$?"
    return f"{ils_s} ({usd_s}, FX {fx_as_of})"


def allocation_donut(params: dict) -> dict:
    top_n = int(params.get("top_n", 8))
    out_dir_override = params.get("out_dir")

    alloc = _run_sector_allocation(top_n)
    by_sector = alloc.get("by_sector") or {}
    if not by_sector:
        return {
            "kind": "allocation-donut",
            "chart_path": None,
            "data_as_of": alloc.get("data_as_of"),
            "warnings": ["sector-allocation returned no by_sector data"],
        }

    items = _group_top_n(by_sector, top_n)
    labels = [k for k, _ in items]
    sizes = [v for _, v in items]

    today = dt.date.today().isoformat()
    out_dir = Path(out_dir_override) if out_dir_override else ROOT / "research" / "daily" / today / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "allocation-donut.png"

    fig, ax = plt.subplots(figsize=(8, 6), dpi=130)
    colors = plt.get_cmap("tab20").colors[: len(items)]

    def _autopct(pct: float) -> str:
        return f"{pct:.1f}%" if pct >= 3 else ""

    wedges, texts, autotexts = ax.pie(
        sizes,
        labels=labels,
        autopct=_autopct,
        startangle=90,
        counterclock=False,
        pctdistance=0.78,
        wedgeprops=dict(width=0.38, edgecolor="white", linewidth=1.5),
        colors=colors,
        textprops=dict(fontsize=10),
    )
    for t in autotexts:
        t.set_color("white")
        t.set_fontweight("bold")

    # Centered NAV label
    nav_line = _fmt_nav(alloc)
    ax.text(0, 0.05, "NAV", ha="center", va="center", fontsize=11, color="#555")
    ax.text(0, -0.10, nav_line.split(" (")[0], ha="center", va="center",
            fontsize=14, fontweight="bold")
    if "(" in nav_line:
        ax.text(0, -0.22, "(" + nav_line.split("(", 1)[1], ha="center", va="center",
                fontsize=9, color="#777")

    ax.set_title(f"Sector allocation — {today}", fontsize=13, pad=14)
    ax.axis("equal")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    rel_path = out_path.relative_to(ROOT)
    caption = f"Sector allocation as of {today} — NAV {nav_line}."
    return {
        "kind": "allocation-donut",
        "chart_path": str(rel_path),
        "absolute_path": str(out_path),
        "caption": caption,
        "data_as_of": alloc.get("data_as_of"),
        "top_sectors": [{"sector": k, "weight": round(v, 4)} for k, v in items],
        "warnings": alloc.get("warnings", []),
    }


DISPATCH = {
    "allocation-donut": allocation_donut,
}


def main() -> None:
    try:
        raw = sys.stdin.read() or "{}"
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}

    kind = params.get("kind", "allocation-donut")
    handler = DISPATCH.get(kind)
    if handler is None:
        print(json.dumps({
            "error": f"unknown kind '{kind}'",
            "supported": sorted(DISPATCH.keys()),
        }, indent=2))
        sys.exit(1)

    try:
        out = handler(params)
    except Exception as e:
        print(json.dumps({"error": str(e), "kind": kind}, indent=2))
        sys.exit(1)

    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
