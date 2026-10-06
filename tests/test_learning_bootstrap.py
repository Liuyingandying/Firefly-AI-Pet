"""Initial Learning Bootstrap tests (Phase 9C).

The four frozen scenarios:

    场景1 新建课程首次「然后呢，怎么学？」→ 回答含课程名 / 第一章 / 学习入口
    场景2 已有学习记录 → Bootstrap 不触发
    场景3 无 curriculum → 普通聊天
    场景4 Provider 返回「好的」→ Contract 失败

Plus: bootstrap purity (no writes, no LLM, no decision change) and the
contract integration. Pure in-memory + tmp_path SQLite.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.learning.bootstrap import (
    LearningBootstrapContext,
    build_learning_bootstrap,
)
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
from core.learning.decision import LearningDecisionPolicy
from core.learning.orchestrator import LearningLoopOrchestrator, build_response_contract
from core.learning.store import LearningStore
from ui.character_conversation_runner import CharacterConversationRunner


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def fresh(tmp_path):
    """A new course with a confirmed two-chapter curriculum, never studied."""
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("自动控制原理")
    course_id = controller.state.active_course_id
    draft = CurriculumDraft(
        id="d1", course_id=course_id, title="自动控制原理",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(
            ChapterDraft(
                id="ch1", title="第一章 绪论", position=1,
                concepts=(ChapterConceptDraft(
                    proposal=ConceptProposal(
                        proposal_id="p1", name="自动控制概述"), position=1),),
            ),
            ChapterDraft(
                id="ch2", title="第二章 数学模型", position=2,
                concepts=(ChapterConceptDraft(
                    proposal=ConceptProposal(
                        proposal_id="p2", name="传递函数"), position=1),),
            ),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch1"),
            PathStepDraft("s2", 2, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("d1", confirmed_by="user")
    return store, curricula, controller, course_id


def _study(store, controller, course_id: str, name: str) -> None:
    """Simulate study history: interaction + session touch."""
    session = store.start_session(course_id)
    concept = store.find_concept(course_id, name)
    store.touch_concept(session.id, concept.id, "study")


# ---------------------------------------------------------------------------
# scenario 1 — fresh course, first "然后呢，怎么学？"
# ---------------------------------------------------------------------------


def test_scenario1_fresh_course_answer_orients_the_learner(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("然后呢，怎么学？")
    contract = build_response_contract(result)

    # the bootstrap exists and points at the curriculum's first item
    assert result.bootstrap is not None
    assert result.bootstrap.first_chapter == "第一章 绪论"
    assert result.bootstrap.first_focus == "自动控制概述"
    assert result.bootstrap.source == "active_curriculum_first_item"
    # the decision itself is unchanged (bootstrap is NOT a decision)
    assert result.action == "free_chat"
    # the contract carries the bootstrap required elements
    assert contract.required_elements == (
        "课程定位", "开始章节", "第一学习任务", "下一步交互",
    )
    # the anchors make a conforming answer verifiable
    prompt = contract.prompt_block()
    assert "课程：自动控制原理" in prompt
    assert "开始章节：第一章 绪论" in prompt
    assert "第一学习任务：自动控制概述" in prompt


def test_scenario1_conforming_answer_contains_course_chapter_and_entry(
    tmp_path,
) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    result = orchestrator.run("然后呢，怎么学？")
    contract = build_response_contract(result)

    conforming = (
        f"课程定位：我们在学《{contract.course}》。"
        f"开始章节：{contract.next_step and '第一章 绪论'}，"
        "第一学习任务：先掌握自动控制概述。"
        "下一步交互：告诉我你想从哪里开始。"
    )
    assert contract.is_satisfied(conforming) is True
    assert contract.missing_anchors(conforming) == ()


# ---------------------------------------------------------------------------
# scenario 2 — existing learning history: bootstrap does not trigger
# ---------------------------------------------------------------------------


def test_scenario2_existing_history_no_bootstrap(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    _study(store, controller, course_id, "自动控制概述")
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("然后呢，怎么学？")
    assert result.bootstrap is None
    contract = build_response_contract(result)
    assert contract is not None          # the normal contract still applies
    assert "开始章节" not in (contract.prompt_block() or "")

    # a course with assessment history (quiz-only) is equally not fresh
    concept = store.find_concept(course_id, "传递函数")
    store.record_assessment(course_id, concept.id, score=0.9)
    result2 = orchestrator.run("然后呢，怎么学？")
    assert result2.bootstrap is None


def test_scenario2_interaction_history_blocks_bootstrap(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    orchestrator.run("解释一下传递函数")  # records an interaction fact

    result = orchestrator.run("然后呢，怎么学？")
    assert result.bootstrap is None      # the learner already has a focus
    assert controller.learning_context().current_focus == "传递函数"


# ---------------------------------------------------------------------------
# scenario 3 — no curriculum → ordinary chat
# ---------------------------------------------------------------------------


def test_scenario3_no_curriculum_is_ordinary_chat(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("空课程")
    orchestrator = LearningLoopOrchestrator(controller)

    result = orchestrator.run("然后呢，怎么学？")
    assert result.bootstrap is None
    assert result.action == "free_chat"
    assert result.context_block is not None       # runtime block only
    assert "开始章节" not in result.context_block  # no bootstrap elements
    contract = build_response_contract(result)
    assert contract is None or (
        "开始章节" not in (contract.prompt_block() or "")
    )


# ---------------------------------------------------------------------------
# scenario 4 — provider returns 「好的」 → the contract fails
# ---------------------------------------------------------------------------


def test_scenario4_provider_good_fails_the_contract(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    result = orchestrator.run("然后呢，怎么学？")
    contract = build_response_contract(result)

    assert contract.is_satisfied("好的") is False
    missing = contract.missing_anchors("好的")
    assert "自动控制原理" in missing
    assert "第一章 绪论" in missing
    assert "自动控制概述" in missing


def test_scenario4_with_the_real_runner(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    result = orchestrator.run("然后呢，怎么学？")

    runtime, captured = SimpleNamespace(chat=None), {"prompts": [], "calls": 0}

    def chat(user_message, *, history=None, model=None, temperature=0.2,
             turn_context=None):
        captured["prompts"].append(turn_context)
        captured["calls"] += 1
        return {"choices": [{"message": {"content": "好的"}}]}

    runtime.chat = chat
    runner = CharacterConversationRunner(
        runtime=runtime, learning_controller=controller
    )
    runner.perform("然后呢，怎么学？", learning_result=result)

    prompt = captured["prompts"][0]
    assert "课程：自动控制原理" in prompt
    assert "开始章节：第一章 绪论" in prompt
    assert "第一学习任务" in prompt
    # the contract failed on 「好的」 and the enforcement retried once
    assert captured["calls"] == 2
    assert runner.last_contract_state["met"] is False


def test_scenario4_conforming_answer_satisfies_the_contract(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    result = orchestrator.run("然后呢，怎么学？")
    contract = build_response_contract(result)

    conforming = (
        "课程定位：我们在学《自动控制原理》。开始章节：第一章 绪论。"
        "第一学习任务：先掌握自动控制概述。下一步交互：告诉我想从哪里开始。"
    )
    assert contract.is_satisfied(conforming) is True


# ---------------------------------------------------------------------------
# bootstrap builder units
# ---------------------------------------------------------------------------


def test_bootstrap_requires_an_active_curriculum(tmp_path) -> None:
    from core.learning.context import LearningContext
    from core.learning.curriculum import Curriculum as CurriculumModel
    from core.learning.curriculum import CurriculumView

    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("无结构课程")
    context = LearningContext(course_id=course.id, course_name="无结构课程")
    assert build_learning_bootstrap(store, None, course_id=course.id,
                                   learning_context=context) is None
    # an empty curriculum view degrades to None too
    empty_view = CurriculumView.of(
        CurriculumModel(id="cur1", course_id=course.id, title="t", version=1)
    )
    assert build_learning_bootstrap(store, empty_view, course_id=course.id,
                                   learning_context=context) is None


def test_bootstrap_ignores_history_courses(tmp_path) -> None:
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("已学课程")
    concept = store.add_concept(course.id, "旧概念")
    store.apply_mastery_update(concept.id, 4, "high", "existing", source="quiz")
    view = None  # no curriculum
    bootstrap = build_learning_bootstrap(store, view, course_id=course.id,
                                         course_name="已学课程",
                                         learning_context=None)
    assert bootstrap is None or bootstrap.first_focus is None


def test_bootstrap_is_read_only(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    concept = store.find_concept(course_id, "自动控制概述")
    assert concept is not None
    before = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
        len(store.list_sessions(course_id)),
        controller.learning_context().current_focus,
    )
    view = curricula.get_active_curriculum(course_id)
    bootstrap = build_learning_bootstrap(
        store, view, course_id=course_id, course_name="自动控制原理",
        learning_context=controller.learning_context(),
    )
    after = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
        len(store.list_sessions(course_id)),
        controller.learning_context().current_focus,
    )
    assert bootstrap is not None
    assert before == after
    assert after[5] is None  # current_focus untouched (still None)


def test_bootstrap_lines_carry_the_three_frozen_elements(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    view = curricula.get_active_curriculum(course_id)
    bootstrap = build_learning_bootstrap(
        store, view, course_id=course_id, course_name="自动控制原理",
        learning_context=controller.learning_context(),
    )
    lines = bootstrap.lines()
    assert lines[0].startswith("课程定位：")
    assert lines[1].startswith("开始章节：")
    assert lines[2].startswith("第一学习任务：")


# ---------------------------------------------------------------------------
# runner injection (bootstrap rides the learning_result into the contract)
# ---------------------------------------------------------------------------


def test_runner_injects_the_bootstrap_contract(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    orchestrator = LearningLoopOrchestrator(controller)
    result = orchestrator.run("然后呢，怎么学？")

    captured: dict = {}

    def chat(user_message, *, history=None, model=None, temperature=0.2,
             turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=chat), learning_controller=controller
    )
    runner.perform("然后呢，怎么学？", learning_result=result)

    prompt = captured["turn_context"]
    assert "课程：自动控制原理" in prompt
    assert "开始章节：第一章 绪论" in prompt
    assert "第一学习任务：自动控制概述" in prompt


def test_controller_bootstrap_accessor(tmp_path) -> None:
    store, curricula, controller, course_id = fresh(tmp_path)
    assert controller.learning_bootstrap() is not None
    controller.exit_mode()
    assert controller.learning_bootstrap() is None  # mode off → no bootstrap
