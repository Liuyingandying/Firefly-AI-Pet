"""Learning Response Contract tests (Phase 9B-1).

The frozen verification:

    输入「怎么学？」→ 最终 prompt 中存在「课程：自动控制原理」与
    「下一步：第二章 数学模型」；fake provider 返回「好的」时，
    Contract 校验必须失败（未被满足）——证明 Action 不只是 prompt，
    而是真正进入最终回答链。

Plus: no recomputation, no provider, ordinary chat untouched. Pure
in-memory; the provider is a fake.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from core.learning.orchestrator import (
    LearningLoopOrchestrator,
    LearningLoopResult,
    LearningResponseContract,
    build_response_contract,
)
from core.learning.store import LearningStore
from ui.character_conversation_runner import CharacterConversationRunner


class _Settings:
    def __init__(self) -> None:
        self.learning_last_course_id = ""

    def set_learning_last_course_id(self, course_id: str) -> None:
        self.learning_last_course_id = course_id or ""


@pytest.fixture()
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def loop(tmp_path):
    """Course 《自动控制原理》 with 第二章 数学模型 as the next node."""
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
            # 第二章 has no linked concepts: its structural node label IS the
            # chapter title, matching the frozen verification example.
            ChapterDraft(id="ch2", title="第二章 数学模型", position=2),
        ),
        paths=(LearningPathDraft(id="path1", title="默认路线", steps=(
            PathStepDraft("s1", 1, "chapter", "ch1"),
            PathStepDraft("s2", 2, "chapter", "ch2"),
        )),),
    )
    curricula.create_draft(draft)
    curricula.confirm_draft("d1", confirmed_by="user")

    session = store.start_session(course_id)
    concept = store.find_concept(course_id, "自动控制概述")
    store.touch_concept(session.id, concept.id, "study")

    orchestrator = LearningLoopOrchestrator(controller)
    return store, curricula, controller, course_id, orchestrator


def _fake_runtime(responses: list[str]):
    captured: dict = {"prompts": [], "calls": 0}

    def chat(user_message, *, history=None, model=None, temperature=0.2,
             turn_context=None):
        captured["prompts"].append({
            "user": user_message,
            "turn_context": turn_context,
        })
        reply = responses[min(captured["calls"], len(responses) - 1)]
        captured["calls"] += 1
        return {"choices": [{"message": {"content": reply}}]}

    return SimpleNamespace(chat=chat), captured


# ---------------------------------------------------------------------------
# contract construction
# ---------------------------------------------------------------------------


def test_contract_carries_course_and_next_step(loop) -> None:
    store, curricula, controller, course_id, orchestrator = loop
    result = orchestrator.run("怎么学？")

    contract = build_response_contract(result)
    assert contract is not None
    assert contract.course == "自动控制原理"
    assert contract.action == "explain"
    assert contract.next_step == "第二章 数学模型"
    assert contract.required_elements == ("课程定位", "第一学习任务", "下一步交互")
    assert "自动控制原理" in contract.anchors
    assert "第二章 数学模型" in contract.anchors


def test_prompt_contains_the_frozen_lines(loop) -> None:
    store, curricula, controller, course_id, orchestrator = loop
    result = orchestrator.run("怎么学？")
    contract = build_response_contract(result)

    block = contract.prompt_block()
    assert "课程：自动控制原理" in block
    assert "下一步：第二章 数学模型" in block
    assert "回答必须包含：课程定位、第一学习任务、下一步交互" in block


# ---------------------------------------------------------------------------
# the frozen failing-provider verification
# ---------------------------------------------------------------------------


def test_non_answering_provider_fails_the_contract(loop) -> None:
    """「好的」不包含课程与下一步锚点 → Contract 校验必须失败。"""
    store, curricula, controller, course_id, orchestrator = loop
    runtime, captured = _fake_runtime(["好的"])

    from ui.character_conversation_runner import CharacterConversationRunner

    runner = CharacterConversationRunner(
        runtime=runtime, learning_controller=controller
    )
    result = orchestrator.run("怎么学？")
    runner.perform("怎么学？", learning_result=result)

    # the prompt carried the contract...
    prompt = captured["prompts"][0]["turn_context"]
    assert "课程：自动控制原理" in prompt
    assert "下一步：第二章 数学模型" in prompt
    # ...but the answer did not satisfy it — the contract evaluation FAILED
    assert captured["calls"] >= 2          # the enforcement retried once
    assert runner.last_contract_state is not None
    assert runner.last_contract_state["met"] is False
    assert "自动控制原理" in runner.last_contract_state["missing"]
    assert "第二章 数学模型" in runner.last_contract_state["missing"]


def test_conforming_answer_passes_the_contract(loop) -> None:
    store, curricula, controller, course_id, orchestrator = loop
    conforming = (
        "课程定位：我们在学《自动控制原理》。第一学习任务：先看第二章 数学模型"
        "的核心概念。下一步交互：告诉我你想从哪里开始。"
    )
    runtime, captured = _fake_runtime([conforming])

    from ui.character_conversation_runner import CharacterConversationRunner

    runner = CharacterConversationRunner(
        runtime=runtime, learning_controller=controller
    )
    result = orchestrator.run("怎么学？")
    runner.perform("怎么学？", learning_result=result)

    prompt = captured["prompts"][0]["turn_context"]
    assert "课程：自动控制原理" in prompt
    assert "下一步：第二章 数学模型" in prompt
    assert runner.last_contract_state["met"] is True
    assert runner.last_contract_state["missing"] == ()
    assert captured["calls"] == 1              # satisfied → no retry


# ---------------------------------------------------------------------------
# no recomputation / isolation
# ---------------------------------------------------------------------------


def test_runner_does_not_recompute_the_loop(loop, monkeypatch) -> None:
    store, curricula, controller, course_id, orchestrator = loop
    result = orchestrator.run("怎么学？")

    def explode():
        raise AssertionError("runner must not recompute the loop")

    monkeypatch.setattr(controller, "learning_loop_block", explode, raising=False)
    monkeypatch.setattr(controller, "learning_context", explode, raising=False)
    monkeypatch.setattr(controller, "teaching_context", explode, raising=False)
    monkeypatch.setattr(controller, "learning_decision", explode, raising=False)
    monkeypatch.setattr(controller, "learning_action", explode, raising=False)

    from ui.character_conversation_runner import CharacterConversationRunner

    runtime = SimpleNamespace(chat=lambda *a, **k: {"choices": [{"message": {"content": "好的"}}]})
    runner = CharacterConversationRunner(
        runtime=runtime, learning_controller=controller
    )
    runner.perform("怎么学？", learning_result=result)
    # the contract came from the result alone; nothing raised


def test_contract_requires_a_course(loop) -> None:
    store, curricula, controller, course_id, orchestrator = loop
    result = LearningLoopResult(status="ordinary", response="")
    assert build_response_contract(result) is None
    assert build_response_contract(None) is None


def test_free_chat_passes_no_learning_result(loop, qapp) -> None:
    from ui.v2.console import CompanionConsole
    from core.learning.interactions import InteractionStore

    store, curricula, controller, course_id, orchestrator = loop
    controller.exit_mode()

    captured: dict = {}

    class R:
        agent_event = None
        session_video = None
        video_study = None
        _screen_vision_settings = None
        learning_controller = None
        asked: list[str] = []
        turn_contexts: list = []

        def ask(self, text, learning_result=None):
            self.asked.append(text)
            self.turn_contexts.append(learning_result)
            return True

    console = CompanionConsole(R())
    console.learning = controller
    R.learning_controller = controller
    console.input.setText("解释一下传递函数")
    console._send()

    assert R.asked == ["解释一下传递函数"]
    assert R.turn_contexts == [None]           # mode off → no learning result
    console.close()


# ---------------------------------------------------------------------------
# purity: the contract never calls a provider
# ---------------------------------------------------------------------------


def test_contract_module_is_provider_free() -> None:
    import ast

    path = Path(__file__).resolve().parents[1] / "core" / "learning" / "orchestrator" / "contract.py"
    forbidden = ("core.ai_router", "openai", "requests", "PySide6", "ui",
                 "core.learning.rule_engine", "core.learning.assessment")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for f in forbidden:
                assert not (node.module == f or node.module.startswith(f + ".")), (
                    f"contract imports {node.module}"
                )
