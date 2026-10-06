"""Learning Resource layer tests (Phase 7B).

Covers the frozen contract:

    LearningResource model / schema migration / resource CRUD /
    concept-resource binding / chapter-resource binding / deletion never
    touches mastery / textbook confirm creates resources / TeachingContext can
    read them / no provider / no LLM / safe degradation without resources /
    ordinary chat triggers no resource writes

Pure in-memory + tmp_path SQLite (a real PDF is built locally with PyMuPDF for
the confirm-flow test — no network, no provider).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.learning.controller import LearningModeController
from core.learning.curriculum import ConfirmationRequiredError
from core.learning.curriculum.adapters import PageLensCurriculumAdapter
from core.learning.curriculum.adapter.review import CurriculumDraftReviewService
from core.learning.curriculum.store import CurriculumStore
from core.learning.interactions import InteractionStore
from core.learning.resources import (
    LearningResource,
    ResourceOrigin,
    ResourceStore,
    ResourceStoreError,
    ResourceType,
    record_textbook_resources,
)
from core.learning.store import LearningStore


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_DIR = ROOT / "core" / "learning" / "resources"


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _make_pdf(path: Path, toc: list[list]) -> Path:
    """Build a real PDF with bookmarks (PyMuPDF — local and deterministic)."""
    import fitz  # PyMuPDF — the same library the app already uses

    doc = fitz.open()
    pages = max((entry[2] for entry in toc), default=1)
    for _ in range(pages):
        doc.new_page()
    doc.set_toc(toc)
    doc.save(str(path))
    doc.close()
    return path


def _import_textbook(tmp_path: Path, *, concepts=("传递函数", "方框图")):
    """Run the Phase 7A import + confirm flow; return the loop's outputs."""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    pdf = _make_pdf(tmp_path / "自动控制原理.pdf", [
        [1, "第一章 绪论", 1],
        [1, "第二章 控制系统数学模型", 2],
    ])
    from core.pdf_processor import build_pdf_lazy_index

    index = build_pdf_lazy_index(pdf.read_bytes(), display_name=pdf.name)
    course = store.create_course("自动控制原理")
    draft = PageLensCurriculumAdapter().build_draft(
        index, course_id=course.id, course_name=course.name,
        concepts=list(concepts), draft_id="d-tb",
    )
    service = CurriculumDraftReviewService(curricula)
    service.save(draft)
    curriculum = service.confirm("d-tb", confirmed_by="user")
    view = curricula.get_active_curriculum(course.id)
    return store, curricula, course, view, curriculum


# ---------------------------------------------------------------------------
# 1. model
# ---------------------------------------------------------------------------


def test_learning_resource_model_is_frozen_and_validated() -> None:
    resource = LearningResource(
        id="r1", course_id="c1", resource_type="pdf", title="自动控制原理.pdf",
        source="textbook", locator="page 2-4", created_at="2026-01-01T00:00:00+00:00",
    )
    assert resource.type is ResourceType.PDF
    assert resource.label() == "📄 自动控制原理.pdf（page 2-4）"
    with pytest.raises(Exception):
        resource.title = "x"  # type: ignore[misc]
    with pytest.raises(ValueError):
        LearningResource(
            id="r2", course_id="c1", resource_type="dvd", title="x",
            created_at="t",
        )
    with pytest.raises(ValueError):
        LearningResource(
            id="", course_id="c1", resource_type="pdf", title="x", created_at="t"
        )


def test_resource_type_covers_the_six_frozen_kinds() -> None:
    for name in ("TEXTBOOK", "PDF", "VIDEO", "PAPER", "NOTE", "LINK"):
        assert ResourceType[name].value == name.lower()


# ---------------------------------------------------------------------------
# 2. schema migration
# ---------------------------------------------------------------------------


def test_schema_v4_creates_the_resource_table(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    assert store.get_schema_version() == 4
    with store.connect() as connection:
        columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(learning_resources)"
            ).fetchall()
        ]
    assert columns == [
        "id", "course_id", "concept_id", "chapter_id", "resource_type",
        "title", "source", "locator", "metadata", "created_at",
    ]


