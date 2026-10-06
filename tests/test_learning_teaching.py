"""Teaching strategy tests (Phase 3 MVP).

Covers the frozen contract:

    mastery mapping / review-due priority / safe degradation without a concept /
    no database writes / no provider calls / runner receives the context /
    ordinary chat unaffected

plus the deterministic-only purity of the layer (no LLM, no Qt, no
rule-engine/assessment/quiz imports). Pure in-memory + tmp_path SQLite.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.learning.context import LearningContext, LearningContextBuilder
from core.learning.curriculum import (
    ChapterConceptDraft,
    ChapterDraft,
    ConceptProposal,
    CurriculumDraft,
    CurriculumSource,
    LearningPathDraft,
    PathStepDraft,
)
from core.learning.curriculum.store import CurriculumStore
from core.learning.store import LearningStore
from core.learning.teaching import (
    NO_CONCEPT_STRATEGY,
    STAGE_BY_MASTERY,
    STRATEGY_BY_STAGE,
    LearningStage,
    TeachingContext,
    TeachingPolicy,
    TeachingSource,
)


TEACHING_DIR = Path(__file__).resolve().parents[1] / "core" / "learning" / "teaching"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _add_course(store: LearningStore, course_id: str, name: str) -> None:
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO courses (id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'active', ?, ?)",
            (course_id, name, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )


def _publish_curriculum(curricula: CurriculumStore, course_id: str) -> None:
    draft = CurriculumDraft(
        id=f"draft-{course_id}",
        course_id=course_id,
        title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(
                id="ch2", title="第二章 数学模型", position=1,
                concepts=(
                    ChapterConceptDraft(
                        proposal=ConceptProposal(proposal_id="p2", name="传递函数"), position=1),
                    ChapterConceptDraft(
                        proposal=ConceptProposal(proposal_id="p3", name="方框图"), position=2),
                ),
            ),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft(draft.id, confirmed_by="user")


@pytest.fixture()
def env(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    _add_course(store, "c1", "自动控制原理")
    curricula = CurriculumStore(store)
    _publish_curriculum(curricula, "c1")
    builder = LearningContextBuilder(store, curricula)
    session = store.start_session("c1")
    concept = store.find_concept("c1", "传递函数")
    store.touch_concept(session.id, concept.id, "study")
    return store, curricula, builder, session, concept


def _facts(store: LearningStore, course_id: str, concept_id: str) -> tuple:
    return (
        len(store.list_concepts(course_id)),
        store.get_concept(concept_id).mastery_level,
        len(store.list_mastery_audit(concept_id)),
        len(store.list_assessments(concept_id)),
        len(store.list_review_items(concept_id)),
        len(store.list_sessions(course_id)),
    )


# ---------------------------------------------------------------------------
# 1. mastery mapping
# ---------------------------------------------------------------------------


def test_mastery_maps_to_the_frozen_stage_table() -> None:
    expected = {
        0: LearningStage.BEGINNER,
        1: LearningStage.INTRODUCED,
        2: LearningStage.PRACTICE,
        3: LearningStage.PRACTICE,
        4: LearningStage.TRANSFER,
        5: LearningStage.TRANSFER,
    }
    assert STAGE_BY_MASTERY == expected
    for mastery, stage in expected.items():
        decided, strategy = TeachingPolicy.decide(mastery, review_due=False)
        assert decided is stage
        assert strategy == STRATEGY_BY_STAGE[stage]
    # the frozen strategies
    assert "解释" in STRATEGY_BY_STAGE[LearningStage.BEGINNER]
    assert "例子" in STRATEGY_BY_STAGE[LearningStage.BEGINNER]
    assert "回忆" in STRATEGY_BY_STAGE[LearningStage.INTRODUCED]
    assert "练习" in STRATEGY_BY_STAGE[LearningStage.PRACTICE]
    assert "应用" in STRATEGY_BY_STAGE[LearningStage.TRANSFER]


def test_build_reads_mastery_from_the_store(env) -> None:
    store, curricula, builder, session, concept = env
    policy = TeachingPolicy(store)

    context = policy.build(builder.build("c1"))
    assert context.mastery == 0
    assert context.learning_stage == LearningStage.BEGINNER.value
    assert context.concept_name == "传递函数"
    assert context.concept_id == concept.id

    store.apply_mastery_update(concept.id, 1, "low", "test", source="quiz")
    context = policy.build(builder.build("c1"))
    assert context.mastery == 1
    assert context.learning_stage == LearningStage.INTRODUCED.value

    store.apply_mastery_update(concept.id, 3, "medium", "test", source="quiz")
    assert policy.build(builder.build("c1")).learning_stage == LearningStage.PRACTICE.value

    store.apply_mastery_update(concept.id, 5, "high", "test", source="quiz")
    assert policy.build(builder.build("c1")).learning_stage == LearningStage.TRANSFER.value


def test_unknown_mastery_degrades_to_beginner_posture_without_claiming_mastery() -> None:
    stage, strategy = TeachingPolicy.decide(None, review_due=False)
    assert stage is LearningStage.BEGINNER
    assert strategy == STRATEGY_BY_STAGE[LearningStage.BEGINNER]


def test_decision_is_deterministic(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")
    policy = TeachingPolicy(store)
    first = policy.build(builder.build("c1"))
    for _ in range(5):
        again = policy.build(builder.build("c1"))
        assert (again.learning_stage, again.recommended_strategy) == (
            first.learning_stage, first.recommended_strategy
        )


# ---------------------------------------------------------------------------
# 2. review-due priority
# ---------------------------------------------------------------------------


def test_review_due_wins_over_mastery(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 4, "high", "test", source="quiz")
    policy = TeachingPolicy(store)
    assert policy.build(builder.build("c1")).learning_stage == LearningStage.TRANSFER.value

    store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00")
    context = policy.build(builder.build("c1"))
    assert context.learning_stage == LearningStage.REVIEW.value
    assert context.source == TeachingSource.REVIEW_DUE.value
    assert context.mastery == 4  # mastery is reported, never rewritten
    assert context.recommended_strategy == STRATEGY_BY_STAGE[LearningStage.REVIEW]


def test_future_review_is_not_due(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")
    store.create_review_item(concept.id, due_at="2999-01-01T00:00:00+00:00")
    context = TeachingPolicy(store).build(builder.build("c1"))
    assert context.learning_stage == LearningStage.PRACTICE.value
    assert context.source != TeachingSource.REVIEW_DUE.value


def test_completed_review_is_not_due(env) -> None:
    store, curricula, builder, session, concept = env
    item = store.create_review_item(concept.id, due_at="2020-01-01T00:00:00+00:00")
    with store.connect() as connection:
        connection.execute(
            "UPDATE review_items SET status='completed' WHERE id=?", (item.id,)
        )
    context = TeachingPolicy(store).build(builder.build("c1"))
    assert context.learning_stage != LearningStage.REVIEW.value


# ---------------------------------------------------------------------------
# 3. safe degradation
# ---------------------------------------------------------------------------


def test_no_focus_degrades_safely(env) -> None:
    store, curricula, builder, session, concept = env
    context = TeachingPolicy(store).build(
        LearningContext(course_id="c1", course_name="自动控制原理", current_focus=None)
    )
    assert context is not None
    assert context.concept_id is None
    assert context.concept_name is None
    assert context.mastery is None  # no mastery claim is invented
    assert context.source == TeachingSource.NO_CONCEPT.value
    assert context.recommended_strategy == NO_CONCEPT_STRATEGY
    assert context.context_block() is None  # nothing to inject


def test_unknown_focus_does_not_guess(env) -> None:
    """A focus that is not in the course resolves via the most recent concept."""
    store, curricula, builder, session, concept = env
    context = TeachingPolicy(store).build(
        LearningContext(course_id="c1", course_name="自动控制原理",
                        current_focus="不存在的概念")
    )
    # the fallback is the course's most recently studied concept — deterministic
    assert context.concept_id == concept.id


def test_empty_course_has_no_teaching_context(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    _add_course(store, "c-empty", "空课程")
    builder = LearningContextBuilder(store, CurriculumStore(store))
    context = TeachingPolicy(store).build(builder.build("c-empty"))
    assert context is not None
    assert context.concept_id is None
    assert context.mastery is None


def test_no_course_returns_none(env) -> None:
    store, curricula, builder, session, concept = env
    assert TeachingPolicy(store).build(None) is None


def test_context_block_contains_the_frozen_lines(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")
    block = TeachingPolicy(store).build(builder.build("c1")).context_block()
    assert "当前概念：传递函数" in block
    assert "教学阶段：PRACTICE" in block
    assert "教学策略：出一道练习题" in block
    assert "掌握度：2/5" in block
    # the prompt explicitly forbids the model from deciding mastery
    assert "不要自行判定掌握程度" in block


def test_context_lines_expose_stage_and_mastery(env) -> None:
    store, curricula, builder, session, concept = env
    context = TeachingPolicy(store).build(builder.build("c1"))
    lines = context.lines()
    assert any("教学阶段：BEGINNER" in line for line in lines)
    assert any("教学策略：" in line for line in lines)
    assert context.stage is LearningStage.BEGINNER
    assert context.has_concept is True
    assert context.is_review is False


# ---------------------------------------------------------------------------
# 4. read-only: no database writes
# ---------------------------------------------------------------------------


def test_building_teaching_context_never_writes(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 3, "medium", "existing", source="quiz")
    store.record_assessment("c1", concept.id, score=0.9)
    before = _facts(store, "c1", concept.id)

    policy = TeachingPolicy(store)
    for _ in range(3):
        context = policy.build(builder.build("c1"))
        context.context_block()
        context.lines()
    assert _facts(store, "c1", concept.id) == before


def test_teaching_layer_never_touches_mastery(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 4, "high", "existing", source="quiz")
    audit_before = len(store.list_mastery_audit(concept.id))
    TeachingPolicy(store).build(builder.build("c1")).context_block()
    assert store.get_concept(concept.id).mastery_level == 4
    assert len(store.list_mastery_audit(concept.id)) == audit_before


def test_assessment_history_only_affects_provenance(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 2, "medium", "test", source="quiz")
    policy = TeachingPolicy(store)
    assert policy.build(builder.build("c1")).source == TeachingSource.MASTERY.value

    store.record_assessment("c1", concept.id, score=0.8)
    context = policy.build(builder.build("c1"))
    assert context.source == TeachingSource.MASTERY_WITH_EVIDENCE.value
    # ...but the stage/strategy decision is unchanged
    assert context.learning_stage == LearningStage.PRACTICE.value


# ---------------------------------------------------------------------------
# 5. no provider / LLM involvement
# ---------------------------------------------------------------------------


def test_teaching_never_calls_a_provider(env, monkeypatch) -> None:
    import core.ai_router

    def explode(*args, **kwargs):
        raise AssertionError("TeachingPolicy must never call a provider")

    monkeypatch.setattr(core.ai_router, "chat", explode)
    store, curricula, builder, session, concept = env
    context = TeachingPolicy(store).build(builder.build("c1"))
    assert context.learning_stage == LearningStage.BEGINNER.value
    assert context.context_block() is not None


def test_teaching_module_has_no_forbidden_imports() -> None:
    forbidden = {
        "PySide6", "PyQt5", "PyQt6", "ui", "core.ai_router", "core.providers",
        "core.pagelens_bridge", "core.screen_vision", "core.memory", "openai",
        "anthropic", "requests", "httpx", "core.video_study",
        # isolation from the decision layers
        "core.learning.rule_engine", "core.learning.assessment",
        "core.learning.quiz_adapter", "core.learning.review_scheduler",
    }
    offenders: dict[str, set[str]] = {}
    for path in sorted(TEACHING_DIR.glob("*.py")):
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
    assert not offenders, f"teaching imports forbidden modules: {offenders}"


def test_rule_engine_does_not_import_teaching() -> None:
    """The dependency direction is one-way: facts -> posture."""
    engine = (
        Path(__file__).resolve().parents[1] / "core" / "learning" / "rule_engine.py"
    )
    tree = ast.parse(engine.read_text(encoding="utf-8"), filename=str(engine))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "teaching" not in node.module
        if isinstance(node, ast.Import):
            assert all("teaching" not in alias.name for alias in node.names)


def test_teaching_context_is_immutable_and_validated() -> None:
    context = TeachingContext(concept_name="传递函数", mastery=2)
    with pytest.raises(Exception):
        context.mastery = 5  # type: ignore[misc]
    with pytest.raises(ValueError):
        TeachingContext(mastery=9)
    with pytest.raises(ValueError):
        TeachingContext(learning_stage="not-a-stage")


# ---------------------------------------------------------------------------
# 6. the runner receives the teaching context
# ---------------------------------------------------------------------------


def test_runner_receives_teaching_context(env) -> None:
    store, curricula, builder, session, concept = env
    store.apply_mastery_update(concept.id, 3, "medium", "test", source="quiz")

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
    runner.perform("传递函数是什么？")

    block = captured["turn_context"]
    assert block is not None
    assert "教学阶段：PRACTICE" in block
    assert "教学策略：出一道练习题" in block
    # the curriculum structure block is still present (composed, not replaced)
    assert "当前章节：第二章 数学模型" in block


def test_runner_teaching_block_absent_outside_mode(env) -> None:
    store, curricula, builder, session, concept = env
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
    assert controller.teaching_context() is None


# ---------------------------------------------------------------------------
# 7. ordinary chat is unaffected
# ---------------------------------------------------------------------------


def test_controller_exposes_read_only_teaching_hooks(env) -> None:
    store, curricula, builder, session, concept = env
    from core.learning.controller import LearningModeController

    controller = LearningModeController(store=store, settings=None, curriculum_store=curricula)
    # no active course -> no posture
    assert controller.teaching_context() is None
    assert controller.teaching_block() is None

    controller.state.enabled = True
    controller.state.active_course_id = "c1"
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = session.id
    context = controller.teaching_context()
    assert context is not None
    assert "教学阶段" in controller.teaching_block()


def test_ordinary_questions_are_not_hijacked_by_teaching(env) -> None:
    """Teaching posture never turns ordinary messages into learning commands."""
    store, curricula, builder, session, concept = env
    from core.learning.controller import LearningModeController

    controller = LearningModeController(store=store, settings=None, curriculum_store=curricula)
    controller.state.enabled = True
    controller.state.active_course_id = "c1"
    controller.state.active_course_name = "自动控制原理"
    controller.state.active_session_id = session.id

    for text in ("解释一下传递函数", "今天天气不错", "帮我写一段代码"):
        assert controller.handle_text(text) is None

    before = _facts(store, "c1", concept.id)
    controller.teaching_context().context_block()
    assert _facts(store, "c1", concept.id) == before
