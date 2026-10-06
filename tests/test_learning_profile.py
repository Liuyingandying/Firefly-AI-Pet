# -*- coding: utf-8 -*-
"""M4.9 Learner Profile + Memory Consolidation 验收测试。

覆盖：空用户生成 profile / mastery 正确聚合 / memory learning_event 读取 /
错误模式聚合 / 项目上下文读取 / Brain 收到 profile / 完整链。
隔离：fake store + fake memory query; 不修改 Memory/Store/KG 核心。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core.learning.profile import (
    LearnerProfile,
    LearnerProfileBuilder,
    LearningMemoryConsolidator,
)
from core.learning.knowledge_graph import KnowledgeGraph
from core.learning.skill.state_builder import LearnerStateBuilder
from core.learning.skill.brain import TJULLMLearningBrain
from core.learning.skill.loop import MultiTurnSkillLoop
from core.learning.skill.loop.memory_writer import LearningMemoryWriter


class _FakeConcept:
    def __init__(self, cid, level=0, name=""):
        self.id = cid
        self.mastery_level = level
        self.name = name or cid
        self.last_studied_at = None


class _FakeAssessment:
    def __init__(self, score, at="2026-09-01T00:00:00+00:00"):
        self.score = score
        self.created_at = at


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
    def __init__(self, facts=None):
        self.facts = facts or {}
        self.added = []

    def search_memory(self, query):
        return [{"memory": c, "metadata": {}} for c in self.facts.get(query, [])]

    def add_memory(self, content, metadata=None):
        self.added.append({"content": content, "metadata": dict(metadata or {})})
        return {"results": [{"id": f"mem-{len(self.added)}"}]}


# ---------------------------------------------------------------------------
# 1. 空用户生成 profile
# ---------------------------------------------------------------------------

def test_empty_user_profile():
    builder = LearnerProfileBuilder()
    profile = builder.build()
    assert isinstance(profile, LearnerProfile)
    assert profile.knowledge.concept_mastery == {}
    assert profile.knowledge.strong_concepts == ()
    assert profile.learning_behavior.common_error_patterns == ()
    assert profile.activity.review_due == ()


# ---------------------------------------------------------------------------
# 2. mastery 正确聚合
# ---------------------------------------------------------------------------

def test_mastery_aggregation():
    store = _FakeStore(levels={
        "em-uniform-plane-wave": 4,     # 4/5 = 0.8 → strong
        "em-te-polarization": 1,        # 1/5 = 0.2 → weak
        "em-tm-polarization": 2,        # 3/5 = 0.6 → neither
    })
    builder = LearnerProfileBuilder(store=store)
    profile = builder.build()
    assert profile.knowledge.concept_mastery["em-uniform-plane-wave"] == pytest.approx(0.8)
    assert profile.knowledge.concept_mastery["em-te-polarization"] == pytest.approx(0.2)
    assert "em-uniform-plane-wave" in profile.knowledge.strong_concepts
    assert "em-te-polarization" in profile.knowledge.weak_concepts
    assert "em-tm-polarization" not in profile.knowledge.strong_concepts
    assert "em-tm-polarization" not in profile.knowledge.weak_concepts


# ---------------------------------------------------------------------------
# 3. memory learning_event 读取
# ---------------------------------------------------------------------------

def test_memory_learning_event():
    fake_mm = _FakeMemoryManager(facts={
        "learning_event": ["用户学习了均匀平面波TE/TM模式，回答正确，当前理解状态提升。"],
        "preference": ["偏好实践/操作"],
        "project": ["THz-ISAC"],
    })
    builder = LearnerProfileBuilder(memory_query=fake_mm.search_memory)
    profile = builder.build()
    assert len(profile.activity.recent_learning_events) == 1
    assert "均匀平面波" in profile.activity.recent_learning_events[0]
    assert "偏好实践/操作" in profile.learning_behavior.learning_preferences
    assert "THz-ISAC" in profile.projects.active_projects


# ---------------------------------------------------------------------------
# 4. 错误模式聚合
# ---------------------------------------------------------------------------

def test_error_pattern_consolidation():
    events = [
        {"concept_id": "em-te-polarization", "error_type": "concept_confusion",
         "at": "2026-09-01T00:00:00+00:00"},
        {"concept_id": "em-te-polarization", "error_type": "concept_confusion",
         "at": "2026-09-02T00:00:00+00:00"},
        {"concept_id": "em-uniform-plane-wave", "error_type": "wrong_formula",
         "at": "2026-09-03T00:00:00+00:00"},
    ]
    weaknesses = LearningMemoryConsolidator.consolidate(events)
    assert len(weaknesses) == 2
    by_pattern = {w.pattern: w for w in weaknesses}
    assert any("概念关系理解不足" in p for p in by_pattern)
    confusion = next(w for p, w in by_pattern.items() if "概念关系" in p)
    assert confusion.occurrences == 2
    assert confusion.concept_id == "em-te-polarization"
    formula = next(w for p, w in by_pattern.items() if "公式" in p)
    assert formula.occurrences == 1


# ---------------------------------------------------------------------------
# 5. 项目上下文读取
# ---------------------------------------------------------------------------

def test_project_context():
    fake_mm = _FakeMemoryManager(facts={"project": ["THz-ISAC", "PageLens"]})
    builder = LearnerProfileBuilder(memory_query=fake_mm.search_memory)
    profile = builder.build()
    assert "THz-ISAC" in profile.projects.active_projects
    assert "PageLens" in profile.projects.active_projects


# ---------------------------------------------------------------------------
# 6. Brain 收到 profile
# ---------------------------------------------------------------------------

def test_brain_receives_profile():
    class _CapturingModel:
        name = "capturing"
        def __init__(self):
            self.requests = []
        def generate(self, request):
            self.requests.append(request)
            return SimpleNamespace(content='{"action":"quiz","target_concept":"em-te-polarization","reason":"薄弱","confidence":0.8}')

    store = _FakeStore(levels={"em-te-polarization": 1})
    fake_mm = _FakeMemoryManager(facts={
        "preference": ["偏好实践/操作"],
        "project": ["THz-ISAC"],
    })
    builder = LearnerProfileBuilder(store=store, memory_query=fake_mm.search_memory)
    profile = builder.build()

    capturing = _CapturingModel()
    brain = TJULLMLearningBrain(model=capturing, graph=KnowledgeGraph())
    brain.decide(
        __import__("core.learning.skill.schema", fromlist=["LearnerState"]).LearnerState(),
        "继续学习",
        profile=profile,
    )
    assert len(capturing.requests) == 1
    system_content = capturing.requests[0].messages[0]["content"]
    assert "Learner Profile" in system_content
    assert "薄弱概念" in system_content
    assert "THz-ISAC" in system_content


# ---------------------------------------------------------------------------
# 7. 完整链：Memory → Profile → Brain → SkillDecision → SkillLoop
# ---------------------------------------------------------------------------

def test_full_chain_memory_to_skill_loop():
    store = _FakeStore(
        levels={"em-uniform-plane-wave": 0},
        assessments={"em-uniform-plane-wave": [
            SimpleNamespace(score=0.2, created_at="2026-09-01T00:00:00+00:00")
        ]},
    )
    fake_mm = _FakeMemoryManager(facts={"preference": ["偏好实践/操作"]})
    builder = LearnerProfileBuilder(store=store, memory_query=fake_mm.search_memory)
    profile = builder.build()

    from core.learning.skill.schema import LearnerState
    from core.learning.agent.model import MockLearningModel, ModelResponse

    learner_state = LearnerState(cold_start=False)
    script = ModelResponse(
        content=json.dumps({"action": "quiz", "target_concept": "em-uniform-plane-wave",
                            "reason": "薄弱概念", "confidence": 0.8}),
        finish_reason="stop",
    )
    brain = TJULLMLearningBrain(
        model=MockLearningModel(scripted=[script]),
        graph=KnowledgeGraph(),
    )
    decision, done = brain.decide(learner_state, "继续学习", profile=profile)
    assert decision.action == "quiz"
    assert decision.target_concept == "em-uniform-plane-wave"

    fake_mm = _FakeMemoryManager()
    writer = LearningMemoryWriter(memory_manager=fake_mm)
    loop = MultiTurnSkillLoop(
        state_builder=LearnerStateBuilder(store=store, graph=KnowledgeGraph()),
        memory_writer=writer, graph=KnowledgeGraph(),
        answer_provider=lambda q: "电场垂直于入射面",
        max_steps=5,
    )
    result = loop.run("继续学习")
    assert result.success is True
    assert result.state.step_count >= 1


class _StubBrain:
    """脚本化 Brain mock（完整闭环用）。"""
    def __init__(self, action, target):
        self._action = action
        self._target = target
    def decide(self, learner_state, goal, **kw):
        return (
            SimpleNamespace(action=self._action, target_concept=self._target,
                            reason="stub", confidence=0.8, metadata={}),
            False,
        )


# ---------------------------------------------------------------------------
# 额外: JSON 序列化
# ---------------------------------------------------------------------------

def test_profile_json():
    from core.learning.profile.schema import (
        KnowledgeSnapshot, LearningBehavior, ActivitySnapshot, ProjectSnapshot,
    )
    profile = LearnerProfile(
        user_id="u1",
        knowledge=KnowledgeSnapshot(concept_mastery={"c1": 0.8}, strong_concepts=("c1",)),
        learning_behavior=LearningBehavior(learning_preferences=("实践",)),
        activity=ActivitySnapshot(review_due=("c2",)),
        projects=ProjectSnapshot(active_projects=("THz-ISAC",)),
        metadata={"generated_at": "2026-09-24T00:00:00+00:00"},
    )
    text = json.dumps(profile.to_dict(), ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["user_id"] == "u1"
    assert parsed["knowledge"]["strong_concepts"] == ["c1"]
