# -*- coding: utf-8 -*-
"""M4.10 Learner State Gate + Skill Prompt 优化 验收测试。

覆盖：
Case1 空用户 → probe
Case2 已有 mastery → teach/review
Case3 已有错误 → quiz
Gate 安全校验 / 模型失败回退 / JSON 序列化。
隔离：fake store + scripted model; 不修改 Tutor/Judge/Evidence/TJULLM Adapter。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.learning.skill.loop import MultiTurnSkillLoop
from core.learning.skill.schema import LearnerState
from core.learning.skill.brain import TJULLMLearningBrain


class _FakeConcept:
    def __init__(self, cid, level=0):
        self.id = cid
        self.mastery_level = level
        self.last_studied_at = None


class _FakeStore:
    def __init__(self, levels=None, assessments=None, reviews=None):
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


class _FakeMemoryManager:
    def __init__(self, fail=False):
        self.added = []
        self.fail = fail

    def add_memory(self, content, metadata=None):
        if self.fail:
            raise RuntimeError("memory down")
        self.added.append({"content": content, "metadata": dict(metadata or {})})
        return {"results": [{"id": f"mem-{len(self.added)}"}]}


def _decision_model(payload: dict):
    from core.learning.agent.model import MockLearningModel, ModelResponse

    return MockLearningModel(scripted=[ModelResponse(
        content=json.dumps(payload, ensure_ascii=False), finish_reason="stop",
    )])


# ---------------------------------------------------------------------------
# Case1: 空用户 → probe（gate 覆盖 brain）
# ---------------------------------------------------------------------------

def test_empty_user_gate_forces_probe():
    """冷启动用户即使 brain 输出 teach，gate 也覆盖为 probe。"""
    brain_model = _decision_model({"action": "teach", "target_concept": "em-uniform-plane-wave",
                                   "reason": "开始教学", "confidence": 0.9})
    brain = TJULLMLearningBrain(model=brain_model)
    loop = MultiTurnSkillLoop(brain=brain, max_steps=5)
    result = loop.run("学习均匀平面波")

    assert result.success is True
    assert result.state.decision is not None
    # gate 覆盖 teach → probe
    assert result.state.decision.action == "probe"
    assert "cold_start" in result.state.decision.reason
    assert result.state.history[-1].action in ('probe', 'quiz', 'cold_start_probe')


# ---------------------------------------------------------------------------
# Case2: 已有 mastery → teach/review
# ---------------------------------------------------------------------------

def test_mastery_leads_to_teach():
    """mastery 0.5 → teach。"""
    store = _FakeStore(levels={"em-uniform-plane-wave": 3})  # 3/5 = 0.6 → 非弱非冷
    brain_model = _decision_model({"action": "teach",
                                   "target_concept": "em-uniform-plane-wave",
                                   "reason": "继续学习", "confidence": 0.7})
    brain = TJULLMLearningBrain(model=brain_model)
    loop = MultiTurnSkillLoop(brain=brain, state_builder=__import__(
        "core.learning.skill.state_builder", fromlist=["LearnerStateBuilder"]
    ).LearnerStateBuilder(store=store), max_steps=5)
    result = loop.run("继续学习均匀平面波")

    assert result.success is True
    assert result.state.decision.action == "teach"


# ---------------------------------------------------------------------------
# Case3: 已有错误 → quiz
# ---------------------------------------------------------------------------

def test_weak_concept_leads_to_quiz():
    """mastery 0 + 答错记录 → quiz。"""
    store = _FakeStore(
        levels={"em-uniform-plane-wave": 0},
        assessments={"em-uniform-plane-wave": [
            SimpleNamespace(score=0.2, created_at="2026-09-01T00:00:00+00:00")
        ]},
    )
    brain_model = _decision_model({"action": "quiz",
                                   "target_concept": "em-uniform-plane-wave",
                                   "reason": "薄弱", "confidence": 0.8})
    brain = TJULLMLearningBrain(model=brain_model)
    builder = __import__(
        "core.learning.skill.state_builder", fromlist=["LearnerStateBuilder"]
    ).LearnerStateBuilder(store=store)
    loop = MultiTurnSkillLoop(brain=brain, state_builder=builder, max_steps=5)
    result = loop.run("考考我均匀平面波")

    assert result.success is True
    assert result.state.decision.action == "quiz"
    assert result.state.history[-1].action in ('probe', 'quiz', 'cold_start_probe')


# ---------------------------------------------------------------------------
# Gate 安全校验（brain 输出违规 → gate 覆盖）
# ---------------------------------------------------------------------------

def test_gate_overrides_cold_start_teach():
    """冷启动 brain 输出 teach → gate 覆盖为 probe。"""
    brain_model = _decision_model({"action": "teach",
                                   "target_concept": "em-uniform-plane-wave",
                                   "reason": "直接教学", "confidence": 0.9})
    brain = TJULLMLearningBrain(model=brain_model)
    loop = MultiTurnSkillLoop(brain=brain, max_steps=5)
    result = loop.run("学习电磁场")

    assert result.success is True
    assert result.state.decision.action == "probe"
    assert "[gate override" in result.state.decision.reason or "cold_start" in result.state.decision.reason


# ---------------------------------------------------------------------------
# 模型失败自动回退规则
# ---------------------------------------------------------------------------

class _ExplodingModel:
    name = "exploding"
    def generate(self, request):
        raise RuntimeError("TJULLM down")


def test_model_failure_falls_back_to_rules():
    brain = TJULLMLearningBrain(model=_ExplodingModel())
    loop = MultiTurnSkillLoop(brain=brain, max_steps=5)
    result = loop.run("学习均匀平面波")

    assert result.success is True
    assert result.state.decision is not None
    assert "brain degraded" in result.state.decision.reason or "cold_start" in result.state.decision.reason


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization():
    store = _FakeStore(levels={"em-uniform-plane-wave": 3})
    brain_model = _decision_model({"action": "quiz",
                                   "target_concept": "em-uniform-plane-wave",
                                   "reason": "薄弱", "confidence": 0.8})
    brain = TJULLMLearningBrain(model=brain_model)
    sb = __import__(
        "core.learning.skill.state_builder", fromlist=["LearnerStateBuilder"]
    ).LearnerStateBuilder(store=store)
    loop = MultiTurnSkillLoop(brain=brain, state_builder=sb, max_steps=3)
    result = loop.run("考考我均匀平面波", max_steps=3)

    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert parsed["state"]["decision"]["action"] == "quiz"
    assert parsed["state"]["step_count"] >= 1
