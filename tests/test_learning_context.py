"""LearningContext tests (Phase 2-LC).

Covers the frozen contract:

    active curriculum read / draft never read / current chapter resolution /
    next_in_order order / safe degradation without a curriculum /
    runner context injection / no LearningStore writes / no provider calls

plus the read-only purity of the layer (no provider / LLM / UI / PageLens /
Quiz imports). Pure in-memory + tmp_path SQLite; no Qt, no provider.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.learning.context import (
    ContextSource,
    LearningContext,
    LearningContextBuilder,
)
from core.learning.curriculum import (
    ChapterConceptDraft,
    ChapterDraft,
    ChapterConceptRole,
    ConceptProposal,
    CurriculumDraft,
    CurriculumSource,
    LearningPathDraft,
    PathStepDraft,
)
from core.learning.curriculum.store import CurriculumStore
from core.learning.store import LearningStore


CONTEXT_DIR = Path(__file__).resolve().parents[1] / "core" / "learning" / "context"


def proposal(pid: str, name: str) -> ConceptProposal:
    return ConceptProposal(proposal_id=pid, name=name)


def make_curriculum_store(store: LearningStore) -> CurriculumStore:
    return CurriculumStore(store)


def add_course(store: LearningStore, course_id: str, name: str) -> None:
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO courses (id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'active', ?, ?)",
            (course_id, name, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def sample_draft(course_id: str = "c1", *, draft_id: str = "draft-1") -> CurriculumDraft:
    """Two chapters: 绪论(自动控制概述) and 数学模型(传递函数, 方框图)."""
    return CurriculumDraft(
        id=draft_id,
        course_id=course_id,
        title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(
                id="ch1", title="第一章 绪论", position=1,
                concepts=(ChapterConceptDraft(proposal=proposal("p1", "自动控制概述"), position=1),),
            ),
            ChapterDraft(
                id="ch2", title="第二章 数学模型", position=2,
                concepts=(
                    ChapterConceptDraft(proposal=proposal("p2", "传递函数"), position=1),
                    ChapterConceptDraft(proposal=proposal("p3", "方框图"), position=2),
                ),
            ),
        ),
        paths=(
            LearningPathDraft(
                id="path1", title="默认路线",
                steps=(
                    PathStepDraft("s1", 1, "chapter", "ch1"),
                    PathStepDraft("s2", 2, "chapter", "ch2"),
                ),
            ),
        ),
    )


@pytest.fixture()
def env(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    add_course(store, "c1", "自动控制原理")
    add_course(store, "c2", "物理光学")  # deliberately no curriculum
    curricula = make_curriculum_store(store)
    return store, curricula, LearningContextBuilder(store, curricula)


# ---------------------------------------------------------------------------
# 1. active curriculum is read
# ---------------------------------------------------------------------------


def test_active_curriculum_is_read(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")

    context = builder.build("c1")
    assert context is not None
    assert context.course_id == "c1"
    assert context.course_name == "自动控制原理"
    assert context.curriculum_id is not None
    assert context.curriculum_title == "自动控制原理"
    assert context.curriculum_version == 1
    assert context.source == ContextSource.ACTIVE_CURRICULUM.value
    assert context.has_curriculum is True


# ---------------------------------------------------------------------------
# 2. a draft is never read
# ---------------------------------------------------------------------------


def test_draft_is_never_read_as_structure(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())  # saved, NOT confirmed
    context = builder.build("c1")
    assert context is not None
    assert context.curriculum_id is None
    assert context.has_curriculum is False
    assert context.source == ContextSource.NO_CURRICULUM.value
    assert context.next_in_order is None


def test_superseded_version_is_not_read(env) -> None:
    store, curricula, builder = env
    first = sample_draft(draft_id="draft-1")
    curricula.create_draft(first)
    v1 = curricula.confirm_draft("draft-1", confirmed_by="user")
    second = sample_draft(draft_id="draft-2")
    curricula.create_draft(second)
    v2 = curricula.confirm_draft("draft-2", confirmed_by="user")

    context = builder.build("c1")
    assert context.curriculum_id == v2.id
    assert context.curriculum_id != v1.id


# ---------------------------------------------------------------------------
# 3. current chapter resolution
# ---------------------------------------------------------------------------


def test_current_chapter_comes_from_recent_study_session(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")

    # nothing studied yet -> no chapter (never guessed)
    fresh = builder.build("c1")
    assert fresh.current_chapter is None
    assert fresh.current_focus is None

    session = store.start_session("c1")
    concepts = {c.canonical_name: c for c in store.list_concepts("c1")}
    store.touch_concept(session.id, concepts["传递函数"].id, "study")

    context = builder.build("c1")
    assert context.current_focus == "传递函数"
    assert context.current_chapter == "第二章 数学模型"


def test_current_chapter_prefers_primary_placement(env) -> None:
    store, curricula, builder = env
    # 传递函数 is taught in ch1 as SUPPORTING and ch2 as PRIMARY.
    draft = CurriculumDraft(
        id="draft-1", course_id="c1", title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动"),),
        chapters=(
            ChapterDraft(id="ch1", title="第一章 绪论", position=1,
                         concepts=(ChapterConceptDraft(proposal=proposal("p1", "传递函数"), position=1,
                                                       role=ChapterConceptRole.SUPPORTING.value),)),
            ChapterDraft(id="ch2", title="第二章 数学模型", position=2,
                         concepts=(ChapterConceptDraft(proposal=proposal("p2", "传递函数"), position=1),)),
        ),
        paths=(LearningPathDraft(id="p1", title="默认", steps=(
            PathStepDraft("s1", 1, "chapter", "ch1"),
            PathStepDraft("s2", 2, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("draft-1", confirmed_by="user")
    # Both chapters link to the SAME concept id (course-scoped identity).
    session = store.start_session("c1")
    concept = store.find_concept("c1", "传递函数")
    store.touch_concept(session.id, concept.id, "study")

    context = builder.build("c1")
    assert context.current_chapter == "第二章 数学模型"


def test_most_recently_studied_concept_falls_back_without_session(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")
    # An imported/backfilled concept with a study timestamp but no session.
    concept = store.find_concept("c1", "方框图")
    store.touch_concept(store.start_session("c1").id, concept.id, "study")
    context = builder.build("c1")
    assert context.current_focus == "方框图"
    assert context.current_chapter == "第二章 数学模型"


# ---------------------------------------------------------------------------
# 4. next_in_order follows curriculum order
# ---------------------------------------------------------------------------


def test_next_in_order_follows_curriculum_order(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")

    # Before any study: the route's first node (structural start).
    assert builder.build("c1").next_in_order == "自动控制概述"

    session = store.start_session("c1")
    concepts = {c.canonical_name: c for c in store.list_concepts("c1")}
    store.touch_concept(session.id, concepts["传递函数"].id, "study")
    assert builder.build("c1").next_in_order == "方框图"

    store.touch_concept(session.id, concepts["方框图"].id, "study")
    assert builder.build("c1").next_in_order is None  # end of the route


def test_next_in_order_is_not_a_recommendation(env) -> None:
    """It is pure position: no mastery/score input can change it."""
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")
    session = store.start_session("c1")
    concepts = {c.canonical_name: c for c in store.list_concepts("c1")}
    store.touch_concept(session.id, concepts["传递函数"].id, "study")
    before = builder.build("c1").next_in_order
    # A mastery change must not move the structural successor.
    store.apply_mastery_update(concepts["方框图"].id, 5, "high", "test", source="quiz")
    assert builder.build("c1").next_in_order == before


# ---------------------------------------------------------------------------
# 5. safe degradation without a curriculum
# ---------------------------------------------------------------------------


def test_course_without_curriculum_degrades_safely(env) -> None:
    store, curricula, builder = env
    context = builder.build("c2")
    assert context is not None
    assert context.course_id == "c2"
    assert context.has_curriculum is False
    assert context.current_chapter is None
    assert context.current_focus is None
    assert context.next_in_order is None
    assert context.context_block() is None
    assert context.structure_lines() == ()


def test_unknown_or_empty_course_returns_none(env) -> None:
    store, curricula, builder = env
    assert builder.build("missing") is None
    assert builder.build("") is None
    assert builder.build(None) is None


def test_context_block_mentions_structure_only_when_present(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")
    session = store.start_session("c1")
    concepts = {c.canonical_name: c for c in store.list_concepts("c1")}
    store.touch_concept(session.id, concepts["传递函数"].id, "study")

    block = builder.build("c1").context_block()
    assert "当前章节：第二章 数学模型" in block
    assert "当前重点：传递函数" in block
    assert "下一结构节点：方框图" in block
    # explicitly framed as structure, not personalisation
    assert "不是个性化推荐" in block or "而非个性化推荐" in block


# ---------------------------------------------------------------------------
# 6. runner injects the context
# ---------------------------------------------------------------------------


def test_runner_injects_learning_structure_into_turn_context(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")
    session = store.start_session("c1")
    concepts = {c.canonical_name: c for c in store.list_concepts("c1")}
    store.touch_concept(session.id, concepts["传递函数"].id, "study")

    from core.learning.controller import LearningModeController
    from ui.character_conversation_runner import CharacterConversationRunner

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2, turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    controller = LearningModeController(
        store=store, settings=None, curriculum_store=curricula
    )
    controller.state.enabled = True
    controller.state.active_course_id = "c1"
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = session.id

    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    )
    runner.perform("什么是传递函数？")

    block = captured["turn_context"]
    assert block is not None
    assert "当前学习项目：自动控制原理" in block
    assert "第二章 数学模型" in block
    assert "传递函数" in block
    assert "方框图" in block


def test_runner_context_absent_outside_mode(env) -> None:
    store, curricula, builder = env
    from core.learning.controller import LearningModeController
    from ui.character_conversation_runner import CharacterConversationRunner

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2, turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    controller = LearningModeController(store=store, settings=None, curriculum_store=curricula)
    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    )
    runner.perform("随便聊聊")
    assert captured["turn_context"] is None


# ---------------------------------------------------------------------------
# 7. read-only: nothing is written
# ---------------------------------------------------------------------------


def _fact_snapshot(store: LearningStore, course_id: str) -> tuple:
    return (
        len(store.list_courses()),
        len(store.list_concepts(course_id)),
        sorted(c.mastery_level for c in store.list_concepts(course_id)),
        len(store.list_assessments()),
        len(store.list_review_items()),
        len(store.list_sessions(course_id)),
        len(store.list_mastery_audit(course_id)),
    )


def test_building_context_never_writes(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")
    session = store.start_session("c1")
    concepts = {c.canonical_name: c for c in store.list_concepts("c1")}
    store.touch_concept(session.id, concepts["传递函数"].id, "study")

    before = _fact_snapshot(store, "c1")
    for _ in range(3):
        context = builder.build("c1")
        context.context_block()
        context.structure_lines()
        context.resume_line()
    assert _fact_snapshot(store, "c1") == before
    # no curriculum was activated beyond the one we published
    assert len(curricula.list_curriculums("c1")) == 1


def test_context_does_not_create_concepts(env) -> None:
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")
    before = len(store.list_concepts("c1"))
    builder.build("c1").context_block()
    assert len(store.list_concepts("c1")) == before


# ---------------------------------------------------------------------------
# 8. no provider / LLM calls
# ---------------------------------------------------------------------------


def test_building_context_never_calls_a_provider(env, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("LearningContext must never call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula, builder = env
    curricula.create_draft(sample_draft())
    curricula.confirm_draft("draft-1", confirmed_by="user")
    context = builder.build("c1")
    assert context.context_block() is not None


def test_context_module_has_no_forbidden_imports() -> None:
    forbidden = {
        "PySide6", "PyQt5", "PyQt6", "ui", "core.ai_router", "core.providers",
        "core.pagelens_bridge", "core.screen_vision", "core.memory", "openai",
        "anthropic", "requests", "httpx", "core.video_study", "core.learning.quiz_adapter",
        "core.learning.assessment", "core.learning.rule_engine",
    }
    offenders: dict[str, set[str]] = {}
    for path in sorted(CONTEXT_DIR.glob("*.py")):
        imports: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module)
        bad = {
            module for module in imports
            if module.split(".")[0] in forbidden
            or any(module == f or module.startswith(f + ".") for f in forbidden)
        }
        if bad:
            offenders[path.name] = bad
    assert not offenders, f"context imports forbidden modules: {offenders}"


def test_learning_context_is_immutable() -> None:
    context = LearningContext(course_id="c1", course_name="自动控制原理")
    with pytest.raises(Exception):
        context.current_chapter = "x"  # type: ignore[misc]
    assert context.source == ContextSource.NO_CURRICULUM.value
