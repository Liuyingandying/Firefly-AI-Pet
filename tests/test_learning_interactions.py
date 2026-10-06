"""Learning interaction tracking tests (Phase 3.5).

Covers the frozen contract:

    discussion updates focus / ordinary chat writes nothing / PageLens writes
    nothing / mastery unchanged / Rule Engine does not depend on interactions /
    context reads the right focus / failure-safe degradation

plus the append-only and no-provider guarantees. Pure in-memory + tmp_path
SQLite; no Qt, no provider.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.learning.context import LearningContextBuilder
from core.learning.controller import LearningModeController
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
from core.learning.interactions import (
    InteractionEventType,
    InteractionRecorder,
    InteractionSource,
    InteractionStore,
    LearningInteractionEvent,
    match_course_concept,
)
from core.learning.store import LearningStore


ROOT = Path(__file__).resolve().parents[1]


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _seed(tmp_path, *, touch: bool = False, concepts=("传递函数", "方框图")):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    placements = tuple(
        ChapterConceptDraft(
            proposal=ConceptProposal(proposal_id=f"p{index}", name=name), position=index
        )
        for index, name in enumerate(concepts, start=1)
    )
    draft = CurriculumDraft(
        id="d1",
        course_id=course_id,
        title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(
                id="ch2", title="第二章 数学模型", position=1, concepts=placements
            ),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("d1", confirmed_by="user")
    if touch:
        session = store.start_session(course_id)
        store.touch_concept(
            session.id, store.find_concept(course_id, concepts[0]).id, "study"
        )
    return store, curricula, controller, course_id


def _facts(store: LearningStore, course_id: str, concept_id: str) -> tuple:
    return (
        store.get_concept(concept_id).mastery_level,
        len(store.list_mastery_audit(concept_id)),
        len(store.list_assessments(concept_id)),
        len(store.list_review_items(concept_id)),
        len(store.list_concepts(course_id)),
        len(store.list_sessions(course_id)),
    )


# ---------------------------------------------------------------------------
# 1. discussing a concept updates the focus
# ---------------------------------------------------------------------------


def test_discussing_a_concept_updates_focus(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    builder = LearningContextBuilder(store, curricula, InteractionStore(store))
    assert builder.build(course_id).current_focus is None

    # an explicit explanation request is a fact
    assert controller.handle_text("解释一下传递函数") is None  # ordinary chat continues
    context = builder.build(course_id)
    assert context.current_focus == "传递函数"
    assert context.current_chapter == "第二章 数学模型"

    # a later discussion of another concept moves the focus
    controller.handle_text("方框图这里没懂")
    assert builder.build(course_id).current_focus == "方框图"


def test_interaction_takes_priority_over_the_session(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, touch=True)
    builder = LearningContextBuilder(store, curricula, InteractionStore(store))
    # the session touched 传递函数 ...
    assert builder.build(course_id).current_focus == "传递函数"
    # ... but a newer interaction about 方框图 wins
    controller.handle_text("学习一下方框图")
    assert builder.build(course_id).current_focus == "方框图"


def test_event_types_and_sources_are_recorded(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    interactions = InteractionStore(store)
    controller.handle_text("解释一下传递函数")     # concept_explanation
    controller.handle_text("学习一下方框图")         # user_request
    controller.handle_text("传递函数这里没懂")      # concept_discussion
    events = interactions.list_interactions(course_id)
    assert [e.event_type for e in events] == [
        InteractionEventType.CONCEPT_EXPLANATION.value,
        InteractionEventType.USER_REQUEST.value,
        InteractionEventType.CONCEPT_DISCUSSION.value,
    ]
    assert all(e.source == InteractionSource.USER_EXPLICIT.value for e in events)


def test_assessment_start_is_recorded_as_a_system_fact(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, concepts=("传递函数",))
    from core.learning.assessment import AssessmentService
    from core.learning.quiz_adapter import QuizGrading

    controller.assessment_service = AssessmentService(
        store,
        generate_question=lambda c, k: f"{c}的定义是什么？",
        grade_answer=lambda c, k, q, a: QuizGrading(q, a, "正确的部分：好"),
    )
    assert controller.handle_text("考考我")
    events = InteractionStore(store).list_interactions(course_id)
    assert [e.event_type for e in events] == [InteractionEventType.ASSESSMENT_START.value]
    assert events[0].source == InteractionSource.SYSTEM_GENERATED.value


def test_recording_is_append_only(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    interactions = InteractionStore(store)
    controller.handle_text("解释一下传递函数")
    controller.handle_text("解释一下传递函数")
    assert interactions.count(course_id) == 2  # every fact is kept
    # no update/delete API exists
    assert not any(
        name in ("update", "delete", "remove", "clear")
        for name in dir(InteractionStore)
    )


# ---------------------------------------------------------------------------
# 2. ordinary chat writes nothing
# ---------------------------------------------------------------------------


def test_ordinary_chat_writes_nothing(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    interactions = InteractionStore(store)
    for text in ("今天天气不错", "帮我写一段 Python 代码", "什么是洛必达法则", "你好呀"):
        assert controller.handle_text(text) is None
    assert interactions.count() == 0


def test_unknown_term_is_never_extracted(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concepts = store.list_concepts(course_id)
    # "拉普拉斯变换" is a real concept of the subject but NOT stored in this
    # course: mentioning it must not create an event or a concept.
    assert match_course_concept("解释一下拉普拉斯变换", concepts) is None
    controller.handle_text("解释一下拉普拉斯变换")
    assert InteractionStore(store).count() == 0
    assert len(store.list_concepts(course_id)) == 2


def test_mode_off_writes_nothing(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    controller.exit_mode()
    # free chat: the recorder is never reached, even for a concept name
    controller.handle_text("解释一下传递函数")
    assert InteractionStore(store).count() == 0


def test_bare_mention_without_learning_intent_writes_nothing(tmp_path) -> None:
    """A passing mention of a non-focus concept is not a learning interaction."""
    store, curricula, controller, course_id = _seed(tmp_path)
    # focus is 传递函数 after this
    controller.handle_text("解释一下传递函数")
    before = InteractionStore(store).count()
    # 方框图 is mentioned with no explanation/discussion/request marker
    controller.handle_text("方框图")
    assert InteractionStore(store).count() == before


def test_current_focus_mention_counts_as_discussion(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    controller.handle_text("解释一下传递函数")  # focus = 传递函数
    before = InteractionStore(store).count()
    controller.handle_text("传递函数")
    assert InteractionStore(store).count() == before + 1


# ---------------------------------------------------------------------------
# 3. ambient sources write nothing
# ---------------------------------------------------------------------------


def test_pagelens_and_vision_do_not_import_interactions() -> None:
    modules = [
        ROOT / "core" / "pagelens_bridge.py",
        *sorted((ROOT / "core" / "screen_vision").rglob("*.py")),
    ]
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "interactions" not in node.module, path.name
            if isinstance(node, ast.Import):
                assert all(
                    "interactions" not in alias.name for alias in node.names
                ), path.name


def test_ambient_payload_has_no_write_path(tmp_path) -> None:
    """PageLens/OCR style payloads (no course, no resolved concept) write 0 rows."""
    store, curricula, controller, course_id = _seed(tmp_path)
    recorder = InteractionRecorder(store)
    # Ambient data looks like this: free text with no course context — the
    # recorder can only be driven with an explicit course + text.
    for text in ("本页概念：传递函数", "selection: 方框图", ""):
        assert recorder.record_from_message("", text) is None
    assert InteractionStore(store).count() == 0


def test_recorder_rejects_a_concept_from_another_course(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    other = store.create_course("物理光学")
    other_concept = store.add_concept(other.id, "傅里叶变换")
    recorder = InteractionRecorder(store)
    # cross-course assessment fact is refused
    assert recorder.record_assessment_start(course_id, other_concept.id) is None
    assert InteractionStore(store).count() == 0


# ---------------------------------------------------------------------------
# 4. mastery is never touched
# ---------------------------------------------------------------------------


def test_recording_never_changes_mastery_or_learning_facts(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    store.apply_mastery_update(concept.id, 3, "medium", "existing", source="quiz")
    before = _facts(store, course_id, concept.id)

    for text in ("解释一下传递函数", "传递函数这里没懂", "学习一下传递函数", "方框图是什么"):
        controller.handle_text(text)
    assert InteractionStore(store).count() == 4

    assert _facts(store, course_id, concept.id) == before
    assert store.get_concept(concept.id).mastery_level == 3


def test_no_mastery_write_api_in_the_layer() -> None:
    """The interaction layer has no mastery/grading surface at all."""
    for name in ("apply_mastery_update", "record_assessment", "add_concept", "start_session"):
        assert not hasattr(InteractionStore, name)
        assert not hasattr(InteractionRecorder, name)


# ---------------------------------------------------------------------------
# 5. the Rule Engine does not depend on interactions
# ---------------------------------------------------------------------------


def test_rule_engine_does_not_import_interactions() -> None:
    path = ROOT / "core" / "learning" / "rule_engine.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "interactions" not in node.module
        if isinstance(node, ast.Import):
            assert all("interactions" not in alias.name for alias in node.names)


def test_interaction_layer_does_not_import_the_engine() -> None:
    directory = ROOT / "core" / "learning" / "interactions"
    forbidden = (
        "rule_engine", "assessment", "quiz_adapter", "review_scheduler",
        "core.ai_router", "core.providers", "PySide6", "ui", "core.pagelens_bridge",
    )
    offenders: dict[str, set[str]] = {}
    for path in sorted(directory.glob("*.py")):
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
    assert not offenders, f"interactions imports forbidden modules: {offenders}"


def test_rule_engine_decision_is_unaffected_by_interactions(tmp_path) -> None:
    """Mastery evidence produces the same decision with or without events."""
    from core.learning.models import AssessmentEvidence
    from core.learning.rule_engine import LearningRuleEngine

    store, curricula, controller, course_id = _seed(tmp_path)
    concept = store.find_concept(course_id, "传递函数")
    engine = LearningRuleEngine(store)
    evidence = AssessmentEvidence(
        concept_id=concept.id, source="quiz", score=0.95, confidence=0.9,
        difficulty="basic", created_at="2026-06-01T00:00:00+00:00",
    )
    first = engine.evaluate(concept.id, evidence)

    for text in ("解释一下传递函数", "传递函数这里没懂"):
        controller.handle_text(text)
    second = engine.evaluate(concept.id, evidence)

    assert (first.new_mastery, first.changed) == (second.new_mastery, second.changed)
    # evaluate() is a pure PREVIEW (it writes nothing), so the stored mastery
    # stays 0 — recording interactions changed neither the preview nor the fact.
    assert store.get_concept(concept.id).mastery_level == 0
    assert first.new_mastery == 1


# ---------------------------------------------------------------------------
# 6. context reads the right focus
# ---------------------------------------------------------------------------


def test_context_falls_back_to_the_session_then_none(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, touch=True)
    builder = LearningContextBuilder(store, curricula, InteractionStore(store))
    # session fact is used while no interaction exists
    assert builder.build(course_id).current_focus == "传递函数"

    # a course with no session and no interaction has no focus (never guessed)
    empty = store.create_course("空课程")
    assert builder.build(empty.id).current_focus is None


def test_context_focus_survives_a_broken_interaction_store(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path, touch=True)

    class _Broken:
        def latest_for_course(self, course_id):
            raise RuntimeError("boom")

        def list_interactions(self, course_id=None, limit=None):
            raise RuntimeError("boom")

    builder = LearningContextBuilder(store, curricula, _Broken())
    context = builder.build(course_id)
    # degrades to the session fact instead of raising
    assert context.current_focus == "传递函数"


def test_latest_interaction_wins_by_time(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    interactions = InteractionStore(store)
    first = store.find_concept(course_id, "传递函数")
    second = store.find_concept(course_id, "方框图")
    interactions.record(course_id, first.id, "concept_discussion", "user_explicit",
                        created_at="2026-01-01T00:00:00+00:00")
    interactions.record(course_id, second.id, "concept_discussion", "user_explicit",
                        created_at="2026-01-02T00:00:00+00:00")
    assert interactions.latest_for_course(course_id).concept_id == second.id
    builder = LearningContextBuilder(store, curricula, interactions)
    assert builder.build(course_id).current_focus == "方框图"


# ---------------------------------------------------------------------------
# 7. failure-safe degradation
# ---------------------------------------------------------------------------


def test_recorder_degrades_when_the_store_breaks() -> None:
    class _BrokenStore:
        def list_concepts(self, course_id):
            raise RuntimeError("boom")

        def get_concept(self, concept_id):
            raise RuntimeError("boom")

    recorder = InteractionRecorder(_BrokenStore())
    assert recorder.record_from_message("c1", "解释一下传递函数") is None
    assert recorder.record_assessment_start("c1", "k1") is None


def test_recorder_degrades_when_the_insert_fails(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)

    class _FailingInteractions(InteractionStore):
        def record(self, *args, **kwargs):
            raise RuntimeError("disk full")

    recorder = InteractionRecorder(store, interaction_store=_FailingInteractions(store))
    assert recorder.record_from_message(course_id, "解释一下传递函数") is None
    assert InteractionStore(store).count() == 0


def test_chat_turn_survives_a_broken_recorder(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)

    class _Exploding:
        def record_from_message(self, *args, **kwargs):
            raise RuntimeError("boom")

        def record_assessment_start(self, *args, **kwargs):
            raise RuntimeError("boom")

    controller._interaction_recorder = _Exploding()
    # the ordinary-chat path must still return None instead of raising
    assert controller.handle_text("解释一下传递函数") is None


def test_controller_requires_no_recorder(tmp_path) -> None:
    """A controller built without any interaction support still works."""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    controller = LearningModeController(store=store, settings=_Settings())
    controller.enter_mode()
    controller.create_course("自动控制原理")
    assert controller.handle_text("解释一下传递函数") is None
    controller.exit_mode()


def test_interaction_event_is_immutable_and_validated() -> None:
    event = LearningInteractionEvent(
        id="e1", course_id="c1", concept_id="k1",
        event_type="concept_explanation", source="user_explicit",
        created_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(Exception):
        event.course_id = "c2"  # type: ignore[misc]
    with pytest.raises(ValueError):
        LearningInteractionEvent(
            id="e2", course_id="c1", concept_id="k1",
            event_type="not-a-type", source="user_explicit", created_at="t",
        )
    with pytest.raises(ValueError):
        LearningInteractionEvent(
            id="", course_id="c1", concept_id="k1",
            event_type="user_request", source="user_explicit", created_at="t",
        )
    # the enum accepts its own members too
    assert LearningInteractionEvent(
        id="e3", course_id="c1", concept_id="k1",
        event_type=InteractionEventType.USER_REQUEST,
        source=InteractionSource.SYSTEM_GENERATED, created_at="t",
    ).event_type == "user_request"


def test_ordinary_chat_through_the_runner_is_unaffected(tmp_path) -> None:
    store, curricula, controller, course_id = _seed(tmp_path)
    from ui.character_conversation_runner import CharacterConversationRunner

    captured: dict = {}

    def fake_chat(user_message, *, history=None, model=None, temperature=0.2, turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=fake_chat, conversation_store=None),
        learning_controller=controller,
    )
    events, answer = runner.perform("今天天气不错")
    assert answer == "好的"
    assert InteractionStore(store).count() == 0  # ordinary chat recorded nothing
