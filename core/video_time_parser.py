"""Lightweight Chinese/numeric time-expression parser for video follow-ups.

Handles the phrases users actually type after reading a video:

    "刚才2分钟那里" / "两分钟" / "2分30秒" / "90秒" / "1:30" / "第2分钟"
    "开头" / "最开始" / "一开始" / "片头"  ->  0.0
    "最后" / "结尾" / "片尾" / "末尾"      ->  duration_hint - 0.5

No NLP model: ordered regex passes over the raw message. Returns the
leftmost match's seconds, or None when the message carries no time
expression. "最后" needs a duration hint; without one it yields None so the
caller falls back to the ordinary chat path.
"""

from __future__ import annotations

import re

_CJK_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
               "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d+|[零一二两三四五六七八九十]+)"

_MINUTE_SEC_RE = re.compile(  # 2分钟 / 两分钟 / 2分30秒 / 第2分钟
    _NUM + r"\s*分\s*(" + _NUM + r")?\s*秒?(?:钟)?")
_SECOND_RE = re.compile(_NUM + r"\s*秒")
_CLOCK_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\b")
_START_RE = re.compile(r"(最开始|最开头|一开始|开头|片头|起头)")
_END_RE = re.compile(r"(最后|结尾|片尾|末尾)")


def _cjk_to_int(text: str) -> int:
    """Convert 0-99 style Chinese numerals (零一二两三四五六七八九十)."""
    if text.isdigit():
        return int(text)
    total = 0
    if "十" in text:
        left, _, right = text.partition("十")
        total += (_cjk_to_int(left) if left else 1) * 10
        if right:
            total += _cjk_to_int(right)
        return total
    return _CJK_DIGITS.get(text, 0)


def _to_int(num_text: str) -> int:
    return _cjk_to_int(num_text)


def parse_video_time_expression(text: str, duration_hint: float | None = None) -> float | None:
    """Return the timestamp in seconds for the first time expression, or None."""
    if not text:
        return None

    m = _MINUTE_SEC_RE.search(text)
    if m:
        minutes = _to_int(m.group(1))
        seconds = _to_int(m.group(2)) if m.group(2) else 0
        return float(minutes * 60 + seconds)

    m = _CLOCK_RE.search(text)
    if m:
        total = int(m.group(1)) * 60 + int(m.group(2))
        if m.group(3):
            total = total * 60 + int(m.group(3))
        return float(total)

    m = _SECOND_RE.search(text)
    if m:
        return float(_to_int(m.group(1)))

    if _START_RE.search(text):
        return 0.0

    if _END_RE.search(text):
        if duration_hint and duration_hint > 0:
            return max(0.0, float(duration_hint) - 0.5)
        return None
    return None


def format_timestamp(seconds: float) -> str:
    """Render seconds as a friendly "2分钟" / "2分44秒" / "1小时2分3秒"."""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒" if secs else f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟" if secs == 0 else f"{minutes}分{secs}秒"
    return f"{secs}秒"


__all__ = ["parse_video_time_expression", "format_timestamp"]
