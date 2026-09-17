"""Document structure input model (Phase 2-CF3).

A pure, PDF-independent description of a document's outline. PageLens / a PDF
parser / OCR feed this model; the adapter turns it into a ``CurriculumDraft``.
No file access, no Qt, no provider, no PageLens import — ``from_pagelens``
consumes whatever payload the bridge already emits, duck-typed.

Sections form a tree via ``parent_id``:

    第一章 绪论        level=1  position=1
    第二章 数学模型    level=1  position=2
      2.1 微分方程     level=2  position=1 parent=第二章
      2.2 传递函数     level=2  position=2 parent=第二章
    第三章 时域分析    level=1  position=3

Structural invariants (checked by :meth:`DocumentStructure.validate` and
rejected by :func:`build_draft`):

- section ids are unique;
- every level >= 1; level > 1 requires an existing parent with a LOWER level;
- positions are unique among siblings (same parent scope);
- page ranges are (start, end) with 1 <= start <= end;
- titles are non-empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from core.learning.curriculum.models import CurriculumError
from core.learning.models import SourceRef


class DocumentStructureError(CurriculumError):
    """The document structure is broken and cannot become a draft."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_page_range(value: Any) -> tuple[int, int] | None:
    """Accept ``(35, 48)``, ``[35, 48]``, ``"35-48"``, ``35`` or ``None``."""
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            start, end = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+)\s*[-–—至]\s*(\d+)\s*", value)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
        else:
            compact = re.sub(r"\s+", "", value)
            if not compact.isdigit():
                return None
            start = end = int(compact)
    elif isinstance(value, Mapping):
        try:
            start = int(value.get("start") or value.get("first") or value.get("page") or 1)
            end = int(value.get("end") or value.get("last") or start)
        except (TypeError, ValueError):
            return None
    else:
        try:
            start = end = int(value)
        except (TypeError, ValueError):
            return None
    if start < 1:
        raise DocumentStructureError(
            f"page range must start at page 1 or later (got {start})"
        )
    if end < start:
        raise DocumentStructureError(
            f"page range end {end} is before start {start}"
        )
    return (start, end)


# ---------------------------------------------------------------------------
# leaf model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectionConceptHint:
    """A concept term extracted from a section or the page-level OCR signal.

    It is only a PROPOSAL input: it never becomes a Concept row. The adapter
    turns it into a ``ConceptProposal`` carrying its source section.
    """

    name: str
    confidence: float = 0.5
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _text(self.name):
            raise DocumentStructureError("concept hint name must not be empty")
        confidence = float(self.confidence)
        if not (0.0 <= confidence <= 1.0):
            raise DocumentStructureError(
                f"concept hint confidence must be in [0, 1] (got {confidence})"
            )
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "aliases", tuple(_text(a) for a in self.aliases if _text(a)))

    @classmethod
    def from_payload(cls, item: Any) -> "SectionConceptHint":
        """Accept a plain string or a dict (``term``/``name``/``text`` +
        optional ``confidence``/``score`` and ``aliases``)."""
        if isinstance(item, str):
            return cls(name=item)
        if isinstance(item, Mapping):
            name = item.get("term") or item.get("name") or item.get("text")
            confidence = item.get("confidence", item.get("score", 0.5))
            aliases = item.get("aliases") or ()
            return cls(name=_text(name), confidence=confidence, aliases=tuple(aliases))
        raise DocumentStructureError(
            f"concept item must be a string or a mapping (got {type(item).__name__})"
        )


