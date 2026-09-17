"""LearningActionExecutor (Phase 5).

Executes ONE already-decided :class:`LearningRecommendation`. It never decides
which action to take (Phase 4 does that), never changes mastery, never touches
the rule engine or the curriculum, and never adds a provider.

Action mapping (frozen by the phase spec):

    EXPLAIN        -> returns the teaching constraint for the concept
    EXPLAIN_RECALL -> returns a recall hint
    PRACTICE       -> drives the EXISTING AssessmentService (opt-in side effect)
    REVIEW         -> reads the concept's ReviewItem state
    TRANSFER       -> returns an application-training request
    MOVE_NEXT      -> reads the curriculum's next node (from LearningContext)
    FREE_CHAT      -> skipped (nothing to execute)

Read-only by default: ``execute(..., run_assessment=False)`` prepares text
without ever starting an assessment, which is what the per-turn prompt
injection uses. Only an explicit invocation passes ``run_assessment=True``.

Every failure is caught and reported as a DEGRADED/FAILED result — an action
can never break a chat turn or raise into the caller.
"""

from __future__ import annotations

from typing import Any

from core.learning.action.models import ActionStatus, LearningActionResult
from core.learning.decision.models import LearningAction, LearningRecommendation

#: Prompt-facing guidance per action (single source; {concept}/{next} filled in).
CONSTRAINT_BY_ACTION: dict[LearningAction, str] = {
    LearningAction.EXPLAIN: (
        "先解释「{concept}」的概念与直觉，再给一个具体例子，最后问一句是否理解；"
        "不要直接给答案。"
    ),
    LearningAction.EXPLAIN_RECALL: (
        "先请学习者回忆「{concept}」，再给一句提示，然后问一个简单问题。"
    ),
    LearningAction.PRACTICE: (
        "该进入练习：围绕「{concept}」出一道题，由「考考我」流程启动，不要替学习者作答。"
    ),
    LearningAction.REVIEW: (
        "该复习「{concept}」：先回顾上次要点，再检查是否还记得。"
    ),
    LearningAction.TRANSFER: (
        "给「{concept}」一个真实应用场景，请学习者说明如何迁移；不要直接给答案。"
    ),
    LearningAction.MOVE_NEXT: "本章已掌握：准备进入下一结构节点{next}。",
    LearningAction.FREE_CHAT: "",
}

#: Actions that are pure text preparation (no store/provider interaction).
_READ_ONLY_ACTIONS = frozenset(
    {
        LearningAction.EXPLAIN,
        LearningAction.EXPLAIN_RECALL,
        LearningAction.TRANSFER,
        LearningAction.MOVE_NEXT,
        LearningAction.FREE_CHAT,
    }
)


