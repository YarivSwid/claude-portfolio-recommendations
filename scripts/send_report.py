#!/usr/bin/env python3
"""send_report.py — send today's daily report via Telegram.

Reads research/daily/<today>/report.md and sends it as a Telegram message.
Long reports are split into chunks (Telegram max 4096 chars per message).

.env format:
  TELEGRAM_BOT_TOKEN=7123456789:AAF...
  TELEGRAM_CHAT_ID=123456789
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MSG = 4000  # stay under Telegram's 4096 char limit


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _send(token: str, chat_id: str, text: str) -> None:
    url = TELEGRAM_API.format(token=token, method="sendMessage")
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def _extract_section(text: str, heading: str) -> str:
    """Extract content of first markdown section matching heading (## heading...)."""
    import re
    pattern = rf"##[^#].*{re.escape(heading)}.*\n(.*?)(?=\n##|\Z)"
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _split(text: str, max_len: int = MAX_MSG) -> list[str]:
    """Split on paragraph boundaries so messages don't cut mid-sentence."""
    chunks, current = [], []
    length = 0
    for para in text.split("\n\n"):
        if length + len(para) + 2 > max_len and current:
            chunks.append("\n\n".join(current))
            current, length = [], 0
        current.append(para)
        length += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def main() -> None:
    env = _load_env()
    token   = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print(json.dumps({"error": "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env"}))
        sys.exit(1)

    today = dt.date.today().isoformat()
    report_path = ROOT / "research" / "daily" / today / "report.md"

    if not report_path.exists():
        print(json.dumps({"error": f"report.md not found at {report_path}"}))
        sys.exit(1)

    content = report_path.read_text(encoding="utf-8")

    # Extract TL;DR section and send it first as a standalone message
    tldr = _extract_section(content, "TL;DR")
    if tldr:
        safe_tldr = tldr.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        _send(token, chat_id, f"<b>📊 {today} — Today's Priorities</b>\n\n{safe_tldr}")

    # Send full report in chunks
    chunks = _split(content)
    for i, chunk in enumerate(chunks):
        prefix = f"<b>📊 Full Report — {today}</b>\n\n" if i == 0 else ""
        safe   = chunk.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        _send(token, chat_id, prefix + safe)

    print(json.dumps({
        "sent":        True,
        "chunks":      len(chunks),
        "report_path": str(report_path.relative_to(ROOT)),
    }))


if __name__ == "__main__":
    main()
