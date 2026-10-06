"""Bootstrap Response Priority tests (Phase 9C.1).

Frozen verification:

    fake provider 「这本书很难，我们一起加油。」 → contract fail
    合规回答（课程名 / 章节 / 学习任务） → pass
    bootstrap contract 是最后一条 system 消息（优先级最高）
    重试保留 bootstrap context
    决策不变（FREE_CHAT）、零写入、无 LLM 判断
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.learning.bootstrap import LearningBootstrapContext
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
from core.learning.interactions import InteractionStore
from core.learning.orchestrator import (
    LearningLoopOrchestrator,
    LearningLoopResult,
    build_response_contract,
)
from core.learning.store import LearningStore
from ui.character_conversation_runner import CharacterConversationRunner


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


def _fresh(tmp_path):
    """A fresh course: ACTIVE curriculum, focus None, zero history."""
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
    bootstrap = controller.learning_bootstrap()
    assert bootstrap is not None
    result = LearningLoopResult(
        status="learning_loop", action="free_chat", source="no_focus",
        context_block="mode+course 上下文",
        learning_context=controller.learning_context(),
        teaching_context=None,
        decision=LearningDecisionPolicy.decide(focus=None),
        bootstrap=bootstrap,
    )
    return store, curricula, controller, course_id, bootstrap, result


def _runner_with(responses: list[str]):
    captured: dict = {"prompts": [], "calls": 0}

    class _Runtime:
        def chat(self, user_message, *, history=None, model=None,
                 temperature=0.2, turn_context=None):
            captured["prompts"].append(turn_context)
            captured["calls"] += 1
            reply = responses[min(captured["calls"] - 1, len(responses) - 1)]
            return {"choices": [{"message": {"content": reply}}]}

    runner = CharacterConversationRunner(
        runtime=SimpleNamespace(chat=_Runtime().chat), learning_controller=None
    )
    return runner, captured


# ---------------------------------------------------------------------------
# the frozen failing-provider verification
# ---------------------------------------------------------------------------


def test_chatty_provider_fails_the_bootstrap_contract(tmp_path) -> None:
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)
    runner, captured = _runner_with(["这本书很难，我们一起加油。"])
    runner.learning_controller = controller

    runner.perform("怎么学？", learning_result=result)

    prompt = captured["prompts"][0]
    assert "本回合必须优先输出以下学习启动信息" in prompt
    assert "课程定位：自动控制原理" in prompt
    assert "开始章节：第一章 绪论" in prompt
    assert "第一学习任务：自动控制概述" in prompt
    # the chatty answer missed every anchor → the contract FAILED
    assert runner.last_contract_state["met"] is False
    assert "自动控制原理" in runner.last_contract_state["missing"]
    assert "第一章 绪论" in runner.last_contract_state["missing"]
    assert "自动控制概述" in runner.last_contract_state["missing"]


def test_conforming_answer_passes_and_leads_with_the_start(tmp_path) -> None:
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)
    conforming = (
        "我们现在开始学习《自动控制原理》。按照教材结构，第一步从"
        "第一章 绪论开始，第一学习任务是理解自动控制概述。"
        "你可以先告诉我，你对自动控制系统有什么了解？"
    )
    runner, captured = _runner_with([conforming])
    runner.learning_controller = controller

    runner.perform("怎么学？", learning_result=result)

    assert runner.last_contract_state["met"] is True
    assert runner.last_contract_state["missing"] == ()
    assert captured["calls"] == 1                       # satisfied → no retry


def test_priority_directive_requires_the_start_at_the_top(tmp_path) -> None:
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)
    contract = build_response_contract(result)
    block = contract.prompt_block()
    assert block.startswith("学习响应契约（优先级最高，覆盖陪伴默认开场）：")
    assert "本回合必须优先输出以下学习启动信息" in block
    assert "置于回答开头" in block
    assert "不要只做闲聊鼓励" in block
    assert "- 课程定位：自动控制原理" in block
    assert "- 开始章节：第一章 绪论" in block
    assert "- 第一学习任务：自动控制概述" in block
    assert "- 用户下一步动作：邀请学习者确认或开始第一学习任务" in block


def test_bootstrap_data_is_verbatim_nothing_hardcoded(tmp_path) -> None:
    """A different textbook yields a different contract — nothing hardcoded."""
    store = LearningStore(tmp_path / "other.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("物理光学")
    course_id = controller.state.active_course_id
    draft = CurriculumDraft(
        id="d2", course_id=course_id, title="物理光学",
        sources=(CurriculumSource(kind="manual", title="手动录入"),),
        chapters=(ChapterDraft(
            id="ch1", title="第一章 光的电磁理论", position=1,
            concepts=(ChapterConceptDraft(
                proposal=ConceptProposal(
                    proposal_id="p1", name="光程"), position=1),),
        ),),
        paths=(LearningPathDraft(id="p1", title="默认", steps=(
            PathStepDraft("s1", 1, "chapter", "ch1"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("d2", confirmed_by="user")
    bootstrap = controller.learning_bootstrap()
    assert bootstrap is not None
    result = LearningLoopResult(
        status="learning_loop", action="free_chat", source="no_focus",
        context_block="x", bootstrap=bootstrap,
        learning_context=controller.learning_context(),
    )
    block = build_response_contract(result).prompt_block()
    assert "物理光学" in block
    assert "光的电磁理论" in block
    assert "光程" in block
    assert "自动控制" not in block


# ---------------------------------------------------------------------------
# non-bootstrap contracts unchanged
# ---------------------------------------------------------------------------


def test_non_bootstrap_contract_keeps_the_plain_block(tmp_path) -> None:
    store = LearningStore(tmp_path / "l.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("自动控制原理")
    from core.learning.context import LearningContextBuilder

    context = LearningContextBuilder(
        store, curricula
    ).build(controller.state.active_course_id)
    decision = LearningDecisionPolicy.decide(
        focus=("k", "传递函数"), mastery=2
    )
    result = LearningLoopResult(
        status="learning_loop", action="practice", source="mastery",
        context_block="mode 上下文",
        learning_context=context, teaching_context=None,
        decision=decision,
    )
    contract = build_response_contract(result)
    block = contract.prompt_block()
    assert "优先级最高" not in block
    assert "学习动作：PRACTICE" in block
    assert "下一步" in block or "回答必须包含" in block


def test_free_chat_without_bootstrap_has_no_contract(tmp_path) -> None:
    """无 bootstrap 的 FREE_CHAT → 无契约 → 普通聊天。"""
    store = LearningStore(tmp_path / "l.sqlite3")
    store.initialize()
    curricula = CurriculumStore(store)
    controller = LearningModeController(
        store=store, settings=_Settings(), curriculum_store=curricula
    )
    controller.enter_mode()
    controller.create_course("自动控制原理")
    context = controller.learning_context()
    decision = LearningDecisionPolicy.decide(focus=None)
    result = LearningLoopResult(
        status="learning_loop", action="free_chat", source="no_focus",
        context_block="mode 上下文",
        learning_context=context, decision=decision,
        bootstrap=None,
    )
    assert build_response_contract(result) is not None
    assert build_response_contract(result).required_elements == ()
    assert "开始章节" not in (
        build_response_contract(result).prompt_block() or ""
    )


# ---------------------------------------------------------------------------
# retry preserves the bootstrap context
# ---------------------------------------------------------------------------


def test_retry_preserves_the_bootstrap_context(tmp_path) -> None:
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)
    runner, captured = _runner_with(["这本书很难，我们一起加油。", "这本书很难。"])
    runner.learning_controller = controller

    runner.perform("怎么学？", learning_result=result)

    assert captured["calls"] == 2
    first_prompt, retry_prompt = captured["prompts"][0], captured["prompts"][1]
    for token in ("开始章节：第一章 绪论", "第一学习任务：自动控制概述",
                  "本回合必须优先输出以下学习启动信息"):
        assert token in retry_prompt, token
        assert token in first_prompt
    assert "没有覆盖学习契约" in retry_prompt


# ---------------------------------------------------------------------------
# decision / mastery untouched
# ---------------------------------------------------------------------------


def test_decision_stays_free_chat_and_nothing_is_written(tmp_path) -> None:
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)
    concept = store.find_concept(course_id, "自动控制概述")
    before = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
        len(store.list_sessions(course_id)),
        len(InteractionStore(store).list_interactions(course_id)),
        controller.learning_context().current_focus,
    )
    runner, captured = _runner_with(["好的"])
    runner.learning_controller = controller
    runner.perform("怎么学？", learning_result=result)

    # the decision is unchanged — bootstrap is not a decision
    assert result.action == "free_chat"
    after = (
        store.get_concept(concept.id).mastery_level,
        len(store.list_mastery_audit(concept.id)),
        len(store.list_assessments(concept.id)),
        len(store.list_review_items(concept.id)),
        len(store.list_sessions(course_id)),
        len(InteractionStore(store).list_interactions(course_id)),
        controller.learning_context().current_focus,
    )
    assert before == after
    assert controller.learning_context().current_focus is None  # 未改变


def test_contract_check_is_pure_string_containment(tmp_path) -> None:
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)
    contract = build_response_contract(result)
    # a response containing the anchors passes without any model call;
    # one missing them fails — both deterministically
    assert contract.is_satisfied(
        "课程定位：自动控制原理，开始章节：第一章 绪论，"
        "第一学习任务：自动控制概述"
    ) is True
    assert contract.is_satisfied("好的") is False


# ---------------------------------------------------------------------------
# runner injection + ordering lock
# ---------------------------------------------------------------------------


def test_runner_injects_the_bootstrap_contract(tmp_path) -> None:
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)

    captured: dict = {}

    def chat(user_message, *, history=None, model=None, temperature=0.2,
             turn_context=None):
        captured["turn_context"] = turn_context
        return {"choices": [{"message": {"content": "好的"}}]}

    runtime = SimpleNamespace(
        chat=chat, conversation_store=None,
        _try_advance_bond=lambda *a, **k: None,
        _try_extract_suggestions=lambda *a, **k: None,
        _begin_turn=lambda *a, **k: None,
        _finish_turn=lambda *a, **k: None,
        _record_error=lambda *a, **k: None,
    )
    CharacterConversationRunner(
        runtime=runtime, learning_controller=controller
    ).perform("然后呢，怎么学？", learning_result=result)

    prompt = captured["turn_context"]
    assert "课程：自动控制原理" in prompt
    assert "开始章节：第一章 绪论" in prompt
    assert "第一学习任务：自动控制概述" in prompt


def test_contract_is_the_last_system_message(tmp_path) -> None:
    """顺序锁定：persona/bond/memory/narrative 之后、紧贴用户消息之前。"""
    store, curricula, controller, course_id, bootstrap, result = _fresh(tmp_path)
    contract = build_response_contract(result)

    from PySide6.QtWidgets import QLabel

    # replicate the runtime's message assembly: identity/bond/memory/narrative
    # system messages, then the contract inserted immediately before the user
    messages = [
        {"role": "system", "content": "personality"},
        {"role": "system", "content": "bond"},
        {"role": "system", "content": "memory"},
        {"role": "user", "content": "怎么学？"},
    ]
    messages.insert(-1, {"role": "system", "content": contract.prompt_block()})
    system_texts = [m["content"] for m in messages if m["role"] == "system"]
    # the contract is the LAST system message → highest priority position
    contract_texts = [t for t in system_texts if "学习响应契约" in t]
    assert len(contract_texts) == 1
    assert system_texts.index(contract_texts[0]) == len(system_texts) - 1
    # the user message is the final message
    assert messages[-1]["role"] == "user"
