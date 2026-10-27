"""Text helpers: HTML to plain text, normalization."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

_BLOCK_TAGS = re.compile(r"(?i)</?(p|div|br|ul|ol|h[1-6]|tr|section|article)[^>]*>")
_LI_OPEN = re.compile(r"(?i)<li[^>]*>")
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")
_SCRIPT = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def html_to_text(raw: str | None) -> str:
    """Fast, dependency-free HTML -> readable plain text."""
    if not raw:
        return ""
    s = html.unescape(raw) if "&lt;" in raw else raw   # Greenhouse double-escapes content
    s = _SCRIPT.sub(" ", s)
    s = _LI_OPEN.sub("\n- ", s)
    s = _BLOCK_TAGS.sub("\n", s)
    s = _TAGS.sub(" ", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return _BLANKS.sub("\n\n", s).strip()


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def to_iso_date(value) -> str | None:
    """Accepts epoch ms, epoch s, or ISO strings; returns YYYY-MM-DD."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        s = str(value).strip()
        if s.isdigit():
            return to_iso_date(int(s))
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, OSError):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
        return m.group(1) if m else None


def truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + " …[truncated]"
