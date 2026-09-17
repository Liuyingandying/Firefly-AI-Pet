"""Minimal markdown -> QTextDocument-safe HTML for chat surfaces.

QTextEdit / QTextBrowser render Qt's HTML subset natively but show markdown
markers (``**bold**``) literally. This module converts the small markdown
vocabulary the assistant actually emits — bold, bullets, ``#`` headings,
inline code — into escaped HTML, so plain chat text stays visually identical
(everything is escaped first) while formatted text stops leaking markers.

The output is always wrapped in a ``<span>`` so Qt reliably takes the rich
text path; ``&lt;``-style escapes would otherwise render literally.
"""

from __future__ import annotations

import html
import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_CODE_RE = re.compile(r"`([^`]*)`")
_LIST_RE = re.compile(r"^(\s*)[*\-•·]\s+(.*)$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")

# HTML <sub>/<sup> → Unicode（论文常见上下标：AI 输出 E<sub>0</sub>=mc<sup>2</sup>
# 这类形态时，escape 会把标签变成字面文本；转为 Unicode 后正常显示且保持防注入）。
_SUB_HTML_RE = re.compile(r"<sub>(.*?)</sub>", re.S | re.I)
_SUP_HTML_RE = re.compile(r"<sup>(.*?)</sup>", re.S | re.I)

_SUBSCRIPT_MAP = str.maketrans({
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "−": "₋", "=": "₌",
    "(": "₍", ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ",
    "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ",
    "s": "ₛ", "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
})
_SUPERSCRIPT_MAP = str.maketrans({
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼",
    "(": "⁽", ")": "⁾", "n": "ⁿ", "i": "ⁱ",
})

# QTextDocument 会把任何输入转换为它自己的标准标签子集；toHtml() 输出若
# 出现该白名单之外的标签，说明上游注入了未转义的 HTML（验收测试用）。
_KNOWN_QT_TAGS = frozenset({
    "html", "head", "meta", "style", "title", "body", "p", "span", "br",
    "div", "b", "i", "u", "s", "code", "pre", "em", "strong", "sub", "sup",
    "table", "tbody", "thead", "tr", "td", "th", "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "a", "font", "img",
    "blockquote", "center", "nobr",
})


def _inline(escaped: str) -> str:
    """Format inline markers on already-escaped text."""
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _CODE_RE.sub(r"<code>\1</code>", escaped)
    return escaped


def _sub_sup_to_unicode(text: str) -> str:
    """Convert literal HTML <sub>/<sup> into Unicode sub/superscripts.

    Runs before html.escape so AI-emitted `E<sub>0</sub>=mc<sup>2</sup>`
    renders as E₀=mc² instead of leaking raw tags as literal text. Unmapped
    characters keep their original form (safe no-op per character).
    """
    text = _SUB_HTML_RE.sub(lambda m: m.group(1).translate(_SUBSCRIPT_MAP), text)
    text = _SUP_HTML_RE.sub(
        lambda m: m.group(1).translate(_SUPERSCRIPT_MAP), text)
    return text


def markdown_to_html(text: str) -> str:
    """Render assistant text as QTextDocument-friendly HTML.

    Plain text without markdown passes through escaped and visually
    unchanged; markdown lines are converted line by line.
    """
    if not text:
        return ""
    text = _sub_sup_to_unicode(text)
    rendered: list[str] = []
    for raw in text.splitlines():
        m = _HEADING_RE.match(raw)
        if m:
            rendered.append(f"<b>{html.escape(m.group(1))}</b>")
            continue
        m = _LIST_RE.match(raw)
        if m:
            indent = "&nbsp;" * len(m.group(1).replace("\t", "    "))
            rendered.append(f"{indent}•&nbsp;{_inline(html.escape(m.group(2)))}")
            continue
        rendered.append(_inline(html.escape(raw)))
    body = "<br>".join(rendered)
    return f"<span>{body}</span>"


__all__ = ["markdown_to_html"]
