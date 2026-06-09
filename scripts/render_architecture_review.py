#!/usr/bin/env python3
"""render_architecture_review.py — convert manager-synthesis.md + reviewer
outputs into a single standalone HTML file at <out_dir>/report.html.

Usage:
    python3 scripts/render_architecture_review.py <out_dir>

Where <out_dir> is the timestamped directory created by /architecture-review,
e.g. research/architecture-review/2026-05-28T16-43/. The directory is expected
to contain:
  - manager-synthesis.md         (required — the synthesized report)
  - review-stock-analyst.md      (collapsible appendix)
  - review-equity-research.md    (collapsible appendix)
  - review-system-architect.md   (collapsible appendix)
  - cross-review-*.md            (collapsible appendix, three files)

Missing files are tolerated — they're omitted from the HTML rather than
breaking the render. No external CSS / JS — everything is inline so the file
opens cleanly from disk in any browser.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from markdown_it import MarkdownIt

md = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": True})
md.enable("table")
md.enable("strikethrough")


CSS = """
:root {
  --bg: #0f1419;
  --bg-card: #1a2128;
  --bg-soft: #232b35;
  --fg: #e6e9ef;
  --fg-muted: #9aa5b1;
  --accent: #6ea8ff;
  --keep: #2ea043;
  --fix: #f85149;
  --consider: #d29922;
  --border: #30363d;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 15px;
  line-height: 1.55;
}
.container {
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 24px 96px;
}
header.top {
  border-bottom: 1px solid var(--border);
  padding-bottom: 18px;
  margin-bottom: 28px;
}
header.top h1 {
  font-size: 28px;
  margin: 0 0 6px;
  letter-spacing: -0.01em;
}
header.top .meta {
  color: var(--fg-muted);
  font-size: 13px;
}
nav.toc {
  position: sticky;
  top: 12px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 16px;
  margin-bottom: 24px;
  font-size: 13px;
  z-index: 10;
}
nav.toc a {
  color: var(--accent);
  text-decoration: none;
  margin-right: 14px;
  white-space: nowrap;
}
nav.toc a:hover { text-decoration: underline; }
section.synthesis {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 24px 28px;
  margin-bottom: 28px;
}
section.synthesis h1,
section.synthesis h2 {
  margin-top: 1.4em;
  padding-top: 6px;
  border-top: 1px solid var(--border);
}
section.synthesis h1:first-child,
section.synthesis h2:first-of-type {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}
section.synthesis h1 { font-size: 24px; }
section.synthesis h2 { font-size: 19px; color: var(--accent); }
section.synthesis h3 { font-size: 16px; color: var(--fg); margin-top: 1.6em; }
section.synthesis p { margin: 0.6em 0; }
section.synthesis ul, section.synthesis ol { padding-left: 1.6em; }
section.synthesis li { margin: 0.3em 0; }
section.synthesis code {
  background: var(--bg-soft);
  border-radius: 4px;
  padding: 1px 6px;
  font-family: "SF Mono", "Menlo", Consolas, monospace;
  font-size: 0.88em;
}
section.synthesis pre {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 18px;
  overflow-x: auto;
  font-family: "SF Mono", "Menlo", Consolas, monospace;
  font-size: 0.85em;
  line-height: 1.5;
}
section.synthesis pre code { background: transparent; padding: 0; }
section.synthesis blockquote {
  border-left: 3px solid var(--accent);
  margin: 1em 0;
  padding: 4px 16px;
  color: var(--fg-muted);
  background: var(--bg-soft);
  border-radius: 0 6px 6px 0;
}
section.synthesis table {
  width: 100%;
  border-collapse: collapse;
  margin: 1em 0;
  font-size: 14px;
}
section.synthesis th, section.synthesis td {
  border: 1px solid var(--border);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}
