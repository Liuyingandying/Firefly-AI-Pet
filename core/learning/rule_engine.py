"""Deterministic Learning Rule Engine (Phase 1C).

Implements the frozen Phase 0.5 semantics as pure, testable rules:

- The AI teaches; this layer records. Any assessment producer emits an
  :class:`AssessmentEvidence`; ONLY the rule engine decides mastery.
- Score classification is deterministic: >=0.80 pass, >=0.60 partial,
  else fail. Confidence never replaces score; confidence < 0.60 evidence
  is stored but cannot move mastery.
- Single-step rule: one evidence changes mastery by at most +1 / -1.
- Difficulty caps: recall<=2, basic<=3, variant<=4, applied<=5.
- 3->4 requires >=2 variant/applied passes spanning >=24h (different
  records). 4->5 requires >=2 application passes + >=3 consecutive
  perfect review passes + a >=14-day span (state-based gate).
- New-concept protection: within 24h of creation mastery is capped at 3.
- Time NEVER lowers mastery; only counter-evidence (>=2 consecutive
  high-difficulty fails) demotes, by one level. Self-assessment never
  promotes. User overrides go through ``apply_user_override`` (audited).

This module is deterministic domain logic: no model calls, no network, no
UI. ``evaluate`` is a pure preview; ``apply_assessment`` is the production
entry (records the evidence once, then applies the decision).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from core.learning.models import (
    AssessmentEvidence,
    ConceptState,
    MasteryUpdate,
    Retention,
    utc_now_iso,
)
from core.learning.store import LearningStore, LearningStoreError
from core.learning.review_scheduler import ReviewScheduler

PASS_THRESHOLD = 0.80
PARTIAL_THRESHOLD = 0.60
CONFIDENCE_FLOOR = 0.60
MASTERY_MAX = 5

NEW_CONCEPT_PROTECTION_HOURS = 24.0
VARIANT_EVIDENCE_SPAN_HOURS = 24.0
LONG_TERM_SPAN_DAYS = 14.0
REVIEW_STREAK_FOR_MASTERY_FIVE = 3

_REAL_SOURCES = frozenset({"quiz", "review", "applied"})
_HIGH_DIFFICULTIES = frozenset({"variant", "applied"})
_DIFFICULTY_CAPS = {"recall": 2, "basic": 3, "variant": 4, "applied": 5}

OUTCOME_PASS = "pass"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAIL = "fail"


# ---------------------------------------------------------------------------
# Pure rule functions
# ---------------------------------------------------------------------------


def classify_score(score: float) -> str:
    """Deterministic score bands: pass / partial / fail."""
    if score >= PASS_THRESHOLD:
        return OUTCOME_PASS
    if score >= PARTIAL_THRESHOLD:
        return OUTCOME_PARTIAL
    return OUTCOME_FAIL


def difficulty_cap(difficulty: str) -> int:
    """Highest mastery level this difficulty can prove."""
    return _DIFFICULTY_CAPS[difficulty]


def is_new_concept_protected(created_at: str, now: str) -> bool:
    """True within NEW_CONCEPT_PROTECTION_HOURS of concept creation."""
    created = _parse_ts(created_at)
    moment = _parse_ts(now)
    if created is None or moment is None:
        return False
    hours = (moment - created).total_seconds() / 3600.0
    return hours < NEW_CONCEPT_PROTECTION_HOURS


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _trailing_high_difficulty_fails(records) -> int:
    """Longest trailing run of variant/applied records below the fail
    threshold (chronological input; a pass resets the streak)."""
    count = 0
    for record in reversed(records):
        if record.difficulty in _HIGH_DIFFICULTIES and record.score < PARTIAL_THRESHOLD:
            count += 1
        else:
            break
    return count


def _qualifying_passes(records, *, difficulty: str | None = None) -> list:
    """Real-source records at/above the pass threshold, optionally filtered
    by difficulty."""
    return [
        record
        for record in records
        if record.source in _REAL_SOURCES
        and record.score >= PASS_THRESHOLD
        and (difficulty is None or record.difficulty == difficulty)
    ]


# ---------------------------------------------------------------------------
# Decision object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleDecision:
    """Explainable outcome of one evidence evaluation."""

    concept_id: str
    old_mastery: int
    new_mastery: int
    old_retention: str
    new_retention: str
    changed: bool
    eligible: bool
    reason: str            # stable machine code (audit trail)
    detail: str = ""       # human-readable explanation
    evidence_ids: tuple[str, ...] = ()
    review_action: str = "none"
    state: str | None = None
    mastery_update: MasteryUpdate | None = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class LearningRuleEngine:
    """Reads history, applies the frozen rules, writes via the Store."""

    def __init__(self, store: LearningStore, *, scheduler: ReviewScheduler | None = None,
                 now_fn: Callable[[], str] | None = None) -> None:
        self._store = store
        self._now_fn = now_fn or utc_now_iso
        self.scheduler = scheduler or ReviewScheduler(store, now_fn=self._now_fn)

    def _now(self, now: str | None = None) -> str:
        return now or self._now_fn()

    # -- public API -------------------------------------------------------

    def evaluate(self, concept_id: str, evidence: AssessmentEvidence,
                 *, now: str | None = None) -> RuleDecision:
        """Pure preview: decide without writing anything. The decision is
        computed against the persisted history (the evidence itself is not
        yet recorded); ``apply_assessment`` is the authoritative entry."""
        concept = self._store.get_concept(concept_id)
        if concept is None:
            raise LearningStoreError(f"concept not found: {concept_id}")
        evidence = evidence.normalized()
        history = self._store.list_assessments(concept_id)
        decision = self._decide(concept, evidence, history, now=self._now(now))
        return replace(decision, review_action=self._review_preview(decision))

    def apply_assessment(self, evidence: AssessmentEvidence,
                         *, now: str | None = None) -> RuleDecision:
        """Production entry: record the evidence exactly once, evaluate
        deterministically, then apply mastery/state/retention/review."""
        evidence = evidence.normalized()
        concept = self._store.get_concept(evidence.concept_id)
        if concept is None:
            raise LearningStoreError(f"concept not found: {evidence.concept_id}")

        record = self._store.record_assessment(
            concept.course_id,
            evidence.concept_id,
            source=evidence.source,
            score=evidence.score,
            confidence=evidence.confidence,
            difficulty=evidence.difficulty,
            ai_generated=bool((evidence.metadata or {}).get("ai_generated", False)),
            session_id=evidence.session_id,
            created_at=evidence.created_at or None,
        )
        history = self._store.list_assessments(evidence.concept_id)
        decision = self._decide(concept, evidence, history, now=self._now(now))

        if decision.changed:
            mastery_update = self._store.apply_mastery_update(
                evidence.concept_id,
                decision.new_mastery,
                decision.new_retention,
                reason=decision.reason,
                source=evidence.source,
            )
            self._store.update_concept_state(
                evidence.concept_id,
                state=decision.state,
                retention=decision.new_retention,
            )
            decision = replace(decision, mastery_update=mastery_update)
        else:
            self._store.update_concept_state(
                evidence.concept_id,
                state=decision.state,
                retention=decision.new_retention,
            )

        review_action = self._sync_review(evidence, decision, now=now)
        return replace(decision, evidence_ids=(record.id,), review_action=review_action)

    def apply_user_override(self, concept_id: str, target_mastery: int,
                            reason: str) -> RuleDecision:
        """User-confirmed mastery correction. The ONLY path that may set an
        arbitrary (validated) level; fully audited via the store."""
        concept = self._store.get_concept(concept_id)
        if concept is None:
            raise LearningStoreError(f"concept not found: {concept_id}")
        target = int(target_mastery)
        if not 0 <= target <= MASTERY_MAX:
            raise LearningStoreError("override mastery must be an integer 0-5")
        clean_reason = (reason or "").strip()
        if not clean_reason:
            raise LearningStoreError("override requires a reason")
        update = self._store.apply_mastery_update(
            concept_id, target, concept.retention,
            reason=f"user_override: {clean_reason}",
            source="user_override",
        )
        state = (ConceptState.MASTERED.value if target >= 4
                 else ConceptState.ASSESSED.value)
        self._store.update_concept_state(concept_id, state=state)
        review_action = self.scheduler.reschedule_base(concept_id, target)
        return RuleDecision(
            concept_id=concept_id,
            old_mastery=concept.mastery_level,
            new_mastery=target,
            old_retention=concept.retention,
            new_retention=concept.retention,
            changed=True,
            eligible=True,
            reason="user_override",
            detail=clean_reason,
            review_action=review_action,
            state=state,
            mastery_update=update,
        )

    # -- decision core ------------------------------------------------------

    def _decide(self, concept, evidence: AssessmentEvidence, history: list,
                *, now: str | None) -> RuleDecision:
        old_mastery = concept.mastery_level
        old_retention = concept.retention
        outcome = classify_score(evidence.score)
        new_mastery = old_mastery
        reason = ""
        detail = ""

        if evidence.source == "self_assessment":
            reason = "self_assessment_not_promotive"
            detail = "自评只作辅助证据，不能独立升级掌握度。"
        elif evidence.source == "user_override":
            reason = "user_override_requires_explicit_apply"
            detail = "人工修改必须走 apply_user_override 通道。"
        elif evidence.confidence < CONFIDENCE_FLOOR:
            reason = "low_confidence"
            detail = f"confidence {evidence.confidence:.2f} 低于 {CONFIDENCE_FLOOR}，仅记录不改变掌握度。"
        elif outcome == OUTCOME_FAIL:
            streak = _trailing_high_difficulty_fails(history)
            if evidence.difficulty in _HIGH_DIFFICULTIES and streak >= 2 and old_mastery > 0:
                new_mastery = old_mastery - 1
                reason = "high_difficulty_consecutive_fail"
                detail = f"高难度题连续 {streak} 次未通过，掌握度 -1。"
            elif evidence.difficulty in _HIGH_DIFFICULTIES:
                reason = "high_difficulty_single_fail"
                detail = "高难度题单次失败不降级，仅降低 retention。"
            else:
                reason = "fail_no_demotion"
                detail = "基础题失败不自动降级，仅降低 retention 并提前复习。"
        elif outcome == OUTCOME_PARTIAL:
            reason = "partial_no_promotion"
            detail = "部分通过不升级掌握度。"
        else:  # pass
            cap = difficulty_cap(evidence.difficulty)
            if old_mastery >= cap:
                reason = "difficulty_cap"
                detail = f"{evidence.difficulty} 题最高只能证明 mastery {cap}。"
            elif old_mastery == 0:
                new_mastery = 1
                reason = "first_assessment_pass"
                detail = "首次有效评估通过。"
            elif old_mastery == 3:
                if self._variant_gate(history):
                    new_mastery = 4
                    reason = "variant_evidence_sufficient"
                    detail = "两次变式/综合题通过且跨 >=24h。"
                else:
                    reason = "insufficient_variant_evidence"
                    detail = "需要两次变式/综合题通过，且跨 >=24h。"
            elif old_mastery == 4:
                gate = self._long_term_gate(history, now)
                if gate:
                    new_mastery = 5
                    reason = "application_long_term_mastery"
                    detail = "应用题 >=2 次 + 复习连续 3 次满分 + 跨 >=14 天。"
                else:
                    reason = "long_term_evidence_missing"
                    detail = "需要应用题 >=2 次、复习连续 3 次满分、跨 >=14 天。"
            else:  # 1->2 / 2->3
                new_mastery = old_mastery + 1
                reason = f"{evidence.difficulty}_pass"
                detail = f"{evidence.difficulty} 题通过，mastery {old_mastery} -> {new_mastery}。"

        # New-concept protection: within 24h mastery cannot exceed 3.
        now_value = now or self._now_fn()
        if (new_mastery > 3
                and is_new_concept_protected(concept.created_at, now_value)):
            new_mastery = 3
            reason = "new_concept_protection"
            detail = "新概念 24h 保护期：最高 mastery 3。"

        # Single-step safety (override path bypasses via apply_user_override).
        new_mastery = min(new_mastery, old_mastery + 1, MASTERY_MAX)

        changed = new_mastery != old_mastery
        new_retention = self._next_retention(evidence, outcome, old_retention)
        new_state = self._next_state(evidence, outcome, old_mastery, new_mastery,
                                     changed, concept.state)
        return RuleDecision(
            concept_id=concept.id,
            old_mastery=old_mastery,
            new_mastery=new_mastery,
            old_retention=old_retention,
            new_retention=new_retention,
            changed=changed,
            eligible=changed,
            reason=reason,
            detail=detail,
            state=new_state,
        )

    @staticmethod
    def _variant_gate(history: list) -> bool:
        """3->4: >=2 qualifying variant/applied passes (different records),
        the latest two spanning >=24h. Basic passes never count toward it."""
        qualifying = [
            record for record in _qualifying_passes(history)
            if record.difficulty in _HIGH_DIFFICULTIES
        ]
        if len(qualifying) < 2:
            return False
        latest, previous = qualifying[-1], qualifying[-2]
        latest_ts = _parse_ts(latest.created_at)
        previous_ts = _parse_ts(previous.created_at)
        if latest_ts is None or previous_ts is None:
            return False
        return (latest_ts - previous_ts).total_seconds() / 3600.0 >= VARIANT_EVIDENCE_SPAN_HOURS

    def _long_term_gate(self, history: list, now: str | None) -> bool:
        """4->5: >=2 application passes + >=3 consecutive perfect review
        passes + >=14-day span since the first application pass."""
        applications = _qualifying_passes(history, difficulty="applied")
        if len(applications) < 2:
            return False
        reviews = [record for record in history if record.source == "review"]
        streak = 0
        for record in reversed(reviews):
            if record.score >= 1.0:
                streak += 1
            else:
                break
        if streak < REVIEW_STREAK_FOR_MASTERY_FIVE:
            return False
        first_ts = _parse_ts(applications[0].created_at)
        moment = _parse_ts(now or self._now_fn())
        if first_ts is None or moment is None:
            return False
        return (moment - first_ts).total_seconds() / 86400.0 >= LONG_TERM_SPAN_DAYS

    @staticmethod
    def _next_retention(evidence: AssessmentEvidence, outcome: str,
                        old_retention: str) -> str:
        if evidence.source in ("self_assessment", "user_override"):
            return old_retention
        if outcome == OUTCOME_PASS:
            return Retention.HIGH.value
        if outcome == OUTCOME_PARTIAL:
            return old_retention if old_retention == Retention.LOW.value else Retention.MEDIUM.value
        return Retention.LOW.value

    @staticmethod
    def _next_state(evidence: AssessmentEvidence, outcome: str,
                    old_mastery: int, new_mastery: int, changed: bool,
                    old_state: str) -> str:
        if evidence.source in ("self_assessment", "user_override"):
            return old_state
        if outcome == OUTCOME_FAIL or new_mastery < old_mastery:
            return ConceptState.NEEDS_REVIEW.value
        if new_mastery >= 4:
            return ConceptState.MASTERED.value
        if new_mastery == 1 and old_mastery == 0:
            return ConceptState.LEARNING.value
        if old_state in (ConceptState.DISCOVERED.value, ConceptState.LEARNING.value):
            return ConceptState.ASSESSED.value
        return old_state

    # -- review glue --------------------------------------------------------

    def _sync_review(self, evidence: AssessmentEvidence, decision: RuleDecision,
                     *, now: str | None) -> str:
        if evidence.source == "self_assessment":
            return "none"
        if evidence.source == "user_override":
            return self.scheduler.reschedule_base(
                evidence.concept_id, decision.new_mastery, now=now)
        if evidence.source not in _REAL_SOURCES:
            return "none"
        outcome = classify_score(evidence.score)
        return self.scheduler.sync_after_assessment(
            evidence.concept_id,
            mastery_level=decision.new_mastery,
            outcome=outcome,
            cycle="review" if evidence.source == "review" else "fresh",
            now=now,
        )

    def _review_preview(self, decision: RuleDecision) -> str:
        if decision.new_mastery <= 0:
            return "unscheduled"
        return f"planned:{decision.new_mastery}"
