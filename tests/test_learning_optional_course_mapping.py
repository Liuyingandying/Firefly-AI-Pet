"""Public synthetic course fixtures; no imported textbook or production state."""

from __future__ import annotations

import hashlib
import json

import pytest

from core.learning.skill.v2.catalog import curriculum_evidence
from core.learning.skill.v2.session import LearningSession
from learning.orchestrator import curriculum as loader


def _mapping(root):
    path = root / "learning/courses/control_theory/experience.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _synthetic_course(root):
    """Two tiny arithmetic questions exercise references, not private content."""
    folder = root / "public_fixture"
    folder.mkdir()
    ref = {"unit_id": "demo-unit", "topic_id": "demo-topic",
           "concept_id": "demo_control:state:concept", "lesson_id": "demo-lesson",
           "quiz_id": "demo-quiz"}
    questions = [{"id": f"demo-q-{i}", "question": f"{i}+1 等于多少？",
                  "options": [str(i + 1), "0"], "correct_option": "A",
                  "answer": str(i + 1), "explanation": "按整数加法计算。"}
                 for i in (1, 2)]
    course = {"course_id": "demo_control", "title": "合成课程", "registered": False}
    unit = {"unit_id": ref["unit_id"], "topic_id": ref["topic_id"], "order": 1,
            "concepts": [{"id": ref["concept_id"], "title": "状态空间演示",
                          "core_points": [{"text": "仅为引用完整性测试。"}]}],
            "lessons": [{"id": ref["lesson_id"], "concept_id": ref["concept_id"]}],
            "quizzes": [{"id": ref["quiz_id"], "concept_id": ref["concept_id"],
                         "questions": questions}]}
    v2 = folder / "control_theory_curriculum_draft_v2.json"
    v2.write_text(json.dumps({"course": course, "chapters": [{"units": [unit]}]}), encoding="utf-8")
    final = folder / "final.json"
    final.write_text(json.dumps({"course": course,
        "source_v2_sha256": hashlib.sha256(v2.read_bytes()).hexdigest(),
        "topics": [ref], "review_units": [],
        "chapters": [{"chapter_id": "demo-chapter", "title": "合成章", "order": 1,
                      "sections": [ref], "exercises": []}]}), encoding="utf-8")
    _mapping(root).write_text(json.dumps({"runtime_course_id": "demo-runtime",
        "curriculum_draft": final.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(final.read_bytes()).hexdigest()}), encoding="utf-8")
    return final


def test_absent_optional_mapping_returns_none_without_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    assert loader.configured_draft("any-course") is None
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("topic", ["state", "feedback", "step", "root", "bode"])
def test_unconfigured_navigation_has_no_formal_course_facts(tmp_path, monkeypatch, topic):
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    evidence = curriculum_evidence(topic)
    assert evidence["mode"] == "unconfigured"
    assert evidence["course_id"] is None and evidence["refs"] == []
    session = LearningSession(tmp_path / "local-navigation.json")
    session.start(topic, "合成目标")
    assert session.state["curriculum"] == evidence
    assert "mastery" not in session.state


def test_configured_public_course_preserves_pinned_reference_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    final = _synthetic_course(tmp_path)
    assert loader.configured_draft("unrelated") is None
    assert loader.configured_draft("demo-runtime") == final
    draft = loader.load_curriculum(final)
    assert draft.course_id == "demo_control" and len(draft.lessons) == 1
    evidence = curriculum_evidence("state")
    assert evidence["mode"] == "curriculum_preview"
    assert evidence["course_id"] == "demo_control"
    assert evidence["refs"][0]["concept_id"] == "demo_control:state:concept"
    assert evidence["review_units"] == []


def test_configured_hash_mismatch_still_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    final = _synthetic_course(tmp_path)
    final.write_text("changed", encoding="utf-8")
    with pytest.raises(loader.CurriculumError):
        loader.configured_draft("demo-runtime")
    with pytest.raises(loader.CurriculumError):
        curriculum_evidence("state")


def test_configured_missing_draft_is_not_treated_as_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    _mapping(tmp_path).write_text(json.dumps({"runtime_course_id": "demo-runtime",
        "curriculum_draft": "missing.json", "sha256": "missing"}), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        loader.configured_draft("demo-runtime")


def test_malformed_existing_mapping_still_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(loader, "ROOT", tmp_path)
    _mapping(tmp_path).write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        loader.configured_draft("any-course")