section.synthesis th {
  background: var(--bg-soft);
  font-weight: 600;
  color: var(--accent);
}
section.synthesis tr:nth-child(even) { background: rgba(255,255,255,0.018); }
/* Color-code rows by section based on the heading just above the table. */
section.synthesis h2:has(+ table) + table tr td:first-child { font-weight: 600; }
section.synthesis h2#what-to-keep-do-not-touch ~ table th,
section.synthesis h2[id*="keep"] ~ table th { color: var(--keep); }
section.synthesis h2[id*="fix"] ~ table th { color: var(--fix); }
section.synthesis h2[id*="consider"] ~ table th { color: var(--consider); }
details.appendix {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 14px;
  padding: 0;
}
details.appendix summary {
  cursor: pointer;
  padding: 14px 18px;
  font-weight: 600;
  font-size: 15px;
  color: var(--accent);
  user-select: none;
  list-style: none;
}
details.appendix summary::-webkit-details-marker { display: none; }
details.appendix summary::before {
  content: "▸ ";
  display: inline-block;
  margin-right: 6px;
  transition: transform 0.15s;
}
details.appendix[open] summary::before {
  transform: rotate(90deg);
}
details.appendix > .body {
  padding: 4px 22px 18px;
  border-top: 1px solid var(--border);
}
details.appendix > .body h1 { font-size: 18px; margin-top: 14px; }
details.appendix > .body h2 { font-size: 16px; color: var(--accent); margin-top: 16px; }
details.appendix > .body h3 { font-size: 14px; margin-top: 14px; }
footer {
  margin-top: 36px;
  padding-top: 18px;
  border-top: 1px solid var(--border);
  color: var(--fg-muted);
  font-size: 12px;
}
.section-label {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-right: 6px;
}
.section-label.analyst { background: rgba(110,168,255,0.18); color: #6ea8ff; }
.section-label.equity   { background: rgba(210,153,34,0.18); color: #d29922; }
.section-label.architect{ background: rgba(46,160,67,0.18); color: #2ea043; }
.section-label.manager  { background: rgba(248,81,73,0.18); color: #f85149; }
"""


APPENDIX_SPECS = [
    ("review-stock-analyst.md", "Stock-analyst reviewer — full review", "analyst"),
    ("review-equity-research.md", "Equity-research expert — full review", "equity"),
    ("review-system-architect.md", "System architect — full review", "architect"),
    ("cross-review-stock-analyst.md", "Stock-analyst — cross-review addendum", "analyst"),
    ("cross-review-equity-research.md", "Equity-research — cross-review addendum", "equity"),
    ("cross-review-system-architect.md", "System architect — cross-review addendum", "architect"),
]


def _render_md_to_html(md_text: str) -> str:
    return md.render(md_text)


def _slugify(text: str) -> str:
    return (
        text.lower()
        .strip()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("—", "-")
        .replace("(", "")
        .replace(")", "")
        .replace(":", "")
        .replace(",", "")
        .replace(".", "")
    )


def _add_heading_ids(html: str) -> str:
    """Add id attributes to h2 headings so the CSS attribute-selectors can
    color-code KEEP / FIX / CONSIDER tables. Minimal regex pass."""
    import re

    def repl(m: "re.Match[str]") -> str:
        text = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return f'<h2 id="{_slugify(text)}">{m.group(1)}</h2>'

    return re.sub(r"<h2>(.*?)</h2>", lambda m: repl(m), html, flags=re.DOTALL)


def _build_toc() -> str:
    items = [
        ("Executive summary", "executive-summary"),
        ("What to KEEP", "what-to-keep-do-not-touch"),
        ("What to FIX urgently", "what-to-fix-urgently-high-confidence"),
        ("What to CONSIDER", "what-to-consider-lower-confidence-or-single-reviewer"),
        ("Tensions", "genuine-tensions"),
        ("Diff suggestions", "concrete-diff-recommendations"),
        ("What to build NEXT", "what-to-build-next"),
        ("Risks of doing nothing", "risks-of-doing-nothing"),
        ("Appendix: full reviews", "appendix-full-reviews"),
    ]
    return " · ".join(f'<a href="#{slug}">{label}</a>' for label, slug in items)


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: render_architecture_review.py <out_dir>", file=sys.stderr)
        sys.exit(2)

    out_dir = Path(sys.argv[1]).resolve()
    if not out_dir.is_dir():
        print(f"error: {out_dir} is not a directory", file=sys.stderr)
        sys.exit(2)

    synth_path = out_dir / "manager-synthesis.md"
    if not synth_path.exists():
        print(f"error: {synth_path} does not exist — run Phase 3 first", file=sys.stderr)
        sys.exit(1)

    synthesis_md = synth_path.read_text(encoding="utf-8")
    synthesis_html = _add_heading_ids(_render_md_to_html(synthesis_md))

    appendix_html_parts = []
    for fname, title, kind in APPENDIX_SPECS:
        p = out_dir / fname
        if not p.exists():
            continue
        body_html = _render_md_to_html(p.read_text(encoding="utf-8"))
        appendix_html_parts.append(
            f'<details class="appendix" id="app-{fname}">'
            f'<summary><span class="section-label {kind}">{kind}</span>{title}</summary>'
            f'<div class="body">{body_html}</div>'
            f"</details>"
        )
    appendix_html = "\n".join(appendix_html_parts)
    if appendix_html:
        appendix_html = (
            '<h2 id="appendix-full-reviews">Appendix — full reviewer outputs</h2>\n'
            + appendix_html
        )

    now = dt.datetime.now().isoformat(timespec="seconds")
    out_name = out_dir.name

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Architecture review — {out_name}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">
  <header class="top">
    <h1>Architecture review — {out_name}</h1>
    <div class="meta">Generated {now} · investing-workbench multi-reviewer pipeline</div>
  </header>

  <nav class="toc">{_build_toc()}</nav>

  <section class="synthesis">
    {synthesis_html}
  </section>

  {appendix_html}

  <footer>
    Rendered by <code>scripts/render_architecture_review.py</code>.
    Inputs: <code>manager-synthesis.md</code> and the reviewer / cross-review
    markdown files in <code>{out_dir}</code>. No external dependencies — this
    file opens offline.
  </footer>
</div>
</body>
</html>
"""

    out_html = out_dir / "report.html"
    out_html.write_text(html, encoding="utf-8")
    print(f"wrote {out_html} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
