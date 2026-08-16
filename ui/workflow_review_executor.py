"""Execution for the Claude REVIEW step of PLAN_IMPLEMENT_REVIEW (9D.5 / 9D.6-H4).

This is the application layer that consumes a confirmed READY Step 3 and drives
a real managed Claude review exactly as far as: mark the step RUNNING, verify
the cross-step PLAN + CHANGED_FILES artifacts (producer-validated), build the
read-only review context (original task + PLAN + CHANGED_FILES + optional
IMPLEMENTATION_SUMMARY + the current changed-file contents), and — only on a
usable FINAL — write the REVIEW artifact, attach it, and succeed the step.

Transport policy (9D.6-H4): the Review step runs through the direct
Anthropic-compatible provider (:class:`DirectProviderRunner`), NOT the Claude
Code CLI / :class:`QuickAskRunner`. Because the direct provider has no local
file tools, the changed-file contents are inlined via the deterministic
:class:`core.review_context` builder. No Claude native session is created, so
``config/sessions.json`` is never touched and no Claude hook fires.

Completion semantics: a Review step SUCCEEDED means a review was produced and
persisted — never that the code passed. The REVIEW artifact keeps the full
text; the verdict (PASS / NEEDS_CHANGES / UNCERTAIN) is never parsed here and
never drives a workflow transition. A provider ERROR or an empty response fails
the step; there is no automatic retry, no fallback agent, and no remediation
loop.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.agent_events import AgentEvent, AgentEventType, ErrorCategory
from core.artifact_store import ArtifactStore
from core.review_context import build_review_context
from core.routing_models import HandoffMode
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
from core.workflow_models import (
    ArtifactKind,
    CompletionSource,
    StepCompletionEvidence,
    WorkflowPlan,
    WorkflowState,
    WorkflowStepIntent,
    WorkflowStepState,
)
from core.workflow_prompt import build_review_prompt
from ui.workflow_provider_runner import DirectProviderRunner


class ReviewExecutionError(Exception):
    """The Review step cannot be executed in its current shape/state."""


class UnsupportedReviewExecution(ReviewExecutionError):
    """The step's agent/intent/handoff is not a supported Review shape."""


class ReviewExecutionBusy(ReviewExecutionError):
    """The executor is already running one Review step."""


