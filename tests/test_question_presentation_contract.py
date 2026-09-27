"""Question Presentation contract tests (Firefly side, Phase 12).

Locks the skill-side protocol: interactive renders stem+options (gate),
embedded keeps result.json; question_not_presented repairs are recorded.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

SKILL = (PROJECT_DIR / "learning" / "skill_source" / "SKILL.md").read_text(
    encoding="utf-8"
)


def test_6_7_interactive_embedded_delivery_split():
    assert "不写 bridge/result.json" in SKILL or "不写 result.json" in SKILL
    assert "delivery_mode" in SKILL
    assert "embedded" in SKILL and "interactive" in SKILL
    # both modes documented as mutually exclusive
    assert "不得同时执行" in SKILL


def test_8_9_question_presented_gate_rules():
    assert "QUESTION_PRESENTED" in SKILL
    assert "QUESTION_PRESENTATION_INCOMPLETE" in SKILL
    assert "禁止对用户任何输入调用 evaluate_answer / record_result" in SKILL


def test_needs_review_never_enters_teaching_contract():
    assert "needs_review" in SKILL
    assert "禁止自行出题兜底" in SKILL


def test_question_not_presented_repair_recorded():
    con = sqlite3.connect(
        r"file:E:/Firefly_AI_MCP/teach_mcp/state/session.db?mode=ro", uri=True
    )
    try:
        rows = con.execute(
            "SELECT question_id FROM result_repairs WHERE repair_reason = 'question_not_presented'"
        ).fetchall()
    finally:
        con.close()
    assert rows, "盲答撤销审计缺失"
    assert {r[0] for r in rows} == {"RC-E01"}


def test_choice_judge_bug_repairs_present():
    con = sqlite3.connect(
        r"file:E:/Firefly_AI_MCP/teach_mcp/state/session.db?mode=ro", uri=True
    )
    try:
        rows = con.execute(
            "SELECT question_id FROM result_repairs WHERE repair_reason = 'choice_judge_bug'"
        ).fetchall()
    finally:
        con.close()
    assert {r[0] for r in rows} == {"THZ-Q01", "THZ-Q02"}
