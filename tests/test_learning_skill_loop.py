# -*- coding: utf-8 -*-
"""M4.7 Learning Skill Loop 验收测试。

覆盖：空状态 cold_start_probe / quiz 闭环（Question→Answer→Judge→Evidence→
Memory write）/ 错误回答 weak observation 不中断 / 多步骤 / Memory 写失败 /
最大 step 限制 / JSON 序列化。
隔离：fake store + fake memory manager; 不修改 Memory/Store/KG 核心。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.learning.skill.state_builder import LearnerStateBuilder
from core.learning.skill.loop import (
    LearningMemoryWriter,
    MultiTurnSkillLoop,
    SkillLoopResult,
    SkillLoopState,
)


class _FakeMemoryManager:
    """Fake memory manager（add_memory 记账不真写）。"""

    def __init__(self, fail=False):
        self.added = []
        self.fail = fail

    def suggest_memory(self, content, metadata=None):
        if self.fail:
            raise RuntimeError("memory backend down")
        self.added.append({"content": content, "metadata": dict(metadata or {})})
        return {"results": [{"id": f"mem-{len(self.added)}"}]}

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


class _FakeConcept:
    def __init__(self, cid, level=0):
        self.id = cid
        self.mastery_level = level
        self.last_studied_at = None


class _FakeReview:
    def __init__(self, cid, due_at="2026-09-01T00:00:00+00:00"):
        self.concept_id = cid
        self.status = "pending"
        self.due_at = due_at


# ---------------------------------------------------------------------------
# 1. 空状态 → cold_start_probe
# ---------------------------------------------------------------------------

def test_cold_start_probe():
    loop = MultiTurnSkillLoop(max_steps=5)
    result = loop.run("帮我学习电磁场")
    assert result.success is True
    assert result.state.status == "completed"
    assert result.state.step_count >= 1
    assert result.state.history[0].action == "cold_start_probe"
    assert "探底" in result.final_message or "入门" in result.final_message


# ---------------------------------------------------------------------------
# 2. 一次 quiz 闭环
# ---------------------------------------------------------------------------

def test_quiz_closed_loop():
    from core.learning.question import QuestionGenerator

    qg = QuestionGenerator()
    generated = qg.generate("em-uniform-plane-wave")
    question = generated.questions[0]
    correct_answer = question.expected_answer

    fake_mm = _FakeMemoryManager()
    writer = LearningMemoryWriter(memory_manager=fake_mm)
    loop = MultiTurnSkillLoop(
        state_builder=LearnerStateBuilder(store=_FakeStore(
            levels={'em-uniform-plane-wave': 0},
            assessments={'em-uniform-plane-wave': [SimpleNamespace(score=0.2, created_at='2026-09-01T00:00:00+00:00')]},
        )),
        memory_writer=writer, max_steps=5)

    loop._answer_provider = lambda q: correct_answer
    result = loop.run("考考我均匀平面波", max_steps=2)

    assert result.success is True
    quiz_steps = [h for h in result.state.history if h.action == "quiz"]
    assert len(quiz_steps) >= 1
    step = quiz_steps[0]
    assert step.result["correct"] is True
    assert step.result["evidence_kind"] == "answer_correct"
    assert step.result["memory_ok"] is True
    assert len(fake_mm.added) > 0


# ---------------------------------------------------------------------------
# 3. 错误回答不中断
# ---------------------------------------------------------------------------

def test_wrong_answer_no_interruption():
    from core.learning.knowledge_graph import KnowledgeGraph as KG
    from core.learning.skill.state_builder import LearnerStateBuilder

    store = _FakeStore(
        levels={"em-uniform-plane-wave": 0},
        assessments={"em-uniform-plane-wave": [
            SimpleNamespace(score=0.3, created_at="2026-09-01T00:00:00+00:00")
        ]},
    )
    fake_mm = _FakeMemoryManager()
    writer = LearningMemoryWriter(memory_manager=fake_mm)
    graph = KG()
    loop = MultiTurnSkillLoop(
        state_builder=LearnerStateBuilder(store=store, graph=graph),
        memory_writer=writer, graph=graph,
        answer_provider=lambda q: "磁场和传播方向相同",
        max_steps=5,
    )
    result = loop.run("帮我学习均匀平面波")
    assert result.success is True
    weak = [o for o in result.state.observations if "weak_point" in o]
    assert len(weak) >= 1
    assert result.state.status == "completed"


# ---------------------------------------------------------------------------
# 4. Memory 写失败不阻断
# ---------------------------------------------------------------------------

def test_memory_write_failure_loop_continues():
    failing_mm = _FakeMemoryManager(fail=True)
    writer = LearningMemoryWriter(memory_manager=failing_mm)
    loop = MultiTurnSkillLoop(memory_writer=writer, max_steps=3)
    result = loop.run("学习电磁场", max_steps=2)
    assert result.success is True
    assert any("memory write failed" in w for w in result.warnings)
    assert result.state.step_count >= 1


# ---------------------------------------------------------------------------
# 5. 最大 step 限制
# ---------------------------------------------------------------------------

def test_max_step_limit():
    loop = MultiTurnSkillLoop(max_steps=2)
    result = loop.run("无限学习", max_steps=2)
    assert result.success is True
    assert result.state.step_count <= 2
    assert result.state.status == "completed"


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization():
    fake_mm = _FakeMemoryManager()
    writer = LearningMemoryWriter(memory_manager=fake_mm)
    loop = MultiTurnSkillLoop(memory_writer=writer, max_steps=3)
    result = loop.run("帮我学习电磁场")
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert parsed["state"]["step_count"] >= 1
    assert isinstance(parsed["state"]["history"], list)

    state_json = json.loads(result.state.to_json())
    assert state_json["session_id"].startswith("skill-loop-")
    assert state_json["goal"] == "帮我学习电磁场"


# ---------------------------------------------------------------------------
# 集成: 完整链路（Question → Judge → Evidence → Memory）
# ---------------------------------------------------------------------------

def test_full_loop_question_to_memory():
    from core.learning.question import QuestionGenerator as QG
    from core.learning.judge import AnswerJudge as AJ
    from core.learning.evidence_answer import AnswerEvidenceBuilder as AEB

    fake_mm = _FakeMemoryManager()
    writer = LearningMemoryWriter(memory_manager=fake_mm)
    qg = QG()
    judge = AJ()
    builder = AEB()
    answer = "三者相互正交"

    generated = qg.generate("em-uniform-plane-wave")
    question = generated.questions[0]
    evaluation = judge.evaluate(question, answer)
    evidence_result = builder.build(question, evaluation)
    evidence = evidence_result.evidence
    memory_record = writer.write_learning_event(
        concept_id=question.concept_id,
        content=f"学习了{question.concept_id}，回答正确。",
        outcome="success",
    )

    assert evaluation.correct is True
    assert evidence is not None
    assert evidence.kind == "answer_correct"
    assert memory_record.ok is True
    assert len(fake_mm.added) == 1
