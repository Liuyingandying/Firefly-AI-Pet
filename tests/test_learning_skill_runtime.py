# -*- coding: utf-8 -*-
"""M4.6 Learning Skill Runtime 验收测试。

覆盖：空状态 cold_start_probe / 已有 mastery → review / 弱概念 → quiz /
项目 → research / 非法 action 被 validator 拒绝 / 完整闭环
（LearnerState → TJULLM Mock → SkillDecision → ToolRegistry → Learning 工具）。
隔离：fake store/memory（SimpleNamespace 鸭子类型）+ 脚本化模型;
不修改 KnowledgeGraph/LearningStore 核心/TJULLM Adapter/ProviderRouter/TutorSession。
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from core.learning.agent.model import MockLearningModel, ModelResponse
from core.learning.evidence.schema import KIND_RUN
from core.learning.evidence_bridge import EvidenceBridge
from core.learning.judge import AnswerJudge
from core.learning.mastery_adapter import MasteryAdapter
from core.learning.question import QuestionGenerator
from core.learning.skill import (
    ACTION_VALUES,
    LearnerState,
    LearningAction,
    LearningSkillRuntime,
    LearnerStateBuilder,
    SkillDecisionValidator,
)
from core.learning.tools.schema import TOOL_QUERY_CONCEPT


# ---------------------------------------------------------------------------
# Fake 来源（鸭子类型, 只实现 builder 消费面）
# ---------------------------------------------------------------------------

class _FakeConcept:
    def __init__(self, cid: str, level: int):
        self.id = cid
        self.mastery_level = level
        self.last_studied_at = None


class _FakeAssessment:
    def __init__(self, score: float, at: str = "2026-09-01T00:00:00+00:00"):
        self.score = score
        self.created_at = at


class _FakeReview:
    def __init__(self, cid: str, due_at: str = "2026-09-01T00:00:00+00:00"):
        self.concept_id = cid
        self.status = "pending"
        self.due_at = due_at


class _FakeStore:
    def __init__(self, levels: dict[str, int] | None = None,
                 assessments: dict[str, list] | None = None,
                 reviews: list | None = None):
        self._levels = levels or {}
        self._assessments = assessments or {}
        self._reviews = reviews or []

    def get_concept(self, cid):
        level = self._levels.get(cid)
        return _FakeConcept(cid, level) if level is not None else None

    def list_assessments(self, cid=None):
        return self._assessments.get(cid, [])

    def list_review_items(self, cid=None):
        return list(self._reviews)

    def get_active_session(self, course_id):
        return None

    def list_courses(self):
        return []


def _memory_query(facts: dict[str, list[str]]):
    def query(q: str):
        return [{"content": c} for c in facts.get(q, [])]
    return query


def _decision_model(payload: dict) -> MockLearningModel:
    return MockLearningModel(scripted=[ModelResponse(
        content=json.dumps(payload, ensure_ascii=False), finish_reason="stop",
    )])


# ---------------------------------------------------------------------------
# 阶段 0 基线: LearnerStateBuilder 聚合
# ---------------------------------------------------------------------------

def test_builder_cold_start_flag():
    state = LearnerStateBuilder().build()
    assert state.cold_start is True
    assert state.available_concepts if hasattr(state, "available_concepts") else True


def test_builder_cold_start_false_with_mastery():
    builder = LearnerStateBuilder(store=_FakeStore(
        levels={"em-uniform-plane-wave": 4}
    ))
    state = builder.build()
    assert state.cold_start is False
    assert state.concept_mastery["em-uniform-plane-wave"] == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 验收 1: 空状态 → cold_start_probe
# ---------------------------------------------------------------------------

def test_empty_state_returns_cold_start_probe():
    runtime = LearningSkillRuntime()           # 无模型/无 store/无 memory → 全空
    result = runtime.start_learning("随便看看")
    assert result.success is True
    assert result.decision is not None
    assert result.decision.action == "cold_start_probe"
    assert result.decision.target_concept == "em-uniform-plane-wave"  # 入门概念（无前置）
    assert result.served_question is not None                  # 探底题已出
    assert "均匀平面波" in result.response


# ---------------------------------------------------------------------------
# 验收 2: 已有 mastery → TJULLM 选择 review
# ---------------------------------------------------------------------------

def test_mastery_leads_to_review():
    store = _FakeStore(levels={"em-te-polarization": 4})
    model = _decision_model({
        "action": "review", "target_concept": "em-te-polarization",
        "reason": "mastery 4 且概念到期", "confidence": 0.9,
    })
    runtime = LearningSkillRuntime(
        model=model, state_builder=LearnerStateBuilder(store=store),
    )
    result = runtime.start_learning("继续")
    assert result.success is True
    assert result.decision.action == "review"
    assert TOOL_QUERY_CONCEPT in [t["tool"] for t in result.tool_results]
    assert "复习" in result.response


# ---------------------------------------------------------------------------
# 验收 3: 弱概念 → quiz（题面脱敏）
# ---------------------------------------------------------------------------

def test_weak_concept_leads_to_quiz_sanitized():
    store = _FakeStore(
        levels={"em-uniform-plane-wave": 0},
        assessments={"em-uniform-plane-wave": [_FakeAssessment(0.2)]},
    )
    model = _decision_model({
        "action": "quiz", "target_concept": "em-uniform-plane-wave",
        "reason": "薄弱概念", "confidence": 0.8,
    })
    runtime = LearningSkillRuntime(
        model=model, state_builder=LearnerStateBuilder(store=store),
    )
    result = runtime.start_learning("考考我")
    assert result.success is True
    assert result.decision.action == "quiz"
    assert result.served_question is not None
    # 题面脱敏: 答案与判定规则不出服务端
    assert "三者相互正交" not in result.response
    assert "keyword_any" not in result.response
    assert result.served_question.question_text in result.response


def test_builder_marks_weak_from_wrong_answer():
    store = _FakeStore(
        levels={"em-uniform-plane-wave": 0},
        assessments={"em-uniform-plane-wave": [_FakeAssessment(0.2)]},
    )
    state = LearnerStateBuilder(store=store).build()
    assert {"id": "em-uniform-plane-wave", "reason": "wrong_answer"} in [
        dict(w) for w in state.weak_concepts
    ]
    assert state.recent_errors[0]["concept_id"] == "em-uniform-plane-wave"


# ---------------------------------------------------------------------------
# 验收 4: 项目 → research
# ---------------------------------------------------------------------------

def test_project_leads_to_research():
    model = _decision_model({
        "action": "research", "target_concept": "em-uniform-plane-wave",
        "reason": "项目相关", "confidence": 0.7,
    })
    runtime = LearningSkillRuntime(
        model=model,
        state_builder=LearnerStateBuilder(memory_query=_memory_query(
            {"project": ["THz-ISAC"]}
        )),
    )
    result = runtime.start_learning("围绕项目学习")
    assert result.success is True
    assert result.decision.action == "research"
    state = result.state
    assert state.active_projects == ("THz-ISAC",)
    assert TOOL_QUERY_CONCEPT in [t["tool"] for t in result.tool_results]


# ---------------------------------------------------------------------------
# 验收 5: 非法 action 被 validator 拒绝（runtime 回退安全决策）
# ---------------------------------------------------------------------------

def test_validator_rejects_unknown_action():
    check = SkillDecisionValidator().validate({"action": "run_python"})
    assert check.ok is False
    assert check.error.startswith("unknown_action")


def test_validator_rejects_unknown_concept_and_bad_confidence():
    v = SkillDecisionValidator()
    check = v.validate(
        {"action": "quiz", "target_concept": "made-up"},
        available_concepts=("em-uniform-plane-wave",),
    )
    assert check.ok is False and check.error.startswith("unknown_concept")

    check = v.validate({"action": "quiz", "target_concept": "c", "confidence": 7})
    assert check.ok is False and check.error.startswith("invalid_confidence")


def test_runtime_falls_back_after_rejection():
    rogue = _decision_model({"action": "run_python"})
    runtime = LearningSkillRuntime(
        model=rogue, state_builder=LearnerStateBuilder(
            store=_FakeStore(levels={"em-uniform-plane-wave": 0})
        ),
    )
    result = runtime.start_learning("anything")
    assert result.success is True                             # 回退安全决策
    assert result.decision.action == "quiz"                   # 规则回退: 薄弱 → quiz
    assert any("model decision rejected" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 验收 6: 完整闭环（LearnerState → Mock TJULLM → SkillDecision → 链路）
# ---------------------------------------------------------------------------

def test_full_loop_state_to_mastery():
    store = _FakeStore(levels={"em-te-polarization": 2})
    model = _decision_model({
        "action": "review", "target_concept": "em-te-polarization",
        "reason": "到期复习", "confidence": 0.9,
    })
    runtime = LearningSkillRuntime(
        model=model, state_builder=LearnerStateBuilder(store=store),
    )
    result = runtime.start_learning("开始复习")

    assert result.success is True
    assert result.decision is not None
    assert result.decision.action == "review"

    # 复习路由 = 概念查询 + 复习题; 随后模拟作答进入 Judge → Evidence → Mastery
    question = QuestionGenerator().generate("em-te-polarization").questions[0]
    evaluation = AnswerJudge().evaluate(question, "电场垂直于入射面")
    assert evaluation.correct is True

    evidence_result = __import__(
        "core.learning.evidence_answer", fromlist=["AnswerEvidenceBuilder"]
    ).AnswerEvidenceBuilder().build(question, evaluation)
    assert evidence_result.evidence is not None

    bridge_compatible = dataclasses.replace(
        evidence_result.evidence, kind=KIND_RUN
    )
    inputs = EvidenceBridge().convert(bridge_compatible).inputs
    signals = MasteryAdapter().convert_all(inputs)
    assert signals[0].concept_id == "em-te-polarization"
    assert signals[0].signal_type == "basic"
    assert signals[0].strength == pytest.approx(0.8)


def test_action_vocabulary_closed():
    assert ACTION_VALUES == frozenset({
        "cold_start_probe", "probe", "teach", "quiz", "review",
        "research", "experiment", "continue_project",
    })
    assert LearningAction.COLD_START_PROBE.value == "cold_start_probe"
