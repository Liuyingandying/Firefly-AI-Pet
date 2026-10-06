"""Review Scheduler tests (Phase 1C).

Locks the frozen intervals (1/3/7/14/30 days by mastery), pass x1.5 with
base-x2 cap (mastery 5 max 60), fail /2 with 1-day floor, partial keeping
the interval, single active ReviewItem per concept, due queries, snooze,
and relearning semantics. Deterministic timestamps throughout.
"""

from __future__ import annotations

import inspect

import pytest

from core.learning import LearningStore
from core.learning.review_scheduler import (
    ReviewScheduler,
    base_review_interval,
    next_review_interval,
)

T0 = "2026-09-01T00:00:00+00:00"


def _day(n: int) -> str:
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    # Match the store's millisecond ISO format so string comparisons in
    # get_due_reviews behave like datetime comparisons.
    return (base + timedelta(days=n)).isoformat(timespec="milliseconds")


@pytest.fixture()
def env(tmp_path):
    store = LearningStore(tmp_path / "learning.sqlite3")
    store.initialize()
    course = store.create_course("自动控制原理")
    concept = store.add_concept(course.id, "根轨迹", created_at=T0)
    scheduler = ReviewScheduler(store)
    return store, scheduler, concept.id


# -- pure functions -----------------------------------------------------------


def test_base_intervals_frozen() -> None:
    assert [base_review_interval(m) for m in range(6)] == [0, 1, 3, 7, 14, 30]


def test_next_interval_pass_cap_fail_floor() -> None:
    # mastery 3 base 7: pass 7 -> 11 (ceil 10.5), cap 14.
    assert next_review_interval(3, "pass", 7) == 11
    assert next_review_interval(3, "pass", 11) == 14  # cap base*2
    # mastery 5 may reach 60.
    assert next_review_interval(5, "pass", 30) == 45
    assert next_review_interval(5, "pass", 45) == 60
    assert next_review_interval(5, "pass", 60) == 60
    # fail halves with a 1-day floor.
    assert next_review_interval(3, "fail", 7) == 4
    assert next_review_interval(3, "fail", 1) == 1
    # partial keeps the interval.
    assert next_review_interval(3, "partial", 7) == 7


# -- scheduling behavior --------------------------------------------------------


def test_first_pass_schedules_base_interval(env) -> None:
    store, scheduler, concept_id = env
    action = scheduler.sync_after_assessment(
        concept_id, mastery_level=1, outcome="pass", now=_day(0))
    assert action == "scheduled:1d"
    items = store.list_review_items(concept_id)
    assert len(items) == 1
    assert items[0].due_at == _day(1)


def test_interval_matches_mastery(env) -> None:
    store, scheduler, concept_id = env
    for mastery, expected in ((2, 3), (3, 7), (4, 14), (5, 30)):
        # Fresh concept per level keeps one active item each.
        concept = store.add_concept(
            store.list_courses()[0].id, f"概念{mastery}", created_at=T0)
        scheduler.sync_after_assessment(
            concept.id, mastery_level=mastery, outcome="pass", now=_day(0))
        item = [i for i in store.list_review_items(concept.id) if i.status == "pending"][0]
        assert item.interval_days == expected
    _ = expected  # last loop value


def test_review_pass_extends_interval(env) -> None:
    store, scheduler, concept_id = env
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="pass", now=_day(0))  # 7d
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="pass", cycle="review", now=_day(7))
    item = [i for i in store.list_review_items(concept_id) if i.status == "pending"][0]
    assert item.interval_days == 11  # ceil(7 * 1.5)
    assert item.due_at == _day(18)


def test_review_fail_halves_and_sets_low_retention(env) -> None:
    store, scheduler, concept_id = env
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="pass", now=_day(0))  # 7d
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="fail", cycle="review", now=_day(7))
    item = [i for i in store.list_review_items(concept_id) if i.status == "pending"][0]
    assert item.interval_days == 4  # ceil(7/2)
    assert item.retention == "low"
    assert item.consecutive_success == 0


def test_partial_keeps_interval_medium_retention(env) -> None:
    store, scheduler, concept_id = env
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="pass", now=_day(0))
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="partial", cycle="review", now=_day(7))
    item = [i for i in store.list_review_items(concept_id) if i.status == "pending"][0]
    assert item.interval_days == 7
    assert item.retention == "medium"


def test_single_active_review_item(env) -> None:
    store, scheduler, concept_id = env
    for day in range(4):
        scheduler.sync_after_assessment(
            concept_id, mastery_level=3, outcome="pass", cycle="review",
            now=_day(day))
    pending = [i for i in store.list_review_items(concept_id) if i.status == "pending"]
    assert len(pending) == 1


def test_due_reviews_respect_schedule(env) -> None:
    store, scheduler, concept_id = env
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="pass", now=_day(0))  # due day 7
    assert store.get_due_reviews(now=_day(6)) == []
    due = store.get_due_reviews(now=_day(7))
    assert len(due) == 1 and due[0].concept_id == concept_id


def test_snooze_review(env) -> None:
    store, scheduler, concept_id = env
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="pass", now=_day(0))
    scheduler.snooze_review(concept_id, 2, now=_day(5))
    item = [i for i in store.list_review_items(concept_id) if i.status == "pending"][0]
    assert item.due_at == _day(9)  # day-7 due + 2 days (snoozed from day 5)
    with pytest.raises(Exception):
        scheduler.snooze_review(concept_id, 0)


def test_mark_relearning(env) -> None:
    store, scheduler, concept_id = env
    # Give the concept a mastery level first (relearning never resets it).
    store.apply_mastery_update(concept_id, new_mastery=3,
                               retention="medium", reason="test climb")
    scheduler.sync_after_assessment(
        concept_id, mastery_level=3, outcome="pass", now=_day(0))
    action = scheduler.mark_relearning(concept_id, now=_day(3))
    assert action == "rescheduled:7d"
    concept = store.get_concept(concept_id)
    assert concept.state == "learning"
    assert concept.retention == "low"
    assert concept.mastery_level == 3  # never reset by relearning itself


def test_mastery_zero_not_scheduled(env) -> None:
    store, scheduler, concept_id = env
    action = scheduler.sync_after_assessment(
        concept_id, mastery_level=0, outcome="fail", now=_day(0))
    assert action == "unscheduled"
    assert store.list_review_items(concept_id) == []


def test_scheduler_never_calls_providers() -> None:
    import core.learning.review_scheduler as module

    source = inspect.getsource(module)
    for forbidden in ("ai_router", "providers", "openai", "screen_vision",
                      "requests", "urllib", "http"):
        assert forbidden not in source.lower(), f"forbidden reference: {forbidden}"
