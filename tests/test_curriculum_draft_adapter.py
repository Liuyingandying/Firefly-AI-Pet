"""Curriculum Draft Adapter tests (Phase 2-CF3).

Covers the frozen contract:

    PDF 结构输入 / 章节生成 / position 保持 / source provenance /
    concept proposal 生成 / proposal 不写 Concept / confirm 后才进入 Curriculum /
    空目录处理 / 损坏结构拒绝 / course isolation

plus the adapter's import purity. Pure in-memory (no Qt / PDF viewer / provider);
the two end-to-end tests use a tmp_path SQLite LearningStore only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.learning.curriculum.adapter import (
    CurriculumDraftReviewService,
    DocumentStructure,
    DocumentStructureError,
    PAGE_CONCEPTS_CHAPTER_TITLE,
    Section,
    SectionConceptHint,
    build_draft,
)
from core.learning.curriculum.store import CurriculumStore
from core.learning.store import LearningStore


ADAPTER_DIR = (
    Path(__file__).resolve().parents[1]
    / "core" / "learning" / "curriculum" / "adapter"
)


def sample_structure(
    *,
    sections=None,
    page_concepts=(),
    document_id: str = "auto-ctrl-v7",
    title: str = "自动控制原理",
    author: str = "胡寿松",
) -> DocumentStructure:
    if sections is None:
        sections = (
            Section(id="ch1", title="绪论", level=1, position=1, source_page_range=(1, 10)),
            Section(id="ch2", title="数学模型", level=1, position=2, source_page_range=(11, 60)),
            Section(
                id="ch3", title="时域分析", level=1, position=3, source_page_range=(61, 90),
                concepts=[{"term": "频率响应", "confidence": 0.7}],
            ),
        )
    return DocumentStructure(
        document_id=document_id,
        title=title,
        author=author,
        sections=tuple(sections),
        page_concepts=tuple(page_concepts),
    )


@pytest.fixture()
def env(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    with store.connect() as connection:
        for course_id, name in (("c1", "自动控制原理"), ("c2", "物理光学")):
            connection.execute(
                "INSERT INTO courses (id, name, status, created_at, updated_at)"
                " VALUES (?, ?, 'active', ?, ?)",
                (course_id, name, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
    return store, CurriculumStore(store)


# ---------------------------------------------------------------------------
# 1. PDF structure input
# ---------------------------------------------------------------------------


def test_document_structure_accepts_pdf_outline() -> None:
    document = sample_structure(
        sections=(
            Section(id="c1", title="第一章 绪论", level=1, position=1, source_page_range=(1, 10)),
            Section(id="c2", title="第二章 数学模型", level=1, position=2, source_page_range=(11, 60)),
            Section(id="s21", title="2.1 微分方程", level=2, position=1, parent_id="c2", source_page_range=(11, 20)),
            Section(id="s22", title="2.2 传递函数", level=2, position=2, parent_id="c2", source_page_range=(21, 40)),
        )
    )
    assert document.document_id == "auto-ctrl-v7"
    assert len(document.sections) == 4
    assert [s.title for s in document.chapter_sections] == [
        "第一章 绪论",
        "第二章 数学模型",
    ]
    assert [s.title for s in document.children_of("c2")] == ["2.1 微分方程", "2.2 传递函数"]


def test_from_pagelens_reads_bridge_payloads() -> None:
    # pdf_opened payload shape (title + url) as the bridge emits it today.
    structure = DocumentStructure.from_pagelens(
        {"url": "file:///docs/auto-ctrl-v7.pdf", "title": "自动控制原理"}
    )
    assert structure.title == "自动控制原理"
    assert structure.document_id == "file:///docs/auto-ctrl-v7.pdf"
    assert structure.source_refs[0].source_type == "pdf"
    assert structure.source_refs[0].url == "file:///docs/auto-ctrl-v7.pdf"
    # a page_context payload also works
    structure2 = DocumentStructure.from_pagelens({"title": "物理光学", "url": "u2"})
    assert structure2.title == "物理光学"


def test_from_pagelens_consumes_concepts_and_outline_forward_compatibly() -> None:
    structure = DocumentStructure.from_pagelens(
        {"title": "自动控制原理", "url": "u"},
        concepts=[{"term": "传递函数", "confidence": 0.9}, "状态空间"],
        outline=[
            {"title": "绪论", "level": 1, "position": 1, "pages": [1, 10]},
            {"title": "数学模型", "level": 1, "position": 2, "pages": "11-60"},
        ],
    )
    assert [s.title for s in structure.chapter_sections] == ["绪论", "数学模型"]
    assert structure.chapter_sections[1].source_page_range == (11, 60)
    assert [h.name for h in structure.page_concepts] == ["传递函数", "状态空间"]


def test_page_range_parser_accepts_common_shapes() -> None:
    assert Section(id="a", title="A", level=1, position=1, source_page_range=(5, 8)).source_page_range == (5, 8)
    assert Section(id="b", title="B", level=1, position=2, source_page_range="5-8").source_page_range == (5, 8)
    assert Section(id="c", title="C", level=1, position=3, source_page_range=7).source_page_range == (7, 7)
    assert Section(id="d", title="D", level=1, position=4, source_page_range="7").source_page_range == (7, 7)
    assert Section(id="e", title="E", level=1, position=5).source_page_range is None
    with pytest.raises(DocumentStructureError):
        Section(id="f", title="F", level=1, position=6, source_page_range=(9, 3))


def test_concept_hint_from_payloads() -> None:
    assert SectionConceptHint.from_payload("传递函数") == SectionConceptHint(name="传递函数")
    hint = SectionConceptHint.from_payload({"term": "频率响应", "score": 0.8, "aliases": ["频响"]})
    assert hint.confidence == 0.8
    assert hint.aliases == ("频响",)
    with pytest.raises(DocumentStructureError):
        SectionConceptHint.from_payload({"term": "x", "confidence": 1.5})


# ---------------------------------------------------------------------------
# 2. chapter generation
# ---------------------------------------------------------------------------


def test_level1_sections_become_chapters() -> None:
    draft = build_draft(sample_structure(), "c1", draft_id="draft-1")
    assert [c.title for c in draft.chapters_in_order] == ["绪论", "数学模型", "时域分析"]
    assert [c.position for c in draft.chapters_in_order] == [1, 2, 3]
    assert draft.course_id == "c1"
    assert draft.created_by == "pagelens"
    # not a Curriculum: no version, no active status
    assert not hasattr(draft, "version")
    assert draft.status == "draft"


def test_sub_sections_become_chapter_description() -> None:
    document = DocumentStructure(
        document_id="d", title="t", sections=(
            Section(id="c2", title="数学模型", level=1, position=1, source_page_range=(11, 60)),
            Section(id="s21", title="2.1 微分方程", level=2, position=1, parent_id="c2", source_page_range=(11, 20)),
            Section(id="s22", title="2.2 传递函数", level=2, position=2, parent_id="c2", source_page_range=(21, 40)),
        ),
    )
    draft = build_draft(document, "c1", draft_id="draft-1")
    chapter = draft.chapters_in_order[0]
    assert "2.1 微分方程（p.11-20）" in chapter.description
    assert "2.2 传递函数（p.21-40）" in chapter.description
    # sub-sections are NOT chapters
    assert [c.title for c in draft.chapters_in_order] == ["数学模型"]


# ---------------------------------------------------------------------------
# 3. position preservation
# ---------------------------------------------------------------------------


def test_chapter_positions_follow_section_positions_not_list_order() -> None:
    document = sample_structure(
        sections=(
            Section(id="ch3", title="时域分析", level=1, position=3, source_page_range=(61, 90)),
            Section(id="ch1", title="绪论", level=1, position=1, source_page_range=(1, 10)),
            Section(id="ch2", title="数学模型", level=1, position=2, source_page_range=(11, 60)),
        )
    )
    draft = build_draft(document, "c1", draft_id="draft-1")
    assert [c.title for c in draft.chapters_in_order] == ["绪论", "数学模型", "时域分析"]
    assert [c.position for c in draft.chapters_in_order] == [1, 2, 3]
    # the default path follows the same normalized order
    path = draft.paths[0]
    steps = sorted(path.steps, key=lambda s: (s.position, s.id))
    assert [s.position for s in steps] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 4. source provenance
# ---------------------------------------------------------------------------


def test_every_chapter_has_provenance() -> None:
    document = DocumentStructure(
        document_id="doc-1", title="自动控制原理", source_refs=(), sections=(
            Section(id="c1", title="绪论", level=1, position=1, source_page_range=(1, 10)),
            Section(id="c2", title="数学模型", level=1, position=2),  # no page range
        ),
    )
    draft = build_draft(document, "c1", draft_id="draft-1")
    for chapter in draft.chapters:
        # never a source-less chapter: the section ref falls back to the doc ref
        assert chapter.source_refs, chapter.title
        assert all(ref.document_id == "doc-1" for ref in chapter.source_refs)
    _c1, c2 = draft.chapters_in_order
    assert any(ref.page == 1 for ref in _c1.source_refs)
    assert all(ref.page is None for ref in c2.source_refs if ref.section == "数学模型")
    # the draft itself carries a CurriculumSource
    assert draft.sources[0].kind == "pdf"
    assert draft.sources[0].locator == "doc-1"


def test_proposal_source_section_is_recorded() -> None:
    document = DocumentStructure(
        document_id="doc-1", title="t", sections=(
            Section(
                id="c2", title="数学模型", level=1, position=1, source_page_range=(21, 40),
                concepts=[{"term": "传递函数", "confidence": 0.9}],
            ),
        ),
    )
    draft = build_draft(document, "c1", draft_id="draft-1")
    proposals = list(draft.proposals_by_id().values())
    assert len(proposals) == 1
    assert proposals[0].name == "传递函数"
    assert proposals[0].source_section == "数学模型"
    assert proposals[0].confidence == 0.9
    assert any(ref.page == 21 for ref in proposals[0].source_refs)


# ---------------------------------------------------------------------------
# 5-6. concept proposals stay proposals
# ---------------------------------------------------------------------------


def test_concept_proposals_are_generated_from_sections() -> None:
    document = DocumentStructure(
        document_id="doc-1", title="t", sections=(
            Section(id="c1", title="数学模型", level=1, position=1, source_page_range=(1, 10),
                    concepts=["传递函数"]),
            Section(id="c2", title="时域分析", level=1, position=2, source_page_range=(11, 20),
                    concepts=["传递函数", {"term": "频率响应", "confidence": 0.66}]),
        ),
    )
    draft = build_draft(document, "c1", draft_id="draft-1")
    proposals = draft.proposals_by_id()
    assert set(proposals) == {"draft-1:prop:传递函数-1", "draft-1:prop:频率响应-1"}
    # the SAME concept in two chapters is ONE proposal with two placements
    transfer = [p for p in proposals.values() if p.name == "传递函数"][0]
    placements = [
        link for chapter in draft.chapters for link in chapter.concepts
        if link.proposal is transfer
    ]
    assert len(placements) == 2


def test_build_never_touches_the_store_or_concepts(env) -> None:
    store, _cs = env
    draft = build_draft(sample_structure(page_concepts=[{"term": "状态空间"}]), "c1")
    # proposals only live in the draft
    assert draft.concept_candidates
    assert store.list_concepts("c1") == []
    # nothing was persisted anywhere
    assert store.get_schema_version() == 4
    with store.connect() as connection:
        curriculums = connection.execute("SELECT COUNT(*) FROM curriculums").fetchone()[0]
        drafts = connection.execute("SELECT COUNT(*) FROM curriculum_drafts").fetchone()[0]
    assert (curriculums, drafts) == (0, 0)


def test_page_concepts_go_to_the_auto_review_chapter() -> None:
    document = DocumentStructure(
        document_id="doc-1", title="t", sections=(
            Section(id="c1", title="绪论", level=1, position=1, source_page_range=(1, 2)),
        ),
        page_concepts=[{"term": "状态空间", "confidence": 0.55}],
    )
    draft = build_draft(document, "c1", draft_id="draft-1")
    assert draft.chapters[-1].title == PAGE_CONCEPTS_CHAPTER_TITLE
    assert [p.name for p in draft.chapters[-1].proposals] == ["状态空间"]
    assert draft.chapters[-1].proposals[0].source_section == "（页面识别）"


# ---------------------------------------------------------------------------
# 7. confirm is the only door into a Curriculum
# ---------------------------------------------------------------------------


def test_confirm_after_review_is_the_only_activation_path(env) -> None:
    store, _cs = env
    store.add_concept("c2", "外来概念")  # exists in another course only
    document = DocumentStructure(
        document_id="doc-1", title="t", sections=(
            Section(id="c1", title="绪论", level=1, position=1, source_page_range=(1, 5),
                    concepts=["传递函数"]),
        ),
    )
    draft = build_draft(document, "c1", draft_id="draft-1")
    review = CurriculumDraftReviewService(_cs)

    summary = review.review(draft)
    assert summary.ready is True
    assert "传递函数" in [name for name, _, _ in summary.concept_proposals]

    # before saving/confirming: nothing in the store
    assert _cs.get_active_curriculum("c1") is None
    # saving makes it a DRAFT — still not a curriculum
    review.save(draft)
    assert _cs.get_draft("draft-1") is not None
    assert _cs.get_active_curriculum("c1") is None
    # only the explicit user confirmation activates it
    curriculum = review.confirm("draft-1", confirmed_by="user")
    assert curriculum.is_active
    assert _cs.get_active_curriculum("c1").curriculum.id == curriculum.id


def test_review_confirms_without_auto_activation(env) -> None:
    store, cs = env
    store.add_concept("c1", "自动控制概述", created_at="2026-01-01T00:00:00+00:00")
    draft = build_draft(sample_structure(page_concepts=[]), "c1")
    review = CurriculumDraftReviewService(cs)
    summary = review.review(draft)
    assert summary.ready
    # nothing happened yet
    assert not cs.list_curriculums("c1")
    assert cs.list_drafts("c1") == []


# ---------------------------------------------------------------------------
# 8. empty outline handling
# ---------------------------------------------------------------------------


def test_empty_outline_produces_a_guarded_draft(env) -> None:
    store, cs = env
    document = DocumentStructure(document_id="doc-1", title="空目录")
    draft = build_draft(document, "c1", draft_id="draft-1")
    assert draft.chapters == ()
    review = CurriculumDraftReviewService(cs)
    summary = review.review(draft)
    assert summary.ready is False
    assert summary.chapter_count == 0
    assert any("目录为空" in warning for warning in summary.warnings)
    # cannot be activated: validator rejects an empty draft
    review.save(draft)
    with pytest.raises(Exception):
        review.confirm("draft-1", confirmed_by="user")
    assert cs.get_active_curriculum("c1") is None


def test_empty_outline_with_only_page_concepts_is_still_reviewable(env) -> None:
    store, cs = env
    document = DocumentStructure(
        document_id="doc-1", title="只有概念", sections=(),
        page_concepts=[{"term": "状态空间"}],
    )
    draft = build_draft(document, "c1")
    assert draft.chapters[-1].title == PAGE_CONCEPTS_CHAPTER_TITLE
    summary = CurriculumDraftReviewService(cs).review(draft)
    assert summary.ready is True


# ---------------------------------------------------------------------------
# 9. broken structures are rejected
# ---------------------------------------------------------------------------


def test_duplicate_section_ids_rejected() -> None:
    with pytest.raises(DocumentStructureError) as excinfo:
        build_draft(
            DocumentStructure(document_id="d", title="t", sections=(
                Section(id="c", title="A", level=1, position=1),
                Section(id="c", title="B", level=1, position=2),
            )),
            "c1",
        )
    assert "duplicate section id" in str(excinfo.value)


def test_dangling_parent_rejected() -> None:
    with pytest.raises(DocumentStructureError) as excinfo:
        build_draft(
            DocumentStructure(document_id="d", title="t", sections=(
                Section(id="s", title="2.1", level=2, position=1, parent_id="ghost"),
            )),
            "c1",
        )
    assert "parent" in str(excinfo.value)


def test_parent_level_not_lower_rejected() -> None:
    with pytest.raises(DocumentStructureError) as excinfo:
        build_draft(
            DocumentStructure(document_id="d", title="t", sections=(
                # two level-2 sections that parent each other: neither parent
                # has a LOWER level.
                Section(id="a", title="A", level=2, position=1, parent_id="b"),
                Section(id="b", title="B", level=2, position=2, parent_id="a"),
            )),
            "c1",
        )
    assert "parent level must be lower" in str(excinfo.value)


def test_sibling_position_duplicate_rejected() -> None:
    with pytest.raises(DocumentStructureError) as excinfo:
        build_draft(
            DocumentStructure(document_id="d", title="t", sections=(
                Section(id="a", title="A", level=1, position=1),
                Section(id="b", title="B", level=1, position=1),
            )),
            "c1",
        )
    assert "share position" in str(excinfo.value)


def test_invalid_level_and_title_rejected() -> None:
    with pytest.raises(DocumentStructureError):
        Section(id="a", title="A", level=0, position=1)
    with pytest.raises(DocumentStructureError):
        Section(id="b", title="", level=1, position=1)
    with pytest.raises(DocumentStructureError):
        DocumentStructure(document_id="", title="t")
    with pytest.raises(DocumentStructureError):
        build_draft(DocumentStructure(document_id="d", title="t"), "")


# ---------------------------------------------------------------------------
# 10. course isolation / course binding
# ---------------------------------------------------------------------------


def test_draft_is_bound_to_the_requested_course(env) -> None:
    store, cs = env
    draft = build_draft(sample_structure(), "c2")
    assert draft.course_id == "c2"
    # a phantom course cannot even be saved (FK) or confirmed (validator):
    # the store guards the course from two directions.
    phantom = build_draft(sample_structure(), "no-such-course")
    review = CurriculumDraftReviewService(cs)
    with pytest.raises(Exception):
        review.confirm_reviewed(phantom, confirmed_by="user")
    assert cs.get_active_curriculum("c2") is None
    assert cs.get_curriculum("whatever") is None


def _adapter_imports() -> dict[str, set[str]]:
    """module-level imports per adapter file (AST, deterministic)."""
    result: dict[str, set[str]] = {}
    for path in sorted(ADAPTER_DIR.glob("*.py")):
        imports: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
        result[path.name] = imports
    return result


def test_adapter_never_imports_providers_ui_or_llm() -> None:
    forbidden = {
        "PySide6", "PyQt5", "PyQt6", "ui", "core.ai_router", "core.providers",
        "core.pagelens_bridge", "core.screen_vision", "core.memory", "openai",
        "anthropic", "requests", "httpx", "core.video_study",
    }
    for filename, imports in _adapter_imports().items():
        bad = {module for module in imports if module.split(".")[0] in forbidden}
        assert not bad, f"{filename} imports forbidden modules: {bad}"


def test_pure_builder_files_are_database_free() -> None:
    """documents.py + draft.py never touch the store (or sqlite); review.py is
    the single door that may reach CurriculumStore."""
    imports_by_file = _adapter_imports()
    for name in ("documents.py", "draft.py"):
        imports = imports_by_file[name]
        assert "core.learning.curriculum.store" not in imports, name
        assert "core.learning.store" not in imports, name
        assert "sqlite3" not in imports, name
    assert "core.learning.curriculum.store" in imports_by_file["review.py"]