class LearningActionExecutor:
    """Executes a decided action against existing read-only services."""

    def __init__(
        self,
        *,
        store: Any | None = None,
        assessment_service: Any | None = None,
    ) -> None:
        self._store = store
        self._assessment_service = assessment_service

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def execute(
        self,
        recommendation: LearningRecommendation | None,
        *,
        course_id: str | None = None,
        course_name: str = "",
        session_id: str | None = None,
        next_label: str | None = None,
        run_assessment: bool = True,
    ) -> LearningActionResult:
        """Execute one recommendation; never raises.

        ``run_assessment=False`` makes PRACTICE a pure preparation (no provider
        call, no in-flight assessment) — the read-only path used for prompt
        injection.
        """
        if recommendation is None:
            return LearningActionResult(
                action=LearningAction.FREE_CHAT.value,
                status=ActionStatus.SKIPPED.value,
                message="",
            )
        try:
            return self._dispatch(
                recommendation,
                course_id=course_id,
                course_name=course_name,
                session_id=session_id,
                next_label=next_label,
                run_assessment=run_assessment,
            )
        except Exception as exc:  # noqa: BLE001 - an action must never raise
            return LearningActionResult(
                action=recommendation.action,
                status=ActionStatus.FAILED.value,
                message=f"（这一步没执行成功：{type(exc).__name__}）",
                source=recommendation.source,
                concept_id=recommendation.concept_id,
                concept_name=recommendation.concept_name,
            )

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        recommendation: LearningRecommendation,
        *,
        course_id: str | None,
        course_name: str,
        session_id: str | None,
        next_label: str | None,
        run_assessment: bool,
    ) -> LearningActionResult:
        action = recommendation.kind
        concept_name = recommendation.concept_name or "当前概念"

        if action is LearningAction.FREE_CHAT:
            return self._result(recommendation, ActionStatus.SKIPPED, "")

        if action in _READ_ONLY_ACTIONS:
            if action is LearningAction.MOVE_NEXT:
                return self._move_next(recommendation, next_label)
            return self._result(
                recommendation,
                ActionStatus.READY,
                CONSTRAINT_BY_ACTION[action].format(concept=concept_name),
            )

        if action is LearningAction.REVIEW:
            return self._review(recommendation)
        if action is LearningAction.PRACTICE:
            return self._practice(
                recommendation,
                course_id=course_id,
                course_name=course_name,
                session_id=session_id,
                run_assessment=run_assessment,
            )
        # Unknown action: degrade instead of raising.
        return self._result(
            recommendation, ActionStatus.DEGRADED, f"（不支持的动作：{action.value}）"
        )

    # -- individual actions ------------------------------------------------

    def _review(self, recommendation: LearningRecommendation) -> LearningActionResult:
        """Read the concept's review state (read-only)."""
        if self._store is None or not recommendation.concept_id:
            return self._result(
                recommendation,
                ActionStatus.DEGRADED,
                "该复习了，但暂时读不到复习项（按复习流程回顾上次要点即可）。",
            )
        item = self._due_review_item(recommendation.concept_id)
        if item is None:
            return self._result(
                recommendation,
                ActionStatus.DEGRADED,
                "当前没有到期的复习项，可以先回顾上次要点。",
            )
        interval = getattr(item, "interval_days", None)
        successes = getattr(item, "consecutive_success", None)
        details = []
        if interval:
            details.append(f"间隔 {interval} 天")
        if successes:
            details.append(f"连续成功 {successes} 次")
        suffix = f"（{'，'.join(details)}）" if details else ""
        return self._result(
            recommendation,
            ActionStatus.READY,
            f"「{recommendation.concept_name or '该概念'}」已到复习时间{suffix}："
            "先回顾上次要点，再检查是否还记得。",
        )

    def _practice(
        self,
        recommendation: LearningRecommendation,
        *,
        course_id: str | None,
        course_name: str,
        session_id: str | None,
        run_assessment: bool,
    ) -> LearningActionResult:
        """Drive the EXISTING AssessmentService (never grades, never writes)."""
        concept_name = recommendation.concept_name or "当前概念"
        if not run_assessment:
            # Read-only preparation: no provider call, no in-flight assessment.
            return self._result(
                recommendation,
                ActionStatus.READY,
                CONSTRAINT_BY_ACTION[LearningAction.PRACTICE].format(concept=concept_name),
            )
        if self._assessment_service is None or not course_id:
            return self._result(
                recommendation,
                ActionStatus.DEGRADED,
                "练习还没准备好（没有可用的测评服务），先按上面的引导作答。",
            )
        start = getattr(self._assessment_service, "start_assessment", None)
        if not callable(start):
            return self._result(
                recommendation,
                ActionStatus.DEGRADED,
                "练习还没准备好（测评服务不可用）。",
            )
        reply = start(
            course_id,
            course_name,
            requested_concept=recommendation.concept_name,
            session_id=session_id,
        )
        active = getattr(self._assessment_service, "active", None)
        if active is None:
            # The service answered with guidance (e.g. concept needs choosing)
            # instead of a question — surface it without pretending it started.
            return self._result(
                recommendation,
                ActionStatus.DEGRADED,
                str(reply or "暂时没能出题，稍后再试。"),
            )
        return self._result(recommendation, ActionStatus.STARTED, str(reply or ""))

    def _move_next(
        self, recommendation: LearningRecommendation, next_label: str | None
    ) -> LearningActionResult:
        """Read the curriculum's next node (passed in from LearningContext)."""
        label = (next_label or "").strip()
        if not label:
            return self._result(
                recommendation,
                ActionStatus.DEGRADED,
                "本章已掌握，但暂时读不到下一结构节点。",
            )
        return self._result(
            recommendation,
            ActionStatus.READY,
            f"本章已掌握：下一结构节点是「{label}」。",
        )

    # -- helpers -----------------------------------------------------------

    def _due_review_item(self, concept_id: str) -> Any | None:
        try:
            due = self._store.get_due_reviews()
        except Exception:  # noqa: BLE001 - review info is optional
            return None
        for item in due or ():
            if getattr(item, "concept_id", None) == concept_id:
                return item
        return None

    @staticmethod
    def _result(
        recommendation: LearningRecommendation,
        status: ActionStatus,
        message: str,
    ) -> LearningActionResult:
        return LearningActionResult(
            action=recommendation.action,
            status=status.value,
            message=message,
            source=recommendation.source,
            concept_id=recommendation.concept_id,
            concept_name=recommendation.concept_name,
        )


__all__ = ["LearningActionExecutor", "CONSTRAINT_BY_ACTION"]
