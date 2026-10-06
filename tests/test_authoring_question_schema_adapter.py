"""Question normalization between authoring draft and package validator."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if os.environ.get("FIREFLY_TEACH_MCP_ROOT"):
    sys.path.insert(0, os.environ["FIREFLY_TEACH_MCP_ROOT"])

from tools.authoring_question_schema_adapter import (  # noqa: E402
    QuestionSchemaError,
    install_package_adapter,
    normalize_question,
)


def _normalize_fixture(text):
    return text.strip().casefold()


def test_missing_correct_option_requires_unique_exact_answer():
    question = {"options": ["one", "two", "three"], "answer": "unlisted"}
    with pytest.raises(QuestionSchemaError, match="answer_option_match_missing"):
        normalize_question(question, 0, normalize=_normalize_fixture)
    question["options"] = ["one", "two", "two"]
    question["answer"] = "two"
    with pytest.raises(QuestionSchemaError, match="answer_option_match_ambiguous"):
        normalize_question(question, 0, normalize=_normalize_fixture)


def test_question_normalization_keeps_explanation_and_rejects_conflict():
    question = {
        "options": ["one", "two", "three"],
        "answer": "two",
        "explanation": "Because two is correct.",
    }
    result = normalize_question(question, 0, normalize=_normalize_fixture)
    assert result["correct_option"] == "B"
    assert result["answer"] == "two"
    assert result["explanation"] == question["explanation"]
    assert "correct_option" not in question
    with pytest.raises(QuestionSchemaError, match="correct_option_conflict"):
        normalize_question({**question, "correct_option": "A"}, 0, normalize=_normalize_fixture)
    already_valid = {**question, "answer": "B. two"}
    assert normalize_question(already_valid, 0, normalize=_normalize_fixture) == already_valid


def test_adapter_generates_package_accepted_by_unchanged_validator(tmp_path, monkeypatch):
    if os.environ.get("FIREFLY_RUN_TEACH_MCP_INTEGRATION") != "1":
        pytest.skip("explicit external teach-mcp integration opt-in required")
    builders = pytest.importorskip("knowledge_authoring.builders", reason="optional teach-mcp runtime unavailable")
    monkeypatch.setattr(builders, "DRAFT_DIR", tmp_path)
    original = builders.build_package
    install_package_adapter()
    monkeypatch.setattr(builders, "build_package", original)
    # Reinstall under monkeypatch so pytest restores the original afterwards.
    install_package_adapter()
    visual_tool = next(iter(builders.KNOWN_VISUAL_TOOLS))
    spec = {
        "concept": {
            "topic": "反馈控制", "aliases": ["闭环控制", "反馈系统"],
            "core_points": [{"id": f"p{i}", "text": f"要点 {i}"} for i in range(4)],
            "formula": "e = r - y",
            "visual": {"tool": visual_tool},
            "lesson_flow": [{"name": name} for name in
                            ("concept", "visual", "explanation", "quiz", "review")],
        },
        "misconceptions": [
            {"id": f"m{i}", "wrong": f"错误 {i}", "correct": f"正确 {i}"}
            for i in range(3)
        ],
        "questions": [
            {
                "id": f"q{i}", "difficulty": "easy" if i < 2 else "medium",
                "question": f"题目 {i}", "options": ["one", "two", "three"],
                "answer": "two", "explanation": "选项 two 符合定义。",
                "related_points": ["p0"],
            }
            for i in range(5)
        ],
        "visuals": [{"id": "v1", "tool": visual_tool, "title": "反馈图"}],
    }
    result = builders.build_package("control_system", "schema-adapter-test", spec)
    assert result["status"] == "success", result.get("errors")
    validation = builders.validate_package(result["draft_path"])
    assert validation["status"] == "success", validation.get("errors")
    import json

    questions = json.loads((Path(result["draft_path"]) / "questions.json").read_text(encoding="utf-8"))["questions"]
    assert all(q["correct_option"] == "B" and q["explanation"] for q in questions)