@dataclass(frozen=True, slots=True)
class Section:
    """One outline node. ``position`` is the order among SIBLINGS."""

    id: str
    title: str
    level: int
    position: int
    parent_id: str | None = None
    source_page_range: tuple[int, int] | None = None
    concepts: tuple[SectionConceptHint, ...] = ()

    def __post_init__(self) -> None:
        if not _text(self.id):
            raise DocumentStructureError("section id must not be empty")
        if not _text(self.title):
            raise DocumentStructureError(f"section {self.id!r} title must not be empty")
        if self.level < 1:
            raise DocumentStructureError(
                f"section {self.id!r} level must be >= 1 (got {self.level})"
            )
        if self.level > 1 and not _text(self.parent_id):
            raise DocumentStructureError(
                f"section {self.id!r} level {self.level} needs a parent_id"
            )
        object.__setattr__(self, "id", _text(self.id))
        object.__setattr__(self, "title", _text(self.title))
        object.__setattr__(self, "parent_id", _text(self.parent_id) or None)
        object.__setattr__(
            self, "source_page_range", _normalize_page_range(self.source_page_range)
        )
        # Accept hint payloads (str/dict) as well as SectionConceptHint objects.
        object.__setattr__(
            self,
            "concepts",
            tuple(
                hint
                if isinstance(hint, SectionConceptHint)
                else SectionConceptHint.from_payload(hint)
                for hint in self.concepts
            ),
        )

    @property
    def page_label(self) -> str:
        if self.source_page_range is None:
            return ""
        start, end = self.source_page_range
        return f"p.{start}" if start == end else f"p.{start}-{end}"

    @classmethod
    def from_payload(cls, item: Mapping[str, Any]) -> "Section":
        section_id = (
            _text(item.get("id"))
            or f"section:{item.get('level', 1)}:{item.get('position', 0)}"
        )
        concepts = tuple(
            SectionConceptHint.from_payload(hint)
            for hint in (item.get("concepts") or ())
        )
        return cls(
            id=section_id,
            title=item.get("title"),
            level=item.get("level", 1),
            position=item.get("position", 0),
            parent_id=item.get("parent_id"),
            source_page_range=_normalize_page_range(
                item.get("source_page_range", item.get("page_range", item.get("pages")))
            ),
            concepts=concepts,
        )


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentStructure:
    """PDF-independent outline of a document (design §三)."""

    document_id: str
    title: str
    author: str = ""
    sections: tuple[Section, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    page_concepts: tuple[SectionConceptHint, ...] = ()
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not _text(self.document_id):
            raise DocumentStructureError("document_id must not be empty")
        if not _text(self.title):
            raise DocumentStructureError("document title must not be empty")
        object.__setattr__(self, "sections", tuple(self.sections))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(
            self,
            "page_concepts",
            tuple(
                hint
                if isinstance(hint, SectionConceptHint)
                else SectionConceptHint.from_payload(hint)
                for hint in self.page_concepts
            ),
        )
        object.__setattr__(self, "author", _text(self.author))
        object.__setattr__(self, "document_id", _text(self.document_id))
        object.__setattr__(self, "title", _text(self.title))

    # -- validity -----------------------------------------------------------

    def validate(self) -> list[str]:
        """Structural problems as deterministic, human-readable messages."""
        problems: list[str] = []
        ids: dict[str, Section] = {}
        for section in self.sections:
            if section.id in ids:
                problems.append(f"duplicate section id {section.id!r}")
            ids[section.id] = section
        for section in self.sections:
            if section.level > 1:
                parent = ids.get(section.parent_id) if section.parent_id else None
                if parent is None:
                    problems.append(
                        f"section {section.id!r} parent {section.parent_id!r} not found"
                    )
                elif parent.level >= section.level:
                    problems.append(
                        f"section {section.id!r} (level {section.level}) has parent "
                        f"{parent.id!r} (level {parent.level}) — parent level must be lower"
                    )
        scopes: dict[str | None, dict[int, str]] = {}
        for section in self.sections:
            scope = section.parent_id if section.level > 1 else None
            seen = scopes.setdefault(scope, {})
            if section.position in seen:
                problems.append(
                    f"sections {seen[section.position]!r} and {section.id!r} share "
                    f"position {section.position} in the same scope"
                )
            else:
                seen[section.position] = section.id
        return problems

    def raise_if_invalid(self) -> None:
        problems = self.validate()
        if problems:
            raise DocumentStructureError(
                "broken document structure: " + "; ".join(problems)
            )

    # -- section helpers ----------------------------------------------------

    @property
    def chapter_sections(self) -> tuple[Section, ...]:
        """level-1 sections as the chapter spine, in position order."""
        return tuple(
            sorted(
                (section for section in self.sections if section.level == 1),
                key=lambda s: (s.position, s.id),
            )
        )

    def children_of(self, section_id: str) -> tuple[Section, ...]:
        return tuple(
            sorted(
                (section for section in self.sections if section.parent_id == section_id),
                key=lambda s: (s.position, s.id),
            )
        )

    def nearest_level1_ancestor(self, section_id: str) -> Section | None:
        """The owning chapter of a nested section (walking up the tree)."""
        by_id = {section.id: section for section in self.sections}
        current = by_id.get(section_id)
        while current is not None and current.level != 1:
            current = by_id.get(current.parent_id) if current.parent_id else None
        return current

    # -- PageLens bridge ----------------------------------------------------

    @classmethod
    def from_pagelens(
        cls,
        event: Any = None,
        *,
        concepts: Iterable[Any] | None = None,
        outline: Iterable[Any] | None = None,
        title: str | None = None,
        author: str | None = None,
        url: str | None = None,
        document_id: str | None = None,
        fingerprint: str | None = None,
    ) -> "DocumentStructure":
        """Build a structure from the data PageLens ALREADY emits — read-only,
        duck-typed, never importing the bridge.

        ``event`` may be a bridge payload (``pdf_opened`` / ``page_context``
        / any mapping with ``title``/``url``/``document_id``) or any object
        exposing those attributes. Keyword arguments override the event.
        ``outline`` maps to sections (forward-compatible: today's bridge emits
        none), ``concepts`` maps to page-level :class:`SectionConceptHint`\
        s that the adapter collects into a separate auto-review chapter.
        """
        payload: Mapping[str, Any] = {}
        if isinstance(event, Mapping):
            payload = event
        elif event is not None:
            for attr in ("document_id", "title", "author", "url", "fingerprint"):
                value = getattr(event, attr, None)
                if value is not None:
                    payload = {**payload, attr: value}
        page_context = payload.get("page_context")
        page_context = page_context if isinstance(page_context, Mapping) else {}

        document_id_value = (
            _text(document_id)
            or _text(payload.get("document_id"))
            or _text(payload.get("doc_id"))
            or _text(url)
            or _text(payload.get("url"))
            or _text(page_context.get("url"))
            or "pagelens:unknown"
        )
        title_value = (
            _text(title)
            or _text(payload.get("title"))
            or _text(page_context.get("title"))
            or "未命名 PDF 文档"
        )
        author_value = _text(author) or _text(payload.get("author"))

        outline_items = outline
        if outline_items is None:
            for key in ("outline", "sections", "toc", "outline_items"):
                if isinstance(payload.get(key), (list, tuple)):
                    outline_items = payload[key]
                    break
        sections = _coerce_sections(outline_items)

        concept_items = (
            list(concepts)
            if concepts is not None
            else list(payload.get("items") or payload.get("concepts") or ())
        )
        page_concepts = tuple(SectionConceptHint.from_payload(item) for item in concept_items)

        return cls(
            document_id=document_id_value,
            title=title_value,
            author=author_value,
            sections=sections,
            page_concepts=page_concepts,
            source_refs=(SourceRef(
                source_type="pdf",
                document_id=document_id_value,
                url=(
                    _text(url)
                    or _text(payload.get("url"))
                    or _text(page_context.get("url"))
                    or None
                ),
            ),),
            fingerprint=fingerprint or payload.get("fingerprint"),
        )


def _coerce_sections(items: Any) -> tuple[Section, ...]:
    if items is None:
        return ()
    sections: list[Section] = []
    for item in items:
        if isinstance(item, Section):
            sections.append(item)
        elif isinstance(item, Mapping):
            sections.append(Section.from_payload(item))
        else:
            raise DocumentStructureError(
                f"outline item must be a mapping or Section (got {type(item).__name__})"
            )
    return tuple(sections)