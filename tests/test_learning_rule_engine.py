"""Deterministic Learning Rule Engine tests (Phase 1C).

Locks the frozen Phase 0.5 semantics:
- score bands (pass/partial/fail) and difficulty caps
- single-step promotion (+1 max), 3->4 variant gate (>=24h span)
- 4->5 long-term gate (>=2 applications + >=3 perfect reviews + >=14 days)
- 24h new-concept protection
- demotion only on >=2 consecutive high-difficulty fails (max -1)
- self-assessment never promotes; user override is audited and validated
- retention updates; time never changes mastery
- low-confidence evidence is stored but never moves mastery
- exactly one AssessmentRecord per apply; explainable reasons
"""

from __future__ import annotations

import inspect
import math

import pytest

from core.learning import (
    AssessmentEvidence,
    LearningStore,
    Retention,
)
from core.learning.rule_engine import (
    LearningRuleEngine,
    classify_score,
    difficulty_cap,
)
from core.learning.review_scheduler import base_review_interval


@pytest.fixture()
def store(tmp_path) -> LearningStore:
    s = LearningStore(tmp_path / "learning.sqlite3")
    s.initialize()
    return s


@pytest.fixture()
def engine(store) -> LearningRuleEngine:
    return LearningRuleEngine(store)


T0 = "2026-09-01T00:00:00+00:00"


def _day(n: int, hour: int = 0) -> str:
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return (base + timedelta(days=n, hours=hour)).isoformat()


def _make_concept(store: LearningStore, name: str = "根轨迹") -> tuple[str, str]:
    course = store.create_course("自动控制原理")
    concept = store.add_concept(course.id, name, created_at=T0)
    return course.id, concept.id


def _ev(concept_id: str, **kwargs) -> AssessmentEvidence:
    defaults = dict(source="quiz", score=1.0, difficulty="basic", confidence=1.0)
    defaults.update(kwargs)
    return AssessmentEvidence(concept_id=concept_id, **defaults)

def _climb_to(store: LearningStore, engine: LearningRuleEngine,
              concept_id: str, target: int) -> None:
    """Deterministic climb to a target mastery (<=3) via daily basic passes."""
    for level in range(target):
        engine.apply_assessment(
            _ev(concept_id, score=1.0, difficulty="basic", created_at=_day(level)),
            now=_day(level),
        )


def _reach_4(store: LearningStore, engine: LearningRuleEngine, concept_id: str) -> None:
    _climb_to(store, engine, concept_id, 3)
    # Two variant passes spanning >=24h.
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(3)), now=_day(3))
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(4, 1)), now=_day(4, 1))
    assert store.get_concept(concept_id).mastery_level == 4


# ---------------------------------------------------------------------------
# pure functions
# ---------------------------------------------------------------------------


def test_score_classification_boundaries() -> None:
    assert classify_score(0.0) == "fail"
    assert classify_score(0.59) == "fail"
    assert classify_score(0.60) == "partial"
    assert classify_score(0.79) == "partial"
    assert classify_score(0.80) == "pass"
    assert classify_score(1.0) == "pass"


def test_difficulty_caps() -> None:
    assert difficulty_cap("recall") == 2
    assert difficulty_cap("basic") == 3
    assert difficulty_cap("variant") == 4
    assert difficulty_cap("applied") == 5


# ---------------------------------------------------------------------------
# promotion
# ---------------------------------------------------------------------------


def test_first_pass_promotes_0_to_1(store, engine) -> None:
    _, concept_id = _make_concept(store)
    decision = engine.apply_assessment(
        _ev(concept_id, score=1.0, difficulty="basic", created_at=_day(0)),
        now=_day(0))
    assert (decision.old_mastery, decision.new_mastery) == (0, 1)
    assert decision.reason == "first_assessment_pass"
    assert decision.changed and decision.eligible
    assert store.get_concept(concept_id).state == "learning"


