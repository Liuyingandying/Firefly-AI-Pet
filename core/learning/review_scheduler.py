"""Deterministic Review Scheduler (Phase 1C).

Frozen Phase 0.5 intervals by mastery: 1:1d, 2:3d, 3:7d, 4:14d, 5:30d.
Review pass multiplies the interval by 1.5 (ceiling at base x2; mastery 5
may reach 60d after stable passes); fail halves (floor 1 day, retention
low); partial keeps the interval with medium retention. One active
ReviewItem per concept — history lives in AssessmentRecords/audit.

Pure domain logic: no model calls, no network. Scheduling uses
``datetime.fromisoformat`` arithmetic on UTC-ISO timestamps with ``ceil``
rounding for day conversion.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Callable

from core.learning.models import Retention, utc_now_iso
from core.learning.store import LearningStore, LearningStoreError

BASE_INTERVAL_DAYS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
PASS_MULTIPLIER = 1.5
FAIL_DIVISOR = 2.0
MIN_INTERVAL_DAYS = 1
MAX_INTERVAL_FACTOR = 2.0
MASTERY_FIVE_MAX_DAYS = 60

SNOOZE_MIN_DAYS = 1

OUTCOME_PASS = "pass"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAIL = "fail"


def base_review_interval(mastery: int) -> int:
    """Frozen base interval (days) for a mastery level; 0 -> unscheduled."""
    return BASE_INTERVAL_DAYS.get(int(mastery), 0)


def next_review_interval(mastery: int, outcome: str, previous_days: int) -> int:
    """Deterministic next interval in days.

    - pass: previous x1.5 (ceil), capped at base x2 (mastery 5 cap: 60)
    - fail: previous /2 (ceil), floor 1 day
    - partial: unchanged
    """
    mastery = int(mastery)
    previous = max(1, int(previous_days or 0))
    base = base_review_interval(mastery)
    if outcome == OUTCOME_PASS:
        candidate = math.ceil(previous * PASS_MULTIPLIER)
        cap = MASTERY_FIVE_MAX_DAYS if mastery == 5 else math.ceil(base * MAX_INTERVAL_FACTOR)
        return min(candidate, max(base, cap))
    if outcome == OUTCOME_FAIL:
        return max(MIN_INTERVAL_DAYS, math.ceil(previous / FAIL_DIVISOR))
    return previous  # partial keeps the interval


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _format_ts(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


class ReviewScheduler:
    """Maintains exactly one active ReviewItem per concept."""

    def __init__(self, store: LearningStore, *, now_fn: Callable[[], str] | None = None) -> None:
        self._store = store
        self._now_fn = now_fn or utc_now_iso

    # -- helpers -----------------------------------------------------------

    def _active_item(self, concept_id: str):
        for item in self._store.list_review_items(concept_id):
            if item.status == "pending":
                return item
        return None

    # -- public API ----------------------------------------------------------

    def sync_after_assessment(
        self,
        concept_id: str,
        *,
        mastery_level: int,
        outcome: str,
        cycle: str = "fresh",
        now: str | None = None,
    ) -> str:
        """Create/update the concept's single active ReviewItem from one
        assessment outcome. Returns a short action label for the decision."""
        mastery_level = int(mastery_level)
        now_value = now or self._now_fn()
        if mastery_level <= 0:
            return "unscheduled"
        item = self._active_item(concept_id)
        base = base_review_interval(mastery_level)

        if item is None:
            interval = base
            due = self._due_from(now_value, interval)
            self._store.create_review_item(
                concept_id, due_at=due, interval_days=interval,
                retention=self._retention_for(outcome), status="pending",
            )
            return f"scheduled:{interval}d"

        if outcome == OUTCOME_PASS and cycle == "review":
            interval = next_review_interval(
                mastery_level, OUTCOME_PASS, item.interval_days)
        elif outcome == OUTCOME_FAIL:
            interval = next_review_interval(
                mastery_level, OUTCOME_FAIL, item.interval_days)
        else:
            interval = item.interval_days  # partial / fresh pass keeps pacing
        due = self._due_from(now_value, interval)
        self._store.update_review_item(
            item.id,
            due_at=due,
            interval_days=interval,
            status="pending",
            retention=self._retention_for(outcome),
            consecutive_success=item.consecutive_success + 1
            if outcome == OUTCOME_PASS else 0,
        )
        return f"rescheduled:{interval}d"

    def reschedule_base(self, concept_id: str, mastery_level: int,
                        *, now: str | None = None) -> str:
        """Reset pacing to the frozen base interval (e.g. after override)."""
        mastery_level = int(mastery_level)
        now_value = now or self._now_fn()
        if mastery_level <= 0:
            return "unscheduled"
        interval = base_review_interval(mastery_level)
        item = self._active_item(concept_id)
        due = self._due_from(now_value, interval)
        if item is None:
            self._store.create_review_item(
                concept_id, due_at=due, interval_days=interval,
                retention=Retention.MEDIUM.value, status="pending",
            )
            return f"scheduled:{interval}d"
        self._store.update_review_item(
            item.id, due_at=due, interval_days=interval, status="pending")
        return f"rescheduled:{interval}d"

    def mark_relearning(self, concept_id: str, *, now: str | None = None) -> str:
        """'重新学习' semantics: state -> LEARNING, retention -> low, review
        re-anchored from the CURRENT mastery (mastery is never reset here)."""
        concept = self._store.get_concept(concept_id)
        if concept is None:
            raise LearningStoreError(f"concept not found: {concept_id}")
        self._store.update_concept_state(
            concept_id, state="learning", retention=Retention.LOW.value)
        return self.reschedule_base(concept_id, concept.mastery_level, now=now)

    def snooze_review(self, concept_id: str, days: int, *,
                       now: str | None = None) -> str:
        """User scheduling control ('稍后复习'): push the due date; mastery
        untouched. days >= 1."""
        if int(days) < SNOOZE_MIN_DAYS:
            raise LearningStoreError("snooze days must be >= 1")
        now_value = now or self._now_fn()
        item = self._active_item(concept_id)
        if item is None:
            raise LearningStoreError(f"no active review item for concept {concept_id}")
        base = _parse_ts(item.due_at) or _parse_ts(now_value)
        due = _format_ts(base + timedelta(days=int(days)))
        self._store.update_review_item(item.id, due_at=due, status="pending")
        return f"snoozed:{int(days)}d"

    def complete_review(self, concept_id: str, *, skipped: bool = False) -> str:
        """Close the active item after a review interaction (the scheduler
        re-creates/updates pacing on the next assessment)."""
        item = self._active_item(concept_id)
        if item is None:
            return "none"
        self._store.update_review_item(
            item.id, status="skipped" if skipped else "done")
        return "skipped" if skipped else "done"

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _retention_for(outcome: str) -> str:
        if outcome == OUTCOME_PASS:
            return Retention.HIGH.value
        if outcome == OUTCOME_FAIL:
            return Retention.LOW.value
        return Retention.MEDIUM.value

    @staticmethod
    def _due_from(now_value: str, interval_days: int) -> str:
        moment = _parse_ts(now_value) or datetime.now()
        return _format_ts(moment + timedelta(days=int(interval_days)))