class ReviewStepExecutor(QObject):
    """Executes READY Claude REVIEW steps via a transient managed Claude session.

    One executor runs at most one Review step at a time. ``execute`` never
    starts Codex, never re-runs a Plan or Implement step, and only advances a
    step that the user has already confirmed.
    """

    step_started = Signal()
    agent_event = Signal(object)  # passthrough of the managed AgentEvents
    turn_finished = Signal()

    def __init__(
        self,
        coordinator: WorkflowCoordinator,
        artifact_store: ArtifactStore,
        runner: DirectProviderRunner | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self._store = artifact_store
        # Direct-provider transport: no Claude CLI, no QuickAskRunner, no
        # SessionManager, no config/sessions.json, no Claude hooks.
        self._runner = runner if runner is not None else DirectProviderRunner(parent=self)
        self._runner.agent_event.connect(self._on_agent_event)
        self._runner.finished.connect(self._on_finished)
        self._runner.failed.connect(self._on_failed)

        self._busy = False
        self._plan: WorkflowPlan | None = None
        self._workflow_id: str | None = None
        self._step_id: str | None = None
        self._collected_deltas: list[str] = []
        self._final_text = ""
        self._saw_text = False
        self._saw_error = False
        self._cancelled = False
        self._completed = False

    # -- public -----------------------------------------------------------

    @property
    def plan(self) -> WorkflowPlan | None:
        return self._plan

    @property
    def running(self) -> bool:
        return self._busy

    def execute(self, plan: WorkflowPlan, step_id: str) -> WorkflowPlan:
        """Run one confirmed READY Claude REVIEW step; returns the latest plan."""
        if self._busy:
            raise ReviewExecutionBusy("a workflow review is already executing")
        if not isinstance(plan, WorkflowPlan):
            raise ReviewExecutionError("execute requires a WorkflowPlan")
        if plan.state in (WorkflowState.SUCCEEDED, WorkflowState.FAILED, WorkflowState.CANCELLED):
            raise ReviewExecutionError(
                f"workflow {plan.workflow_id} is already {plan.state.value}"
            )
        idx = self._find_step_index(plan, step_id)  # unknown step id -> error
        current = plan.steps[plan.current_step_index]
        if current.step_id != step_id:
            raise ReviewExecutionError(f"step {step_id!r} is not the current step")
        step = plan.steps[idx]
        if step.state != WorkflowStepState.READY:
            raise ReviewExecutionError(
                f"step {step_id!r} is {step.state.value}; only READY steps execute"
            )
        self._validate_supported(step_id, step)
        workspace = plan.workspace
        if not workspace:
            raise ReviewExecutionError("workflow step requires a workspace")

        try:
            plan = self._coordinator.mark_step_started(plan, step_id)
        except WorkflowTransitionError as exc:
            raise ReviewExecutionError(str(exc))

        # Artifact-first review context. Never trust an arbitrary path: the
        # PLAN must exist as a Step-1 artifact and CHANGED_FILES as a Step-2
        # artifact before any Claude call.
        plan_ref = self._find_artifact(plan, ArtifactKind.PLAN, WorkflowStepIntent.PLAN)
        if plan_ref is None:
            self._fail(plan, step_id, "plan_artifact_missing")
            return self._plan
        changed_ref = self._find_artifact(
            plan, ArtifactKind.CHANGED_FILES, WorkflowStepIntent.IMPLEMENT
        )
        if changed_ref is None:
            self._fail(plan, step_id, "changed_files_artifact_missing")
            return self._plan
        try:
            plan_text = self._store.read_text(plan_ref)
            changed_text = self._store.read_text(changed_ref)
        except Exception:
            self._fail(plan, step_id, "artifact_unreadable")
            return self._plan
        summary_text: str | None = None
        summary_ref = self._find_artifact(
            plan, ArtifactKind.IMPLEMENTATION_SUMMARY, WorkflowStepIntent.IMPLEMENT
        )
        if summary_ref is not None:
            try:
                summary_text = self._store.read_text(summary_ref)
            except Exception:
                summary_text = None  # auxiliary only; never fails the review

        # The direct provider has no file tools, so inline the current changed
        # file contents (added/modified) and deleted-file markers. Only the
        # files named by CHANGED_FILES are read, with path/size safety.
        review_context = build_review_context(workspace, changed_text)

        try:
            prompt = build_review_prompt(
                plan.original_request,
                plan_text,
                changed_text,
                summary_text=summary_text,
                workspace_context=review_context.render(),
            )
        except ValueError:
            self._fail(plan, step_id, "invalid_task")
            return self._plan

        self._plan = plan
        self._workflow_id = plan.workflow_id
        self._step_id = step_id
        self._collected_deltas = []
        self._final_text = ""
        self._saw_text = False
        self._saw_error = False
        self._cancelled = False
        self._completed = False
        self._busy = True

        self.step_started.emit()
        ok = self._runner.ask(prompt=prompt)
        if not ok:
            self._fail(plan, step_id, "process_start_failed")
        return self._plan

    def stop(self) -> None:
        """Cancel the running managed review (best-effort, minimal 9D.5 scope)."""
        if self._busy and self._runner is not None:
            self._runner.stop()

    # -- validation -------------------------------------------------------

    @staticmethod
    def _validate_supported(step_id: str, step) -> None:
        if step.agent_id != "claude":
            raise UnsupportedReviewExecution(
                f"step {step_id!r} agent {step.agent_id!r} is not a Claude Review step"
            )
        if step.intent != WorkflowStepIntent.REVIEW:
            raise UnsupportedReviewExecution(
                f"step {step_id!r} intent {step.intent.value!r} is not a Review step"
            )
        if step.handoff_mode != HandoffMode.SHORT_TALK:
            raise UnsupportedReviewExecution(
                f"step {step_id!r} handoff {step.handoff_mode.value!r} is not Short Talk"
            )

    @staticmethod
    def _find_step_index(plan: WorkflowPlan, step_id: str) -> int:
        for i, s in enumerate(plan.steps):
            if s.step_id == step_id:
                return i
        raise ReviewExecutionError(f"unknown step {step_id!r}")

    @classmethod
    def _find_artifact(
        cls, plan: WorkflowPlan, kind: ArtifactKind, intent: WorkflowStepIntent
    ):
        """Find an attached artifact of ``kind`` produced by the step with intent."""
        producer = cls._producer_step_id(plan, intent)
        if producer is None:
            return None
        for step in plan.steps:
            for ref in step.attached_artifacts:
                if ref.kind == kind and ref.producer_step_id == producer:
                    return ref
        return None

    @staticmethod
    def _producer_step_id(plan: WorkflowPlan, intent: WorkflowStepIntent) -> str | None:
        for step in plan.steps:
            if step.intent == intent:
                return step.step_id
        return None

    # -- event consumption ------------------------------------------------

    def _on_agent_event(self, ev: AgentEvent) -> None:
        if not isinstance(ev, AgentEvent) or self._completed:
            return
        if ev.type == AgentEventType.TEXT_DELTA:
            if ev.text:
                self._collected_deltas.append(ev.text)
                self._saw_text = True
        elif ev.type == AgentEventType.FINAL:
            if ev.text and ev.text.strip():
                self._final_text = ev.text
                self._saw_text = True
        elif ev.type == AgentEventType.ERROR:
            if ev.error_code != ErrorCategory.PROTOCOL:
                self._saw_error = True
        elif ev.type == AgentEventType.CANCELLED:
            self._cancelled = True
        self.agent_event.emit(ev)

    def _usable_text(self) -> str | None:
        if self._final_text and self._final_text.strip():
            return self._final_text
        joined = "".join(self._collected_deltas).strip()
        return joined or None

    def _on_finished(self, text: str, exit_code: int) -> None:
        if self._completed:
            return
        self._completed = True
        self._busy = False
        plan, step_id = self._plan, self._step_id
        if plan is None or step_id is None:
            return
        if self._cancelled:
            self._cancel(plan)
            return
        if self._saw_error or exit_code != 0:
            self._fail(plan, step_id, "agent_error" if self._saw_error else "process_exit")
            return
        usable = self._usable_text()
        if usable is None:
            # process exit 0 but no usable response -> Review FAILED. An empty
            # review.md is never written.
            self._fail(plan, step_id, "no_agent_result")
            return
        review_text = text if text.strip() else usable
        try:
            ref = self._store.write_text(
                plan.workflow_id, ArtifactKind.REVIEW, step_id, review_text
            )
        except Exception:
            self._fail(plan, step_id, "artifact_write_failed")
            return
        try:
            plan = self._coordinator.attach_artifact(plan, step_id, ref)
            plan = self._coordinator.mark_step_succeeded(
                plan,
                step_id,
                StepCompletionEvidence(
                    source=CompletionSource.MANAGED_AGENT_RESULT,
                    summary="Claude review produced",
                ),
            )
        except Exception:
            self._fail(plan, step_id, "completion_failed")
            return
        self._plan = plan
        self._step_id = None
        self.turn_finished.emit()

    def _on_failed(self, _message: str) -> None:
        if self._completed:
            return
        self._completed = True
        self._busy = False
        plan, step_id = self._plan, self._step_id
        if plan is None or step_id is None:
            return
        if self._cancelled:
            self._cancel(plan)
            return
        self._fail(plan, step_id, "agent_error")

    # -- coordinator transitions ------------------------------------------

    def _fail(self, plan: WorkflowPlan, step_id: str, error_code: str) -> None:
        try:
            plan = self._coordinator.mark_step_failed(plan, step_id, error_code)
        except WorkflowTransitionError:
            pass
        self._plan = plan
        self._step_id = None
        self.turn_finished.emit()

    def _cancel(self, plan: WorkflowPlan) -> None:
        try:
            plan = self._coordinator.cancel_workflow(plan)
        except WorkflowTransitionError:
            pass
        self._plan = plan
        self._step_id = None
        self.turn_finished.emit()
