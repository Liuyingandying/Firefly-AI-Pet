r"""Assessment flow service (Phase 1D).

Owns the assessment lifecycle ONLY — a tiny deterministic state machine:

    IDLE -> QUESTION_GENERATED -> WAITING_ANSWER -> GRADING -> RECORDED
                                              \-> (any error) FAILED

Layering (UI never touches the Rule Engine):

    UI -> AssessmentService -> Quiz Adapter -> Rule Engine -> Store

Ordinary chat / PageLens explanations never reach this service; the
LearningModeController routes only explicit check-understanding flows here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Callable

from core.learning.intents import QUIZ_MARKERS, REVIEW_MARKERS
from core.learning.models import AssessmentEvidence
from core.learning.quiz_adapter import (
    ConceptBinding,
    QuizGrading,
    bind_quiz_to_concept,
    generate_concept_question,
    grade_concept_answer,
    quiz_grading_to_evidence,
)
from core.learning.rule_engine import LearningRuleEngine
from core.learning.store import LearningStore, LearningStoreError


class AssessmentState(str, Enum):
    IDLE = "idle"
    QUESTION_GENERATED = "question_generated"
    WAITING_ANSWER = "waiting_answer"
    GRADING = "grading"
    RECORDED = "recorded"
    FAILED = "failed"


CHECK_UNDERSTANDING_MARKERS = QUIZ_MARKERS
# Phase 1E: review-practice phrasing routes to the SAME assessment flow, but
# prefers a concept whose ReviewItem is already due.
REVIEW_PRACTICE_MARKERS = REVIEW_MARKERS


@dataclass
class ActiveAssessment:
    """One in-flight assessment (question issued, answer pending)."""

    course_id: str
    course_name: str
    concept_id: str
    concept_name: str
    question: str
    state: AssessmentState = AssessmentState.WAITING_ANSWER
    session_id: str | None = None
    error: str = ""


@dataclass
class AssessmentOutcome:
    """The user-facing result of one recorded assessment."""

    reply: str
    decision: Any | None = None          # RuleDecision
    evidence: AssessmentEvidence | None = None
    state: AssessmentState = AssessmentState.RECORDED


def is_check_understanding_request(text: str) -> bool:
    return any(marker in (text or "") for marker in CHECK_UNDERSTANDING_MARKERS)


def is_review_practice_request(text: str) -> bool:
    return any(marker in (text or "") for marker in REVIEW_PRACTICE_MARKERS)


class AssessmentService:
    """Runs the assessment loop for the active learning course."""

    def __init__(
        self,
        store: LearningStore,
        rule_engine: LearningRuleEngine | None = None,
        *,
        generate_question: Callable[[str, str], str] | None = None,
        grade_answer: Callable[[str, str, str, str], QuizGrading] | None = None,
    ) -> None:
        self._store = store
        self._engine = rule_engine or LearningRuleEngine(store)
        # Injectable graders (tests); default: existing router-backed adapter.
        self._generate_question = generate_question or generate_concept_question
        self._grade_answer = grade_answer or (
            lambda concept, course, question, answer: grade_concept_answer(
                concept, course, question, answer)
        )
        self.active: ActiveAssessment | None = None
        self._last_state: AssessmentState = AssessmentState.IDLE
        # Phase 8A: a needs_choice answer leaves the quiz waiting for the user
        # to NAME a concept. The pending course is remembered so the controller
        # can resume the quiz when the user answers with a concept name.
        self.pending_choice: tuple[str, str, str | None] | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def state(self) -> AssessmentState:
        return self.active.state if self.active else self._last_state

    def start_assessment(self, course_id: str, course_name: str,
                         requested_concept: str | None = None,
                         session_id: str | None = None,
                         *, prefer_due: bool = False) -> str:
        """Generate one question for the bound concept. Returns the question
        (or a guidance reply when binding needs user input).

        ``prefer_due`` (Phase 1E review practice) picks the course concept
        whose ReviewItem is already due, falling back to normal binding when
        nothing is due — it never invents a concept.
        """
        concept_id: str | None = None
        concept_name: str | None = None
        if prefer_due and not requested_concept:
            due_concept = self._due_concept_for_course(course_id)
            if due_concept is not None:
                concept_id, concept_name = due_concept
        if concept_id is None:
            binding: ConceptBinding = bind_quiz_to_concept(
                self._store, course_id, requested_concept)
            if binding.status == "needs_choice":
                # Remember the course so the user's concept-name answer can
                # resume the quiz (Phase 8A wiring).
                self.pending_choice = (course_id, course_name, session_id)
                names = "、".join(name for _, name in binding.choices[:5])
                return f"要检查哪个知识点？当前项目里有：{names}"
            if binding.status == "candidate":
                return (
                    "这个项目还没有对应的知识点。先学习并添加知识点后，"
                    "再让我出题检查吧。"
                )
            concept_id = binding.concept_id
            concept_name = binding.concept_name or requested_concept or ""
        self.pending_choice = None
        concept_name = concept_name or ""
        question = self._generate_question(concept_name, course_name)
        if not question:
            self.active = ActiveAssessment(
                course_id, course_name, concept_id, concept_name, "",
                state=AssessmentState.FAILED, error="empty_question",
                session_id=session_id)
            return "（我这边没出成题，再让我试一次？）"
        self.active = ActiveAssessment(
            course_id=course_id, course_name=course_name,
            concept_id=concept_id, concept_name=concept_name,
            question=question, state=AssessmentState.WAITING_ANSWER,
            session_id=session_id,
        )
        return question

    def submit_answer(self, answer: str) -> AssessmentOutcome:
        """Grade the pending answer, record evidence once, apply rules."""
        if self.active is None or self.active.state != AssessmentState.WAITING_ANSWER:
            return AssessmentOutcome(
                reply="现在没有待回答的题目。说「检查一下我的理解」来出题。",
                state=AssessmentState.IDLE)
        assessment = self.active
        assessment.state = AssessmentState.GRADING
        try:
            grading: QuizGrading = self._grade_answer(
                assessment.concept_name, assessment.course_name,
                assessment.question, answer)
            evidence = quiz_grading_to_evidence(
                grading, assessment.concept_id, session_id=assessment.session_id)
            if not evidence.created_at:
                # Make the record timestamp explicit at submit time so the
                # rule engine's span gates see consistent wall-clock data.
                from core.learning.models import utc_now_iso

                evidence = replace(evidence, created_at=utc_now_iso())
            decision = self._engine.apply_assessment(evidence)
        except Exception as exc:  # grading/engine failure: never lose the loop
            assessment.state = AssessmentState.FAILED
            assessment.error = type(exc).__name__
            self.active = None
            self._last_state = AssessmentState.FAILED
            return AssessmentOutcome(
                reply="（判卷这一步出了点小问题，稍后再试一次？）",
                state=AssessmentState.FAILED)
        self.active = None
        self._last_state = AssessmentState.RECORDED
        return AssessmentOutcome(
            reply=self._format_reply(assessment, grading, decision),
            decision=decision, evidence=evidence,
            state=AssessmentState.RECORDED)

    def resume_pending_choice(self, concept_name: str) -> str | None:
        """Resume a needs_choice quiz with the user-named concept (Phase 8A).

        Returns None when no choice is pending (the controller then treats the
        message as ordinary chat).
        """
        if self.pending_choice is None:
            return None
        course_id, course_name, session_id = self.pending_choice
        return self.start_assessment(
            course_id, course_name,
            requested_concept=concept_name, session_id=session_id,
        )

    def cancel(self) -> None:
        self.active = None
        self._last_state = AssessmentState.IDLE
        self.pending_choice = None

    # -- helpers ---------------------------------------------------------------

    def _due_concept_for_course(self, course_id: str) -> tuple[str, str] | None:
        """First due-review concept that belongs to the course.

        (concept_id, canonical_name) or None. Read-only; a store hiccup
        degrades to None so review practice falls back to plain binding.
        """
        try:
            due_items = self._store.get_due_reviews()
            if not due_items:
                return None
            names = {
                concept.id: concept.canonical_name
                for concept in self._store.list_concepts(course_id)
            }
        except LearningStoreError:
            return None
        for item in due_items:
            name = names.get(item.concept_id)
            if name is not None:
                return item.concept_id, name
        return None

    @staticmethod
    def _format_reply(assessment: ActiveAssessment, grading: QuizGrading,
                      decision) -> str:
        parts = [grading.feedback or "（评分结果为空）"]
        if decision is not None and decision.changed:
            parts.append(
                f"掌握度更新：{decision.old_mastery} → {decision.new_mastery}"
                f"（{decision.detail}）"
            )
        elif decision is not None:
            parts.append(f"掌握度保持 {decision.new_mastery}（{decision.detail}）")
        return "\n\n".join(parts)