def test_basic_pass_climbs_1_to_2_and_2_to_3(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 1)
    d2 = engine.apply_assessment(
        _ev(concept_id, difficulty="basic", created_at=_day(1)), now=_day(1))
    assert (d2.old_mastery, d2.new_mastery) == (1, 2)
    d3 = engine.apply_assessment(
        _ev(concept_id, difficulty="basic", created_at=_day(2)), now=_day(2))
    assert (d3.old_mastery, d3.new_mastery) == (2, 3)


def test_basic_cannot_promote_3_to_4(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 3)
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="basic", score=1.0, created_at=_day(5)),
        now=_day(5))
    assert decision.new_mastery == 3
    assert decision.reason == "difficulty_cap"


def test_variant_passes_promote_3_to_4_with_24h_span(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 3)
    first = engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(3)), now=_day(3))
    assert first.new_mastery == 3  # only one variant evidence so far
    second = engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(4, 1)),
        now=_day(4, 1))
    assert (second.old_mastery, second.new_mastery) == (3, 4)
    assert second.reason == "variant_evidence_sufficient"
    assert store.get_concept(concept_id).state == "mastered"


def test_single_evidence_never_jumps_two_levels(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 2)
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="variant", score=1.0, created_at=_day(5)),
        now=_day(5))
    assert decision.new_mastery - decision.old_mastery <= 1


def test_variant_passes_same_day_do_not_promote(store, engine) -> None:
    """Same-minute grinding (span <24h, same question type) never promotes."""
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 3)
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(3)), now=_day(3))
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(3, 1)),
        now=_day(3, 1))
    assert decision.new_mastery == 3
    assert decision.reason == "insufficient_variant_evidence"


# ---------------------------------------------------------------------------
# 4->5 long-term gate
# ---------------------------------------------------------------------------


def test_4_to_5_requires_two_applications(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(5)), now=_day(5))
    assert decision.new_mastery == 4
    assert decision.reason == "long_term_evidence_missing"


def test_4_to_5_requires_review_streak(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(5)), now=_day(5))
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(6)), now=_day(6))
    # Two applications, zero review passes -> still 4.
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(7)), now=_day(7))
    assert decision.new_mastery == 4


def test_4_to_5_requires_three_perfect_reviews(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(5)), now=_day(5))
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(6)), now=_day(6))
    engine.apply_assessment(
        _ev(concept_id, source="review", score=1.0, created_at=_day(7)), now=_day(7))
    engine.apply_assessment(
        _ev(concept_id, source="review", score=1.0, created_at=_day(8)), now=_day(8))
    # Only two perfect review passes -> not yet.
    decision = engine.apply_assessment(
        _ev(concept_id, source="review", score=0.9, created_at=_day(9)), now=_day(9))
    assert decision.new_mastery == 4


def test_4_to_5_requires_14_day_span(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(5)), now=_day(5))
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(6)), now=_day(6))
    for day in (7, 8, 9):
        engine.apply_assessment(
            _ev(concept_id, source="review", difficulty="applied", score=1.0,
                created_at=_day(day)), now=_day(day))
    # Span since the first application (day 5) is only ~5 days.
    decision = engine.apply_assessment(
        _ev(concept_id, source="review", difficulty="applied", score=1.0,
            created_at=_day(10)), now=_day(10))
    assert decision.new_mastery == 4
    assert decision.reason == "long_term_evidence_missing"


def test_4_to_5_full_path_promotes(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(5)), now=_day(5))
    engine.apply_assessment(
        _ev(concept_id, difficulty="applied", created_at=_day(6)), now=_day(6))
    for day in (7, 8, 9):
        engine.apply_assessment(
            _ev(concept_id, source="review", difficulty="applied", score=1.0,
                created_at=_day(day)), now=_day(day))
    # Day 21: span 16 days, two applications, streak >=3 -> promote.
    decision = engine.apply_assessment(
        _ev(concept_id, source="review", difficulty="applied", score=1.0,
            created_at=_day(21)), now=_day(21))
    assert (decision.old_mastery, decision.new_mastery) == (4, 5)
    assert decision.reason == "application_long_term_mastery"
    assert store.get_concept(concept_id).mastery_level == 5


