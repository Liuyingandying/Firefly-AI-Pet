"""DocumentStructure -> CurriculumDraft adapter (Phase 2-CF3).

Turns a pure :class:`DocumentStructure` into a ``CurriculumDraft`` — never
into a ``Curriculum``. Mapping rules (design §四/§五):

- level-1 sections  -> ChapterDraft
- sub-sections      -> the owning chapter's ``description`` digest
- section concepts  -> ConceptProposal placed in the owning chapter
- page-level OCR    -> a trailing auto-review chapter ("全书概念（自动识别）")
- every chapter carries document-level provenance; the draft always carries at
  least one ``CurriculumSource``

The adapter NEVER writes to any database and NEVER creates a Concept — the
proposals it emits only enter the draft, and only the explicit user-confirmed
``CurriculumStore.confirm_draft`` (via the review service) may activate them.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from typing import Iterable

from core.learning.curriculum.adapter.documents import (
    DocumentStructure,
    DocumentStructureError,
    Section,
)
from core.learning.curriculum.models import (
    ChapterConceptDraft,
    ChapterDraft,
    ConceptProposal,
    CurriculumDraft,
    CurriculumSource,
    DraftOrigin,
    LearningPathDraft,
    PathStepDraft,
    SourceKind,
    normalize_name,
)
from core.learning.models import SourceRef, utc_now_iso


class DraftBuildError(DocumentStructureError):
    """The structure is valid but could not become a draft (kept for symmetric
    error handling; the documented invariants already reject broken input)."""


#: Title of the auto-review chapter that collects page-level OCR concepts.
PAGE_CONCEPTS_CHAPTER_TITLE = "全书概念（自动识别）"

#: Source label used for page-level OCR proposals (review can tell them apart).
PAGE_CONCEPTS_SOURCE_SECTION = "（页面识别）"

DEFAULT_PATH_TITLE = "默认路线"

_SLUG_RE = re.compile(r"[^A-Za-z0-9\u4e00-\u9fff]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name or "").strip("-")[:24] or "concept"


def _merge_refs(
    existing: Iterable[SourceRef], added: Iterable[SourceRef]
) -> list[SourceRef]:
    merged = list(existing)
    seen = {(ref.source_type, ref.document_id, ref.page) for ref in merged}
    for ref in added:
        key = (ref.source_type, ref.document_id, ref.page)
        if key not in seen:
            merged.append(ref)
            seen.add(key)
    return merged


def _document_source_ref(
    document: DocumentStructure, *, page: int | None, section: str | None
) -> SourceRef:
    url = next((ref.url for ref in document.source_refs if ref.url), None)
    return SourceRef(
        source_type="pdf",
        document_id=document.document_id,
        page=page,
        section=section,
        url=url,
    )


def _chapter_source_ref(document: DocumentStructure, section: Section) -> SourceRef:
    page = section.source_page_range[0] if section.source_page_range else None
    return _document_source_ref(document, page=page, section=section.title)


def _sub_section_digest(children: tuple[Section, ...]) -> str:
    """Sub-sections become the chapter's description (design §四)."""
    lines = []
    for child in children:
        page = f"（{child.page_label}）" if child.page_label else ""
        lines.append(f"{child.title}{page}")
    return "\n".join(lines)