def test_migration_is_idempotent(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    store.initialize()
    store.initialize()
    assert store.get_schema_version() == 4
    assert ResourceStore(store).count() == 0


# ---------------------------------------------------------------------------
# 3. CRUD
# ---------------------------------------------------------------------------


def test_resource_crud(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    resources = ResourceStore(store)

    created = resources.add_resource(
        course.id, ResourceType.PDF, "自动控制原理.pdf",
        source="textbook", locator="page 2-4",
        metadata={"document_id": "自动控制原理.pdf"},
        origin=ResourceOrigin.TEXTBOOK_IMPORT,
    )
    assert created.id and created.resource_type == "pdf"
    assert created.metadata["origin"] == "textbook_import"
    assert created.metadata["document_id"] == "自动控制原理.pdf"
    assert resources.get_resource(created.id) is not None
    assert resources.count(course.id) == 1
    assert resources.list_for_course(course.id)[0].title == "自动控制原理.pdf"

    assert resources.delete_resource(created.id) is True
    assert resources.count() == 0
    # deleting an unknown id is a no-op (returns False), never an error
    assert resources.delete_resource("missing") is False


def test_add_resource_requires_an_existing_course(tmp_path) -> None:
    """FK discipline: a resource cannot precede its course."""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    resources = ResourceStore(store)
    with pytest.raises(ResourceStoreError):
        resources.add_resource("missing-course", ResourceType.NOTE, "笔记")


def test_course_deletion_cascades_resources_only(tmp_path) -> None:
    """资源属于课程：删除课程时资源一起消失。"""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    concept = store.add_concept(course.id, "传递函数")
    resources = ResourceStore(store)
    resources.add_resource(course.id, ResourceType.PDF, "教材",
                          concept_id=concept.id)
    assert resources.count(course.id) == 1

    store.delete_course(course.id)
    assert resources.count(course.id) == 0
    assert resources.count() == 0


# ---------------------------------------------------------------------------
# 4-5. concept / chapter bindings (against a REAL confirmed curriculum)
# ---------------------------------------------------------------------------


def test_concept_and_chapter_bindings(tmp_path) -> None:
    store, curricula, course, view, _curriculum = _import_textbook(tmp_path)
    concept_a = store.find_concept(course.id, "传递函数")
    concept_b = store.find_concept(course.id, "方框图")
    chapter = view.chapters_in_order[0]
    resources = ResourceStore(store)

    chapter_resource = resources.add_resource(
        course.id, ResourceType.PDF, "教材", locator="page 1",
        chapter_id=chapter.id,
    )
    concept_resource = resources.add_resource(
        course.id, ResourceType.VIDEO, "B站讲解", locator="BV1xxx",
        concept_id=concept_a.id,
    )
    resources.add_resource(course.id, ResourceType.NOTE, "个人笔记",
                           concept_id=concept_b.id)

    assert [r.id for r in resources.get_resources_for_chapter(chapter.id)] == [
        chapter_resource.id
    ]
    assert [r.id for r in resources.get_resources_for_concept(concept_a.id)] == [
        concept_resource.id
    ]
    assert resources.get_resources_for_concept(concept_b.id)[0].resource_type == "note"
    assert resources.get_resources_for_concept(None) == []
    assert resources.get_resources_for_chapter(None) == []
    assert resources.get_resources_for_chapter("missing") == []


# ---------------------------------------------------------------------------
# 6. deleting a resource never touches mastery
# ---------------------------------------------------------------------------


def test_deleting_a_resource_never_touches_mastery_or_learning_facts(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    concept = store.add_concept(course.id, "传递函数")
    store.apply_mastery_update(concept.id, 3, "medium", "existing", source="quiz")
    store.record_assessment(course.id, concept.id, score=0.8)
    store.create_review_item(concept.id, due_at="2999-01-01T00:00:00+00:00")
    resources = ResourceStore(store)
    resource = resources.add_resource(course.id, ResourceType.PDF, "教材",
                                      concept_id=concept.id)
    before = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
    )

    resources.delete_resource(resource.id)

    after = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
    )
    assert before == after
    assert store.get_concept(concept.id).mastery_level == 3


# ---------------------------------------------------------------------------
# 7. textbook confirm creates resources
# ---------------------------------------------------------------------------


def test_textbook_confirm_creates_chapter_resources(tmp_path) -> None:
    store, curricula, course, view, _curriculum = _import_textbook(tmp_path)
    resources = ResourceStore(store)
    created = record_textbook_resources(resources, course.id, view)

    # one PDF resource per published chapter, with page locators
    assert [r.resource_type for r in created] == ["pdf", "pdf"]
    assert [r.locator for r in created] == ["page 1", "page 2"]
    assert [r.title for r in created] == ["自动控制原理", "自动控制原理"]
    assert all(r.metadata["origin"] == "textbook_import" for r in created)
    # bound to the PUBLISHED chapter ids
    published = {c.id for c in view.chapters_in_order}
    assert {r.chapter_id for r in created} <= published


def test_textbook_confirm_is_user_gated(tmp_path) -> None:
    """A second confirm of the same draft is rejected; resources unchanged."""
    store, curricula, course, view, _curriculum = _import_textbook(tmp_path)
    resources = ResourceStore(store)
    created = record_textbook_resources(resources, course.id, view)

    draft = curricula.get_draft("d-tb")
    with pytest.raises(ConfirmationRequiredError):
        draft.confirm(confirmed_by="user")  # already confirmed -> not a draft
    assert resources.count(course.id) == len(created)  # unchanged


# ---------------------------------------------------------------------------
# 8. TeachingContext can read the resources
# ---------------------------------------------------------------------------


def test_teaching_context_injects_concept_resources(tmp_path) -> None:
    store, curricula, course, view, _curriculum = _import_textbook(tmp_path)
    concept = store.find_concept(course.id, "传递函数")
    resources = ResourceStore(store)
    resources.add_resource(course.id, ResourceType.PDF, "自动控制原理.pdf",
                           locator="page 35-48", concept_id=concept.id,
                           source="textbook")
    # establish the focus (the concept was studied this session)
    session = store.start_session(course.id)
    store.touch_concept(session.id, concept.id, "study")

    from core.learning.context import LearningContextBuilder
    from core.learning.teaching import TeachingPolicy

    context = LearningContextBuilder(store, curricula).build(course.id)
    bound = resources.get_resources_for_concept(concept.id)
    teaching = TeachingPolicy(store).build(context, resources=bound)

    assert teaching.resources == tuple(bound)
    assert "参考材料：📄 自动控制原理.pdf（page 35-48）" in teaching.lines()
    assert "参考材料" in teaching.context_block()
    # the strategy is untouched by the resources
    assert teaching.learning_stage == "beginner"


def test_teaching_policy_without_resources_stays_the_same(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    store.add_concept(course.id, "传递函数")
    from core.learning.context import LearningContext
    from core.learning.teaching import TeachingPolicy

    context = LearningContext(
        course_id=course.id, course_name="自动控制原理", current_focus="传递函数"
    )
    without = TeachingPolicy(store).build(context)
    with_empty = TeachingPolicy(store).build(context, resources=())
    assert without.resources == () and with_empty.resources == ()
    assert (without.learning_stage, without.recommended_strategy) == (
        with_empty.learning_stage, with_empty.recommended_strategy
    )


def test_resource_labels_are_verbatim_for_the_ui() -> None:
    from core.learning.ui import build_status_card

    resource = LearningResource(
        id="r", course_id="c", resource_type="pdf", title="自动控制原理.pdf",
        source="textbook", locator="page 35-48", created_at="t",
    )
    card = build_status_card(None, resources=[resource])
    assert card.resource_labels() == ("📄 自动控制原理.pdf（page 35-48）",)
    assert card.visible is False  # no learning context -> the card stays hidden


# ---------------------------------------------------------------------------
# 9-10. no provider / no LLM
# ---------------------------------------------------------------------------


def test_resource_layer_never_calls_a_provider(tmp_path, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("the resource layer must never call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    resources = ResourceStore(store)
    created = resources.add_resource(course.id, ResourceType.LINK, "参考链接",
                                     locator="https://example.com")
    assert resources.list_for_course(course.id)
    resources.delete_resource(created.id)


def test_resource_package_has_no_forbidden_imports() -> None:
    forbidden = (
        "core.ai_router", "core.providers", "PySide6", "PyQt5", "PyQt6", "ui",
        "openai", "anthropic", "requests", "httpx", "core.video_study",
        "core.learning.rule_engine", "core.learning.assessment",
        "core.learning.quiz_adapter", "core.learning.review_scheduler",
        "core.learning.decision", "core.learning.action",
        "core.learning.teaching",
    )
    offenders: dict[str, set[str]] = {}
    for path in sorted(RESOURCE_DIR.glob("*.py")):
        imports: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
        bad = {
            module for module in imports
            if any(module == f or module.startswith(f + ".") for f in forbidden)
        }
        if bad:
            offenders[path.name] = bad
    assert not offenders, f"resources imports forbidden modules: {offenders}"


# ---------------------------------------------------------------------------
# 11. safe degradation without resources
# ---------------------------------------------------------------------------


def test_empty_resources_degrade_safely(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    concept = store.add_concept(course.id, "传递函数")
    resources = ResourceStore(store)

    assert resources.get_resources_for_concept(concept.id) == []
    assert resources.get_resources_for_chapter("missing") == []
    assert resources.list_for_course(course.id) == []
    assert resources.get_resource("missing") is None

    from core.learning.context import LearningContext
    from core.learning.teaching import TeachingPolicy

    context = LearningContext(
        course_id=course.id, course_name="自动控制原理", current_focus="传递函数"
    )
    teaching = TeachingPolicy(store).build(
        context, resources=resources.get_resources_for_concept(concept.id)
    )
    assert teaching.resource_lines() == ()   # no invented references


def test_store_failures_surface_as_typed_errors(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    resources = ResourceStore(store)
    with pytest.raises(ResourceStoreError):
        resources.add_resource("ghost-course", ResourceType.NOTE, "x")


# ---------------------------------------------------------------------------
# 12. ordinary chat triggers no resource writes
# ---------------------------------------------------------------------------


def test_ordinary_chat_and_learning_turns_write_no_resources(tmp_path) -> None:
    store, curricula, course, view, _curriculum = _import_textbook(tmp_path)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.state.enabled = True
    controller.state.active_course_id = course.id
    controller.state.active_course_name = course.name
    controller.state.active_session_id = store.start_session(course.id).id
    resources = ResourceStore(store)

    for text in ("解释一下传递函数", "传递函数这里没懂", "今天天气不错", "考考我", "下一节"):
        controller.handle_text(text)

    # chatting (ordinary messages AND learning turns) adds no resources —
    # resources only come from the explicit textbook-confirm flow
    assert resources.count(course.id) == 0