def test_basic_review_pass_at_mastery_4_hits_cap(store, engine) -> None:
    """A basic-difficulty review pass re-verifies but cannot prove 5."""
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    decision = engine.apply_assessment(
        _ev(concept_id, source="review", difficulty="basic", score=1.0,
            created_at=_day(30)), now=_day(30))
    assert decision.new_mastery == 4
    assert decision.reason == "difficulty_cap"


# ---------------------------------------------------------------------------
# new-concept protection
# ---------------------------------------------------------------------------


def test_new_concept_protection_blocks_within_24h(store, engine) -> None:
    """Imported/backdated variant evidence can satisfy the variant span
    gate, but within the concept's first 24h the mastery cap still holds."""
    _, concept_id = _make_concept(store)
    # Climb to 3 via basic passes inside day 0.
    engine.apply_assessment(
        _ev(concept_id, difficulty="basic", created_at=_day(0)), now=_day(0))
    engine.apply_assessment(
        _ev(concept_id, difficulty="basic", created_at=_day(0, 1)), now=_day(0, 1))
    engine.apply_assessment(
        _ev(concept_id, difficulty="basic", created_at=_day(0, 2)), now=_day(0, 2))
    # Backdated variant import: 24h before creation.
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(-1, 0)),
        now=_day(0, 3))
    # Variant pass 25h after the first one, but only 1h after creation.
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(0, 1)),
        now=_day(0, 1))
    assert decision.new_mastery == 3
    assert decision.reason == "new_concept_protection"


def test_protection_expires_after_24h(store, engine) -> None:
    _, concept_id = _make_concept(store)
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(-1, 0)),
        now=_day(0))
    engine.apply_assessment(
        _ev(concept_id, difficulty="basic", created_at=_day(0, 1)), now=_day(0, 1))
    engine.apply_assessment(
        _ev(concept_id, difficulty="basic", created_at=_day(0, 2)), now=_day(0, 2))
    # 25h after creation: protection no longer applies.
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="variant", created_at=_day(1, 1)),
        now=_day(1, 1))
    assert decision.new_mastery == 4
    assert store.get_concept(concept_id).state == "mastered"


# ---------------------------------------------------------------------------
# demotion
# ---------------------------------------------------------------------------


def test_single_high_difficulty_fail_no_demotion(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="applied", score=0.3, created_at=_day(7)),
        now=_day(7))
    assert decision.new_mastery == 4
    assert decision.reason == "high_difficulty_single_fail"
    assert store.get_concept(concept_id).retention == "low"
    assert store.get_concept(concept_id).state == "needs_review"


def test_basic_fails_never_demote(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 3)
    engine.apply_assessment(
        _ev(concept_id, difficulty="basic", score=0.1, created_at=_day(4)), now=_day(4))
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="basic", score=0.1, created_at=_day(5)), now=_day(5))
    assert decision.new_mastery == 3
    assert decision.reason == "fail_no_demotion"


def test_consecutive_high_difficulty_fails_demote_one_level(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", score=0.2, created_at=_day(7)), now=_day(7))
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="applied", score=0.2, created_at=_day(8)), now=_day(8))
    assert (decision.old_mastery, decision.new_mastery) == (4, 3)
    assert decision.reason == "high_difficulty_consecutive_fail"


def test_pass_interrupts_fail_streak(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _reach_4(store, engine, concept_id)
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", score=0.2, created_at=_day(7)), now=_day(7))
    engine.apply_assessment(
        _ev(concept_id, difficulty="variant", score=1.0, created_at=_day(8)), now=_day(8))
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="applied", score=0.2, created_at=_day(9)), now=_day(9))
    assert decision.new_mastery == 4  # streak was reset by the pass


# ---------------------------------------------------------------------------
# self assessment / override / retention / confidence
# ---------------------------------------------------------------------------


def test_self_assessment_recorded_but_no_promotion(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 2)
    before = store.get_concept(concept_id).mastery_level
    decision = engine.apply_assessment(
        _ev(concept_id, source="self_assessment", score=1.0, created_at=_day(3)),
        now=_day(3))
    assert decision.new_mastery == before
    assert decision.reason == "self_assessment_not_promotive"
    assert len(store.list_assessments(concept_id)) == 3  # evidence stored


