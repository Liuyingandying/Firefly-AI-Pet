# -*- coding: utf-8 -*-
"""M4.4 Tutor Session Executor 验收测试。

覆盖：完整成功流程 / 正确回答产生 evidence / 错误回答无 evidence /
空回答 / 状态转换正确（created→running→waiting_answer→completed）/
JSON 序列化 / 执行失败结构化返回, 另加阶段 3 真实闭环
（Question→Judge→Evidence→Mastery）。
隔离：真实本地仿真（fast fake runner 供多数用例, 真实 runner 一次）;
不修改 Session Planner/Agent Runtime/Rule Engine/EvidenceBridge。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from core.learning.evidence.schema import KIND_RUN
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.mastery_adapter import MasteryAdapter
from core.learning.question import QuestionGenerator
from core.learning.session.schema import LearningSessionPlan
from core.learning.tutor import (
    STATUS_COMPLETED,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_WAITING_ANSWER,
    STEP_EXPERIMENT,
    STEP_LESSON,
    STEP_QUESTION,
    TutorSessionExecutor,
    TutorSessionState,
)


def _fake_runner(payload: dict | None = None):
    """快速假实验执行器（零仿真成本, 形状与真实一致）。"""
    body = payload or {
        "ok": True,
        "status": "completed",
        "elapsed_ms": 5,
        "artifacts": {"png": "x.png", "gif": "x.gif", "markdown": "x.md"},
        "params": {"frequency_ghz": 2.4},
    }
    return lambda experiment_id: body


def _lesson_plan(target="em-uniform-plane-wave", experiment="uniform-plane-wave"):
    return LearningSessionPlan(
        session_type="lesson", target_concept=target,
        related_experiment=experiment,
        steps=("introduce_concept", "explain_prerequisite", "check_understanding"),
    )


@pytest.fixture()
def executor():
    return TutorSessionExecutor(experiment_runner=_fake_runner())


# ---------------------------------------------------------------------------
# 1. 完整成功流程（lesson → experiment → question → answer → judge → evidence）
# ---------------------------------------------------------------------------

def test_full_success_flow(executor):
    state = executor.start(_lesson_plan())
    assert state.status == STATUS_WAITING_ANSWER
    assert state.question is not None
    assert state.question.concept_id == "em-uniform-plane-wave"
    assert STEP_LESSON in state.completed_steps
    assert STEP_EXPERIMENT in state.completed_steps
    assert STEP_QUESTION in state.completed_steps
    assert state.experiment_result["ok"] is True

    final = executor.submit_answer(state.session_id, "三者相互正交")
    assert final.status == STATUS_COMPLETED
    assert final.evaluation is not None and final.evaluation.correct is True
    assert final.evidence is not None
    assert final.completed_steps[-3:] == ("answer", "judge", "evidence")
    assert "仿真实验结果" not in final.answer                 # answer 原样保存


# ---------------------------------------------------------------------------
# 2. 正确回答产生 evidence / 3. 错误回答无 evidence / 空回答
# ---------------------------------------------------------------------------

def test_correct_answer_evidence(executor):
    state = executor.start(_lesson_plan())
    final = executor.submit_answer(state.session_id, "三者相互正交")
    assert final.evidence is not None
    assert final.evidence.kind == "answer_correct"
    assert final.evidence.confidence == pytest.approx(0.8)
    assert final.evidence.concept_ids == ("em-uniform-plane-wave",)


def test_wrong_answer_no_evidence(executor):
    state = executor.start(_lesson_plan())
    final = executor.submit_answer(state.session_id, "磁场和传播方向相同")
    assert final.status == STATUS_COMPLETED
    assert final.evaluation.error_type == "concept_confusion"
    assert final.evidence is None                            # 错误不产 mastery evidence
    assert any("error_answer_no_evidence" in w for w in final.warnings)


def test_empty_answer_no_evidence(executor):
    state = executor.start(_lesson_plan())
    final = executor.submit_answer(state.session_id, "")
    assert final.status == STATUS_COMPLETED
    assert final.evaluation.error_type == "unknown"
    assert final.evidence is None
    assert any("empty_answer_no_evidence" in w for w in final.warnings)


# ---------------------------------------------------------------------------
# 4. 状态转换正确（created → running → waiting_answer → completed）
# ---------------------------------------------------------------------------

def test_status_transitions(executor):
    state = executor.start(_lesson_plan())
    assert state.status_history[0] == STATUS_CREATED
    assert state.status_history[1] == STATUS_RUNNING
    assert state.status == STATUS_WAITING_ANSWER

    final = executor.submit_answer(state.session_id, "三者相互正交")
    assert final.status_history == (
        STATUS_CREATED, STATUS_RUNNING, STATUS_WAITING_ANSWER, STATUS_COMPLETED,
    )


def test_created_state_not_externally_observable_but_history_recorded(executor):
    state = executor.start(_lesson_plan())
    # created 是瞬态（start 返回时已 running/waiting）, 但迁移史完整保留
    assert STATUS_CREATED in state.status_history


# ---------------------------------------------------------------------------
# 5. 状态机守卫 + 未知会话
# ---------------------------------------------------------------------------

def test_double_submit_rejected(executor):
    state = executor.start(_lesson_plan())
    executor.submit_answer(state.session_id, "三者相互正交")
    again = executor.submit_answer(state.session_id, "再答一次")
    assert again.status == STATUS_FAILED
    assert any("submit_answer called in status" in w for w in again.warnings)


def test_unknown_session_structured(executor):
    state = executor.submit_answer("tutor-ghost", "答案")
    assert state.status == STATUS_FAILED
    assert "unknown session" in state.warnings[0]


def test_invalid_plan_structured(executor):
    state = executor.start("not-a-plan")
    assert state.status == STATUS_FAILED
    assert state.warnings[0].startswith("invalid_input")


def test_none_type_plan_has_no_tutor_flow(executor):
    plan = LearningSessionPlan(
        session_type="none", target_concept="em-uniform-plane-wave",
        related_experiment="",
    )
    state = executor.start(plan)
    assert state.status == STATUS_FAILED
    assert any("no tutor flow" in w for w in state.warnings)


# ---------------------------------------------------------------------------
# 5b. 执行失败结构化（实验失败 / 出题失败 → failed + warnings）
# ---------------------------------------------------------------------------

def test_experiment_failure_structured(executor):
    executor._experiment_runner = _fake_runner(
        {"ok": False, "error": "experiment_id='ghost' 未注册"}
    )
    state = executor.start(_lesson_plan())
    assert state.status == STATUS_FAILED
    assert any("experiment failed" in w for w in state.warnings)
    assert any("未注册" in w for w in state.warnings)


def test_question_generation_failure_structured():
    from core.learning.knowledge_graph import KnowledgeGraph

    empty_graph_executor = TutorSessionExecutor(
        question_generator=QuestionGenerator(
            graph=KnowledgeGraph()                          # 默认种子图仍在——换空图
        ),
        experiment_runner=_fake_runner(),
    )
    empty_graph_executor._questions = QuestionGenerator(
        graph=type("G", (), {"list_concepts": lambda s: (),
                             "get_concept": lambda s, cid: None})()
    )
    state = empty_graph_executor.start(
        _lesson_plan(target="ghost-concept", experiment="uniform-plane-wave")
    )
    assert state.status == STATUS_FAILED
    assert any("question generation failed" in w for w in state.warnings)


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(executor):
    state = executor.start(_lesson_plan())
    waiting_json = json.loads(state.to_json())
    assert waiting_json["status"] == "waiting_answer"
    assert waiting_json["question"]["concept_id"] == "em-uniform-plane-wave"
    assert waiting_json["evaluation"] is None

    final = executor.submit_answer(state.session_id, "三者相互正交")
    parsed = json.loads(final.to_json())
    assert parsed["status"] == "completed"
    assert parsed["evidence"]["kind"] == "answer_correct"
    assert parsed["status_history"] == [
        "created", "running", "waiting_answer", "completed",
    ]


# ---------------------------------------------------------------------------
# 阶段 3: 真实闭环（真实仿真 runner + Question→Judge→Evidence→Mastery）
# ---------------------------------------------------------------------------

def test_phase3_real_simulation_full_chain():
    """真实 runner（~2s）+ 全链: Question → Judge → Evidence → Mastery。"""
    executor = TutorSessionExecutor()          # 默认真实 SimulationRunner
    state = executor.start(_lesson_plan())
    assert state.status == STATUS_WAITING_ANSWER
    assert state.experiment_result["status"] == "completed"

    final = executor.submit_answer(state.session_id, "三者相互正交")
    assert final.status == STATUS_COMPLETED
    assert final.evidence.confidence == pytest.approx(0.8)

    # Question → Judge → Evidence → Mastery（兼容重类型化走真实 Bridge/Adapter）
    bridge_compatible = dataclasses.replace(final.evidence, kind=KIND_RUN)
    inputs = EvidenceBridge().convert(bridge_compatible).inputs
    signals = MasteryAdapter().convert_all(inputs)
    assert len(signals) == 1
    signal = signals[0]
    assert signal.concept_id == "em-uniform-plane-wave"
    assert signal.signal_type == "basic"                      # normal_learning_event
    assert signal.strength == pytest.approx(0.8)
    assert signal.evidence_id == final.evidence.evidence_id
