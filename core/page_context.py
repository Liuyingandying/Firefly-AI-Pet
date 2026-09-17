"""PageConceptContext — page-level reading context for PaperLens 2.2.

Pure logic: no LLM, no Memory, no database. Sources are the already-parsed
``LazyPdfIndex`` (``core.pdf_processor``): per-page native text (1-based)
and PDF bookmarks (``outline``).

Fields:
  - section : nearest preceding bookmark title; heading-heuristic fallback
  - keywords: local word frequency on the page text (stopword filtered)
  - summary : heading line + first-sentence heuristic
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.document_attachment import _first_nonempty_line

_MAX_KEYWORDS = 8
_MAX_SUMMARY_CHARS = 180
_MAX_SECTION_CHARS = 60

# Lightweight academic stopword set (English primarily; papers are mostly EN).
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for",
    "with", "as", "by", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "this", "that", "these", "those", "we", "our", "us", "i",
    "you", "they", "their", "them", "he", "she", "his", "her", "not", "no",
    "but", "from", "than", "then", "when", "where", "which", "who", "whom",
    "whose", "can", "could", "may", "might", "must", "shall", "should",
    "will", "would", "do", "does", "did", "done", "have", "has", "had",
    "been", "into", "over", "under", "between", "among", "after", "before",
    "during", "while", "about", "against", "also", "only", "just", "so",
    "such", "very", "more", "most", "some", "any", "all", "each", "both",
    "few", "other", "another", "one", "two", "three", "use", "used", "using",
    "via", "per", "et", "al", "fig", "figs", "table", "tables", "eq", "eqs",
    "etc", "e.g", "i.e", "cf", "vs", "doi", "arxiv", "page", "pages",
    "基于", "一个", "以及", "对于", "通过", "可以", "进行", "并且", "其中",
})

_WORD_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]+(?:[-'][A-Za-z\u4e00-\u9fff]+)*")


@dataclass(frozen=True)
class PageConceptContext:
    """One page's reading context (page / section / keywords / summary)."""

    page: int
    section: str = ""
    keywords: list[str] = field(default_factory=list)
    summary: str = ""


def build_page_concept(index, page: int) -> PageConceptContext:
    """Build the context for one 1-based page from a LazyPdfIndex.

    ``index`` is a ``core.pdf_processor.LazyPdfIndex`` (or any object with
    ``native_text`` / ``outline``). Pure and deterministic — no I/O beyond
    reading the already-parsed structures.
    """
    page = int(page)
    text = str((index.native_text.get(page, "") or "")).strip()
    section = _section_for(index, page, text)
    keywords = _keywords_for(text)
    summary = _summary_for(text, section)
    return PageConceptContext(
        page=page, section=section, keywords=keywords, summary=summary
    )


# ---------------------------------------------------------------------------
# section
# ---------------------------------------------------------------------------


def _section_for(index, page: int, text: str) -> str:
    """Nearest preceding bookmark title; heading heuristic as fallback."""
    outline = getattr(index, "outline", None) or []
    best = ""
    for entry in outline:
        try:
            title, entry_page = entry[0], int(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if entry_page <= page:
            best = str(title or "").strip()
        else:
            break
    if best:
        return best[:_MAX_SECTION_CHARS]
    return _heuristic_section(text)


def _heuristic_section(text: str) -> str:
    """Heading-style first line (short, non-punctuation-heavy) or empty."""
    first = _first_nonempty_line(text)
    if not first:
        return ""
    first = first.strip()
    # A heading is usually a short line; long lines are prose, not titles.
    if len(first) <= _MAX_SECTION_CHARS and not first.endswith((".", "。", ";")):
        return first
    return ""


# ---------------------------------------------------------------------------
# keywords
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text.lower()):
        token = match.group(0).strip("-'")
        if len(token) >= 2 and token not in _STOPWORDS:
            tokens.append(token)
    return tokens


def _keywords_for(text: str) -> list[str]:
    counts: dict[str, int] = {}
    for token in _tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [token for token, _count in ranked[:_MAX_KEYWORDS]]


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def _summary_for(text: str, section: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    # Skip the heading line when it equals the detected section.
    start = 1 if section and lines[0] == section else 0
    body = " ".join(lines[start:])
    sentence = _first_sentence(body)
    return sentence[:_MAX_SUMMARY_CHARS]


def _first_sentence(text: str) -> str:
    match = re.search(r"[^。.!?]*[。.!?]", text)
    if match:
        return match.group(0).strip()
    return text.strip()


__all__ = ["PageConceptContext", "build_page_concept"]