def test_user_override_with_audit(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 2)
    audits_before = len(store.list_mastery_audit(concept_id))
    decision = engine.apply_user_override(concept_id, 4, "用户确认已掌握")
    assert (decision.old_mastery, decision.new_mastery) == (2, 4)
    assert decision.reason == "user_override"
    audit = store.list_mastery_audit(concept_id)
    assert len(audit) == audits_before + 1
    latest = audit[-1]
    assert latest["source"] == "user_override"
    assert latest["old_mastery"] == 2 and latest["new_mastery"] == 4
    assert "用户确认已掌握" in latest["reason"]


def test_user_override_rejects_out_of_range(store, engine) -> None:
    _, concept_id = _make_concept(store)
    with pytest.raises(Exception):
        engine.apply_user_override(concept_id, 6, "too high")


def test_retention_updates(store, engine) -> None:
    _, concept_id = _make_concept(store)
    engine.apply_assessment(
        _ev(concept_id, score=1.0, created_at=_day(0)), now=_day(0))
    assert store.get_concept(concept_id).retention == "high"
    engine.apply_assessment(
        _ev(concept_id, score=0.7, created_at=_day(1)), now=_day(1))
    assert store.get_concept(concept_id).retention == "medium"
    engine.apply_assessment(
        _ev(concept_id, score=0.2, created_at=_day(2)), now=_day(2))
    assert store.get_concept(concept_id).retention == "low"


def test_time_passage_never_changes_mastery(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 3)
    # 30 days later, the same basic pass cannot promote (cap) — and time
    # alone never demotes.
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="basic", score=1.0, created_at=_day(30)),
        now=_day(30))
    assert decision.new_mastery == 3
    preview = engine.evaluate(
        concept_id,
        _ev(concept_id, difficulty="basic", score=1.0, created_at=_day(40)),
        now=_day(40))
    assert preview.new_mastery == 3


def test_low_confidence_recorded_but_no_mastery_change(store, engine) -> None:
    _, concept_id = _make_concept(store)
    _climb_to(store, engine, concept_id, 3)
    before = store.get_concept(concept_id).mastery_level
    decision = engine.apply_assessment(
        _ev(concept_id, difficulty="variant", score=1.0, confidence=0.4,
            created_at=_day(5)), now=_day(5))
    assert decision.new_mastery == before
    assert decision.reason == "low_confidence"
    assert len(store.list_assessments(concept_id)) == 4  # stored


# ---------------------------------------------------------------------------
# hygiene: single record, explainability, no provider
# ---------------------------------------------------------------------------


def test_apply_assessment_records_exactly_once(store, engine) -> None:
    _, concept_id = _make_concept(store)
    engine.apply_assessment(
        _ev(concept_id, score=1.0, created_at=_day(0)), now=_day(0))
    assert len(store.list_assessments(concept_id)) == 1


def test_every_decision_has_reason(store, engine) -> None:
    _, concept_id = _make_concept(store)
    decisions = [
        engine.apply_assessment(_ev(concept_id, score=1.0, created_at=_day(0)), now=_day(0)),
        engine.apply_assessment(_ev(concept_id, score=0.5, created_at=_day(1)), now=_day(1)),
        engine.apply_assessment(_ev(concept_id, score=0.1, created_at=_day(2)), now=_day(2)),
        engine.apply_assessment(
            _ev(concept_id, source="self_assessment", score=1.0, created_at=_day(3)),
            now=_day(3)),
    ]
    for decision in decisions:
        assert decision.reason, "every RuleDecision must be explainable"


def test_rule_engine_never_calls_providers() -> None:
    import core.learning.rule_engine as module
    source = inspect.getsource(module)
    for forbidden in ("ai_router", "providers", "openai", "screen_vision",
                      "requests", "urllib", "http"):
        assert forbidden not in source.lower(), f"forbidden reference: {forbidden}"
