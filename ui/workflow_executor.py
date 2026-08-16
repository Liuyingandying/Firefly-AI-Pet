"""Thin execution layer for workflow steps (Phase 9D.2 / 9D.6-H4).

The :class:`WorkflowCoordinator` stays Qt-free and provider-free; this module is
the opposite: it is the only place that consumes a READY step and drives a real
managed agent transport. It supports exactly one execution shape — a Claude
Plan step — and explicitly rejects every other agent/intent.

Transport policy (9D.6-H4): the Plan step runs through the direct
Anthropic-compatible provider (:class:`DirectProviderRunner`), NOT the Claude
Code CLI / :class:`QuickAskRunner` / ``run_cli.ps1``. That bypasses the Claude
Code compatibility layer that returned a generic "no task" response for the
same prompt. No Claude native session is created, so nothing is ever read from
or written to ``config/sessions.json``, and no Claude hook fires.

Completion follows existing AgentEvent semantics: a step succeeds only when
there is a usable agent result AND the PLAN artifact was written atomically.
A provider ERROR or an empty response fails the step; there is no automatic
retry and no fallback agent.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.agent_events import AgentEvent, AgentEventType, ErrorCategory
from core.artifact_store import ArtifactStore
from core.plan_validation import validate_plan_text
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
from core.workflow_prompt import build_plan_prompt
from ui.workflow_provider_runner import DirectProviderRunner


class WorkflowExecutionError(Exception):
    """A workflow step cannot be executed in its current shape/state."""


class UnsupportedStepExecution(WorkflowExecutionError):
    """The step's agent/intent/handoff is not supported by this phase's executor."""


class ExecutionBusy(WorkflowExecutionError):
    """The executor is already running one managed workflow step."""


class PlanStepExecutor(QObject):
    """Executes READY Claude PLAN steps via a transient managed Claude session.

    One executor runs at most one workflow step at a time. ``execute`` never
    starts Codex and never advances a step that the user has not confirmed.
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
        """Run one confirmed READY Claude PLAN step; returns the latest plan."""
        if self._busy:
            raise ExecutionBusy("a workflow step is already executing")
        if not isinstance(plan, WorkflowPlan):
            raise WorkflowExecutionError("execute requires a WorkflowPlan")
        if plan.state in (WorkflowState.SUCCEEDED, WorkflowState.FAILED, WorkflowState.CANCELLED):
            raise WorkflowExecutionError(
                f"workflow {plan.workflow_id} is already {plan.state.value}"
            )
        idx = self._find_step_index(plan, step_id)  # unknown step id -> error
        current = plan.steps[plan.current_step_index]
        if current.step_id != step_id:
            raise WorkflowExecutionError(f"step {step_id!r} is not the current step")
        step = plan.steps[idx]
        if step.state != WorkflowStepState.READY:
            raise WorkflowExecutionError(
                f"step {step_id!r} is {step.state.value}; only READY steps execute"
            )
        self._validate_supported(step_id, step)
        workspace = plan.workspace
        if not workspace:
            raise WorkflowExecutionError("workflow step requires a workspace")

        try:
            plan = self._coordinator.mark_step_started(plan, step_id)
        except WorkflowTransitionError as exc:
            raise WorkflowExecutionError(str(exc))

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

        try:
            prompt = build_plan_prompt(plan.original_request)
        except ValueError as exc:
            self._fail(plan, step_id, "invalid_task")
            return self._plan

        self.step_started.emit()
        ok = self._runner.ask(prompt=prompt)
        if not ok:
            self._fail(plan, step_id, "process_start_failed")
        return self._plan

    def stop(self) -> None:
        """Cancel the running managed step (best-effort, minimal 9D.2 scope)."""
        if self._busy and self._runner is not None:
            self._runner.stop()

    # -- validation -------------------------------------------------------

    @staticmethod
    def _validate_supported(step_id: str, step) -> None:
        if step.agent_id != "claude":
            raise UnsupportedStepExecution(
                f"step {step_id!r} agent {step.agent_id!r} is not supported in 9D.2"
            )
        if step.intent != WorkflowStepIntent.PLAN:
            raise UnsupportedStepExecution(
                f"step {step_id!r} intent {step.intent.value!r} is not supported in 9D.2"
            )
        if step.handoff_mode != HandoffMode.SHORT_TALK:
            raise UnsupportedStepExecution(
                f"step {step_id!r} handoff {step.handoff_mode.value!r} is not supported in 9D.2"
            )

    @staticmethod
    def _find_step_index(plan: WorkflowPlan, step_id: str) -> int:
        for i, s in enumerate(plan.steps):
            if s.step_id == step_id:
                return i
        raise WorkflowExecutionError(f"unknown step {step_id!r}")

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
            self._fail(plan, step_id, "no_agent_result")
            return
        plan_text = text if text.strip() else usable
        result = validate_plan_text(plan_text, plan.original_request.text)
        if not result.valid:
            # Deterministic gate: an obviously-invalid plan never becomes a
            # PLAN artifact and never reaches Codex. No retry, no fallback.
            self._fail(plan, step_id, "plan_invalid")
            return
        try:
            ref = self._store.write_text(
                plan.workflow_id, ArtifactKind.PLAN, step_id, plan_text
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
                    summary="Claude plan produced",
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