def build_draft(
    document: DocumentStructure,
    course_id: str,
    *,
    draft_id: str | None = None,
    title: str | None = None,
    created_by: str = DraftOrigin.PAGELENS.value,
    path_title: str = DEFAULT_PATH_TITLE,
    make_path: bool = True,
) -> CurriculumDraft:
    """Build a ``CurriculumDraft`` from a validated document structure.

    Pure: opens no connection, creates no Concept, activates nothing. The
    caller decides when (and whether) to persist it.
    """
    document.raise_if_invalid()
    course_id = str(course_id or "").strip()
    if not course_id:
        raise DocumentStructureError("course_id must not be empty")

    draft_id = draft_id or f"draft-pagelens-{uuid.uuid4().hex}"
    now = utc_now_iso()
    document_ref = _document_source_ref(document, page=None, section=None)

    # ------------------------------------------------------------------
    # 1. collect every concept hint with its owning chapter, source ref and
    #    section label; the same normalized concept keeps ONE proposal id
    #    even when it appears in several chapters.
    # ------------------------------------------------------------------
    spine = document.chapter_sections

    # (chapter_index 1-based, hint, source_ref, source_section)
    hint_items: list[tuple[int, object, SourceRef, str]] = []
    proposal_registry: dict[str, ConceptProposal] = {}  # normalized -> canonical
    counters: dict[str, int] = {}

    def proposal_for(hint, source_ref: SourceRef, source_section: str) -> None:
        """Phase A: build ONE canonical proposal per normalized concept, so a
        concept appearing in several chapters has a single identity and every
        placement references the SAME immutable instance."""
        key = normalize_name(hint.name)
        proposal = proposal_registry.get(key)
        if proposal is None:
            counter = counters.get(key, 0) + 1
            counters[key] = counter
            proposal = ConceptProposal(
                proposal_id=f"{draft_id}:prop:{_slug(hint.name)}-{counter}",
                name=hint.name,
                aliases=tuple(hint.aliases),
                source_section=source_section,
                confidence=hint.confidence,
            )
        else:
            # A real section name wins over the page-level placeholder when the
            # same concept is later found in the outline.
            if source_section != PAGE_CONCEPTS_SOURCE_SECTION:
                proposal = replace(proposal, source_section=source_section)
        proposal = replace(
            proposal,
            source_refs=tuple(_merge_refs(proposal.source_refs, (source_ref,))),
        )
        proposal_registry[key] = proposal

    for index, section in enumerate(spine, start=1):
        chapter_ref = _chapter_source_ref(document, section)
        for hint in section.concepts:
            hint_items.append((index, hint, chapter_ref, section.title))
        for child in document.children_of(section.id):
            child_ref = _document_source_ref(
                document,
                page=child.source_page_range[0] if child.source_page_range else None,
                section=child.title,
            )
            for hint in child.concepts:
                hint_items.append((index, hint, child_ref, child.title))

    for hint in document.page_concepts:
        hint_items.append((len(spine) + 1, hint, document_ref, PAGE_CONCEPTS_SOURCE_SECTION))

    # Phase A: canonicalize every proposal first.
    for chapter_index, hint, source_ref, source_section in hint_items:
        proposal_for(hint, source_ref, source_section)

    # Phase B: placements reference the canonical instances (shared identity).
    positions: dict[int, int] = {}
    placements_by_chapter: dict[int, list[ChapterConceptDraft]] = {
        index: [] for index in range(1, len(spine) + 2)
    }
    for chapter_index, hint, _source_ref, _source_section in hint_items:
        canonical = proposal_registry[normalize_name(hint.name)]
        position = positions.get(chapter_index, 0) + 1
        positions[chapter_index] = position
        placements_by_chapter[chapter_index].append(
            ChapterConceptDraft(proposal=canonical, position=position)
        )

    # ------------------------------------------------------------------
    # 2. chapters (level-1 sections) + sub-section digests + provenance
    # ------------------------------------------------------------------
    chapters: list[ChapterDraft] = []
    path_steps: list[PathStepDraft] = []
    for index, section in enumerate(spine, start=1):
        chapter_ref = _chapter_source_ref(document, section)
        chapters.append(
            ChapterDraft(
                id=f"{draft_id}:chapter:{index}",
                title=section.title,
                position=index,
                description=_sub_section_digest(document.children_of(section.id)),
                source_refs=tuple(_merge_refs((chapter_ref,), (document_ref,))),
                concepts=tuple(placements_by_chapter[index]),
            )
        )
        path_steps.append(
            PathStepDraft(
                id=f"{draft_id}:step-{index}",
                position=index,
                target_type="chapter",
                target_id=f"{draft_id}:chapter:{index}",
            )
        )

    if document.page_concepts:
        extra_index = len(chapters) + 1
        chapters.append(
            ChapterDraft(
                id=f"{draft_id}:chapter:extra",
                title=PAGE_CONCEPTS_CHAPTER_TITLE,
                position=extra_index,
                description="由页面识别自动收集的概念集合，请在审阅时整理到对应章节。",
                source_refs=(document_ref,),
                concepts=tuple(placements_by_chapter.get(extra_index, ())),
            )
        )
        path_steps.append(
            PathStepDraft(
                id=f"{draft_id}:step-extra",
                position=extra_index,
                target_type="chapter",
                target_id=f"{draft_id}:chapter:extra",
            )
        )

    paths: tuple[LearningPathDraft, ...] = ()
    if make_path and path_steps:
        paths = (
            LearningPathDraft(
                id=f"{draft_id}:path",
                title=path_title,
                description="教材目录顺序的默认学习路线（可编辑）。",
                steps=tuple(path_steps),
            ),
        )

    return CurriculumDraft(
        id=draft_id,
        course_id=course_id,
        title=(title or document.title).strip(),
        description=(
            f"由文档「{document.title}」自动生成的大纲草稿，需用户审阅后确认。"
            + (f" 作者：{document.author}" if document.author else "")
        ),
        based_on_curriculum_id=None,
        chapters=tuple(chapters),
        paths=paths,
        prerequisites=(),
        sources=(
            CurriculumSource(
                kind=SourceKind.PDF.value,
                title=document.title,
                locator=document.document_id,
                fingerprint=document.fingerprint,
                captured_at=now,
            ),
        ),
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )


__all__ = [
    "build_draft",
    "DraftBuildError",
    "PAGE_CONCEPTS_CHAPTER_TITLE",
    "PAGE_CONCEPTS_SOURCE_SECTION",
]