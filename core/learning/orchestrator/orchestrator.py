"""LearningLoopOrchestrator (Phase 6).

Composes the already-existing learning layers into ONE turn:

    1. detect the learning request          (controller.handle_text — also
                                             records the allowed interactions
                                             and starts the check-understanding
                                             flow when explicitly asked)
    2. read LearningContext                 (controller.learning_context)
    3. generate TeachingContext             (controller.teaching_context)
    4. call the DecisionPolicy              (controller.learning_decision)
    5. call the ActionExecutor              (controller.learning_action)
    6. return the result

The orchestrator is pure flow composition. It decides nothing itself (the
decision comes from the Phase 4 policy), never judges learning state (mastery is
only read), and never writes mastery / rule-engine / curriculum state. It
imports NO provider, LLM, rule engine or curriculum writer — the controller is
injected and used through its existing read-only interface.

Two entry points:

    run(user_message)      the full loop for a turn OWNER (console / tests);
                           performs detection + recording via handle_text
    loop_block()           READ-ONLY composition of the context blocks
                           (context / teaching / decision / action); used by the
                           runner for per-turn prompt injection — it never calls
                           handle_text, so it can never double-record or
                           double-start an assessment
"""

from __future__ import annotations

from typing import Any

from core.learning.orchestrator.models import LoopSource, LoopStatus, LearningLoopResult


class LearningLoopOrchestrator:
    """Flow composition over the existing learning layers (injected controller)."""

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    # ------------------------------------------------------------------
    # full loop (for turn owners)
    # ------------------------------------------------------------------

    def run(self, user_message: str, *, run_assessment: bool = False) -> LearningLoopResult:
        """Run one learning turn for ``user_message``; never raises.

        ``run_assessment`` is forwarded to the action executor; the default
        (False) keeps the loop free of provider calls.
        """
        text = (user_message or "").strip()
        reply: str | None = None
        try:
            handler = getattr(self._controller, "handle_text", None)
            if callable(handler) and text:
                reply = handler(text)
        except Exception:  # noqa: BLE001 - the loop must survive controller faults
            return self._degraded("learning request detection failed")

        if reply is not None:
            status = LoopStatus.LEARNING_COMMAND
            source: Any = LoopSource.LEARNING_COMMAND
        else:
            status = LoopStatus.LEARNING_LOOP if self._mode_enabled() else LoopStatus.ORDINARY
            source = LoopSource.FREE_CHAT

        blocks, decision, action_result = self._prepare(read_only=not run_assessment)
        if decision is not None:
            source = getattr(decision, "source", None) or source

        # Phase 9B-1: the loop carries the four contexts it computed so the
        # runner can build the response contract without recomputation.
        learning_context = None
        teaching_context = None
        context_reader = getattr(self._controller, "learning_context", None)
        if callable(context_reader):
            try:
                learning_context = context_reader()
            except Exception:  # noqa: BLE001 - contexts are optional payload
                learning_context = None
        teaching_reader = getattr(self._controller, "teaching_context", None)
        if callable(teaching_reader):
            try:
                teaching_context = teaching_reader()
            except Exception:  # noqa: BLE001
                teaching_context = None

        bootstrap = None
        if (status is not LoopStatus.ORDINARY
                and decision is not None
                and getattr(decision, "action", None) == "free_chat"):
            # Phase 9C: a fresh course cannot enter course learning on its own
            # — attach the bootstrap (start chapter / first task) to the turn.
            bootstrap_reader = getattr(self._controller, "learning_bootstrap", None)
            if callable(bootstrap_reader):
                try:
                    bootstrap = bootstrap_reader()
                except Exception:  # noqa: BLE001 - bootstrap is optional
                    bootstrap = None

        if status is LoopStatus.ORDINARY:
            return LearningLoopResult(
                status=status.value,
                source=LoopSource.FREE_CHAT.value,
                response="",
                context_block=None,
                learning_context=learning_context,
                teaching_context=teaching_context,
                decision=decision,
                bootstrap=bootstrap,
            )

        response = reply if reply is not None else blocks
        action_result = None
        action_reader = getattr(self._controller, "learning_action", None)
        if callable(action_reader):
            try:
                action_result = action_reader(run_assessment=False)
            except Exception:  # noqa: BLE001
                action_result = None
        return LearningLoopResult(
            status=status.value,
            action=getattr(decision, "action", None) if decision is not None else None,
            response=response or "",
            source=source,
            context_block=blocks or None,
            learning_context=learning_context,
            teaching_context=teaching_context,
            decision=decision,
            action_result=action_result,
            bootstrap=bootstrap,
        )

    # ------------------------------------------------------------------
    # read-only composition (for the runner)
    # ------------------------------------------------------------------

    def loop_block(self) -> str | None:
        """Compose the read-only prompt blocks; never calls handle_text."""
        try:
            blocks, _decision, _action = self._prepare(read_only=True)
        except Exception:  # noqa: BLE001 - injection must never break a turn
            return None
        return blocks or None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _prepare(self, *, read_only: bool) -> tuple[str, Any | None, Any | None]:
        """Compose the context blocks; returns (block, decision, action_result)."""
        controller = self._controller
        blocks: list[str] = []

        learning_block = getattr(controller, "context_block", None)
        if callable(learning_block):
            block = learning_block()
            if block:
                blocks.append(block)

        teaching_block = getattr(controller, "teaching_block", None)
        if callable(teaching_block):
            block = teaching_block()
            if block:
                blocks.append(block)

        decision = None
        decision_method = getattr(controller, "learning_decision", None)
        if callable(decision_method):
            decision = decision_method()
            block = decision.context_block() if decision is not None else None
            if block:
                blocks.append(block)

        action_result = None
        action_method = getattr(controller, "learning_action", None)
        if callable(action_method):
            action_result = action_method(run_assessment=not read_only)
            block = getattr(action_result, "context_block", None) if action_result is not None else None
            block = block() if callable(block) else block
            if block:
                blocks.append(block)

        return ("\n\n".join(blocks) if blocks else ""), decision, action_result

    def _mode_enabled(self) -> bool:
        state = getattr(self._controller, "state", None)
        return bool(getattr(state, "enabled", False))

    def _degraded(self, message: str) -> LearningLoopResult:
        return LearningLoopResult(
            status=LoopStatus.DEGRADED.value,
            source=LoopSource.FREE_CHAT.value,
            response=message,
        )


__all__ = ["LearningLoopOrchestrator"]
