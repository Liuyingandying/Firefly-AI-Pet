"""Execution + user-confirmed completion for the Codex IMPLEMENT step (9D.6-H2).

This is the application layer that consumes a confirmed READY Step 2 and drives
it through a *managed* non-interactive Codex exec (an owned child process) exactly
as far as: capture a workspace baseline, mark the step RUNNING, read the PLAN
artifact, build the implement prompt (original task + PLAN), run
``codex exec --sandbox workspace-write --json --ephemeral`` through the existing
:class:`QuickAskRunner` / ``CodexJsonlAdapter`` AgentEvent path, and stay
RUNNING.

The old detached interactive handoff (9C, the Codex TUI) is NOT used here: it
requires a TTY and cannot be managed (9D.6 BLOCKER A). The Phase 9C
interactive ``Open Codex`` route (``ProcessLauncher.launch_agent``) is
untouched.

Managed-exec contract (9D.6-H2):
- The child process is owned by this executor's runner, so Firefly reads stdout,
  reads stderr, captures the exit code, cancels the process tree, and knows
  when the process actually finished. "Process spawned" is never treated as a
  managed execution state.
- Non-interactive ``codex exec`` auto-executes commands under the native
  ``workspace-write`` sandbox without a live per-command approval prompt. That
  tradeoff is accepted explicitly; the surrounding Firefly gates are preserved:
  Step 2 start requires user confirmation, and Step 2 completion requires a
  user confirmation with workspace evidence.
- Completing the step is a separate, explicit user action: scan the workspace
  again, diff against the baseline, and only then write the CHANGED_FILES +
  IMPLEMENTATION_SUMMARY artifacts and accept USER_CONFIRMED evidence. A managed
  exec finishing (exit 0 + AgentEvent FINAL + process finished) NEVER succeeds
  the step by itself — execution finished is not task success.
- A transport failure (process cannot start / nonzero exit / fatal
  AgentEvent.ERROR) fails the step; there is no auto retry and no fallback to
  interactive Codex or Claude.

The ``PlanStepExecutor`` / ``ReviewStepExecutor`` stay untouched; this module
owns only agent=codex / intent=IMPLEMENT / managed exec.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.agent_events import AgentEvent, AgentEventType, ErrorCategory
from core.artifact_store import ArtifactStore
from core.routing_models import HandoffMode
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
from core.workflow_models import (
    ArtifactKind,
    ArtifactRef,
    CompletionSource,
    StepCompletionEvidence,
    WorkflowPlan,
    WorkflowState,
    WorkflowStepIntent,
    WorkflowStepState,
)
from core.workflow_prompt import build_implement_prompt
from core.workspace_snapshot import (
    ChangeSet,
    WorkspaceSnapshotError,
    capture,
    changed_files_text,
    diff,
)
from ui.process_launcher import QuickAskRunner


class ImplementExecutionError(Exception):
    """The Implement step cannot be executed in its current shape/state."""


class ImplementStepBusy(ImplementExecutionError):
    """The executor is already carrying an active Implement execution."""


class CompletionAttempt:
    """Result of one user-initiated completion scan.

    ``ok`` True means the step advanced to SUCCEEDED. ``no_changes`` True means
    the scan produced an empty ChangeSet and the UI must ask for an explicit
    override before an empty CHANGED_FILES may be written. ``snapshot_error``
    True means the current workspace could not be scanned and the step is still
    RUNNING (the UI lets the user retry the scan).
    """

    __slots__ = ("ok", "no_changes", "snapshot_error", "message")

    def __init__(
        self,
        *,
        ok: bool,
        no_changes: bool = False,
        snapshot_error: bool = False,
        message: str = "",
    ) -> None:
        self.ok = ok
        self.no_changes = no_changes
        self.snapshot_error = snapshot_error
        self.message = message


class ImplementStepExecutor(QObject):
    """Executes the READY Codex IMPLEMENT step via a managed exec; completion
    is user-confirmed.

    One executor carries at most one active Implement execution. The baseline
    snapshot lives in the executor's memory (not the UI card), so hiding the
    card never loses it. ``execute`` never starts a REVIEW step and never
    auto-completes: after the managed exec finishes the step stays RUNNING until
    the user explicitly confirms completion.

    ``turn_finished`` fires once the managed exec turn ends (success or
    failure); it says nothing about step success. ``execution_finished`` fires
    with the process exit code when the exec completed without a fatal error.
    """

    agent_event = Signal(object)  # passthrough of the managed AgentEvents
    execution_finished = Signal(int)  # exit_code when the managed exec ends cleanly
    turn_finished = Signal()  # the managed exec turn ended (success or failure)

    def __init__(
        self,
        coordinator: WorkflowCoordinator,
        artifact_store: ArtifactStore,
        runner: QuickAskRunner | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._coordinator = coordinator
        self._store = artifact_store
        # TRANSIENT session policy (same as Plan/Review): a private in-memory
        # SessionManager with no store, so a workflow Implement can never read
        # or write the ordinary Short Talk sessions or config/sessions.json.
        # persistent=False adds --ephemeral and no session persistence on Codex.
        self._runner = runner if runner is not None else QuickAskRunner(session_manager=None, parent=self)
        self._runner.agent_event.connect(self._on_agent_event)
        self._runner.finished.connect(self._on_runner_finished)
        self._runner.failed.connect(self._on_runner_failed)

        self._busy = False
        self._plan: WorkflowPlan | None = None
        self._workflow_id: str | None = None
        self._step_id: str | None = None
        self._baseline = None
        self._completion_attempted = False
        self._execution_finished = False
        self._execution_exit_code: int | None = None
        self._handled = False
        self._saw_error = False
        self._cancelled = False
        self._run_token: tuple[str, str] | None = None

    # -- public -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._busy

    @property
    def plan(self) -> WorkflowPlan | None:
        return self._plan

    @property
    def managed_exec_finished(self) -> bool:
        """True when the managed exec completed without a fatal error.

        Still NOT step success: completion additionally requires the user
        confirmation + workspace evidence path (``confirm_completion``).
        """
        return self._execution_finished

    @property
    def managed_exec_exit_code(self) -> int | None:
        return self._execution_exit_code

    def execute(self, plan: WorkflowPlan, step_id: str) -> WorkflowPlan:
        """Run one confirmed READY Codex IMPLEMENT step; returns latest plan.

        Pre-start validation failures raise; launch-time failures mark the step
        FAILED (and the workflow FAILED) through the coordinator and return the
        failed plan, exactly like the Plan executor's spawn-failure path.
        """
        if self._busy:
            raise ImplementStepBusy("an Implement execution is already active")
        if not isinstance(plan, WorkflowPlan):
            raise ImplementExecutionError("execute requires a WorkflowPlan")
        if plan.state in (WorkflowState.SUCCEEDED, WorkflowState.FAILED, WorkflowState.CANCELLED):
            raise ImplementExecutionError(
                f"workflow {plan.workflow_id} is already {plan.state.value}"
            )
        idx = self._find_step_index(plan, step_id)  # unknown step id -> error
        current = plan.steps[plan.current_step_index]
        if current.step_id != step_id:
            raise ImplementExecutionError(f"step {step_id!r} is not the current step")
        step = plan.steps[idx]
        if step.state != WorkflowStepState.READY:
            raise ImplementExecutionError(
                f"step {step_id!r} is {step.state.value}; only READY steps execute"
            )
        self._validate_supported(step_id, step)
        workspace = plan.workspace
        if not workspace:
            raise ImplementExecutionError("workflow step requires a workspace")

        try:
            plan = self._coordinator.mark_step_started(plan, step_id)
        except WorkflowTransitionError as exc:
            raise ImplementExecutionError(str(exc))

        # Baseline must exist before any handoff so a later diff has a source.
        try:
            baseline = capture(workspace)
        except WorkspaceSnapshotError:
            self._fail(plan, step_id, "workspace_scan_failed")
            return self._plan

        plan_ref = self._find_plan_ref(plan)
        if plan_ref is None:
            self._fail(plan, step_id, "plan_artifact_missing")
            return self._plan
        try:
            plan_text = self._store.read_text(plan_ref)
        except Exception:
            self._fail(plan, step_id, "plan_artifact_unreadable")
            return self._plan

        try:
            prompt = build_implement_prompt(plan.original_request, plan_text)
        except ValueError:
            self._fail(plan, step_id, "invalid_task")
            return self._plan

        self._plan = plan
        self._workflow_id = plan.workflow_id
        self._step_id = step_id
        self._baseline = baseline
        self._completion_attempted = False
        self._execution_finished = False
        self._execution_exit_code = None
        self._handled = False
        self._saw_error = False
        self._cancelled = False
        self._run_token = (plan.workflow_id, step_id)
        self._busy = True

        ok = self._runner.ask(
            agent="codex",
            prompt=prompt,
            workspace=workspace,
            effort="low",
            persistent=False,
            isolated=False,
            sandbox="workspace-write",
            reasoning_effort=False,
        )
        if not ok:
            # Launch failure (CLI missing / already running): Step 2 FAILED,
            # workflow FAILED. No artifacts are created, nothing is retried.
            self._fail(plan, step_id, "launch_failed")
            return self._plan
        return self._plan  # RUNNING; the managed exec owns the process

    def stop(self) -> None:
        """Cancel the managed exec: stop the owned runner (process-tree kill).

        Unrelated processes are never touched; the runner only kills the
        process tree it owns for this execution.
        """
        if self._busy and self._runner is not None:
            self._runner.stop()

    def confirm_completion(
        self,
        plan: WorkflowPlan,
        step_id: str,
        *,
        override_empty: bool = False,
    ) -> CompletionAttempt:
        """User confirmed completion: scan workspace, write artifacts, succeed.

        An empty ChangeSet (no explicit override) does NOT succeed the step:
        the step stays RUNNING so the UI can ask for an explicit override
        before an empty CHANGED_FILES may be written.
        """
        if not self._busy or self._plan is None or self._baseline is None:
            return CompletionAttempt(ok=False, message="no active Implement execution")
        if plan.workflow_id != self._workflow_id or step_id != self._step_id:
            return CompletionAttempt(ok=False, message="workflow/step does not match the execution")
        if self._completion_attempted:
            return CompletionAttempt(ok=False, message="completion already handled")
        idx = self._find_step_index(plan, step_id)
        step = plan.steps[idx]
        if step.state != WorkflowStepState.RUNNING:
            return CompletionAttempt(ok=False, message=f"step is {step.state.value}")

        try:
            current = capture(plan.workspace)
        except WorkspaceSnapshotError:
            # A scan I/O failure must not fail the whole workflow or the
            # already-finished Codex work: keep RUNNING and let the UI retry.
            return CompletionAttempt(
                ok=False,
                snapshot_error=True,
                message="Couldn't inspect workspace changes.",
            )
        changes = diff(self._baseline, current)
        if changes.is_empty and not override_empty:
            return CompletionAttempt(
                ok=False,
                no_changes=True,
                message="No workspace changes were detected.",
            )

        # Write both artifacts first; attach only after both exist on disk.
        try:
            cf_ref = self._store.write_text(
                plan.workflow_id, ArtifactKind.CHANGED_FILES, step_id, changed_files_text(changes)
            )
        except Exception:
            return CompletionAttempt(ok=False, message="Couldn't write CHANGED_FILES.")
        plan_ref = self._find_plan_ref(plan)
        if plan_ref is None:
            self._store.remove_artifact(cf_ref)
            return CompletionAttempt(ok=False, message="PLAN artifact is not attached.")
        try:
            summary_ref = self._store.write_text(
                plan.workflow_id,
                ArtifactKind.IMPLEMENTATION_SUMMARY,
                step_id,
                self._summary_text(plan, changes, plan_ref),
            )
        except Exception:
            # Roll back the first artifact so no half-attached pair remains.
            self._store.remove_artifact(cf_ref)
            return CompletionAttempt(ok=False, message="Couldn't write IMPLEMENTATION_SUMMARY.")

        self._completion_attempted = True
        try:
            plan = self._coordinator.attach_artifact(plan, step_id, cf_ref)
            plan = self._coordinator.attach_artifact(plan, step_id, summary_ref)
            plan = self._coordinator.mark_step_succeeded(
                plan,
                step_id,
                StepCompletionEvidence(
                    source=CompletionSource.USER_CONFIRMED,
                    summary="User confirmed the Codex implementation step completed.",
                ),
            )
        except Exception:
            # Defensive: the gates were validated above, so this is unexpected.
            # Roll back the files and drop the execution state so the executor
            # can never stay locked; the workflow remains RUNNING for the user.
            self._store.remove_artifact(cf_ref)
            self._store.remove_artifact(summary_ref)
            self._plan = plan
            self._reset_run(plan)
            return CompletionAttempt(ok=False, message="completion transition failed.")

        self._plan = plan
        self._reset_run(plan)
        return CompletionAttempt(ok=True, message="Implementation confirmed.")

    def reset(self) -> None:
        """Drop the in-memory execution state (used on workflow cancel).

        Never touches the coordinator and never claims to stop the managed
        Codex process; ``stop()`` owns the actual process-tree cancel.
        """
        self._reset_run(None)

    # -- validation -------------------------------------------------------

    @staticmethod
    def _validate_supported(step_id: str, step) -> None:
        if step.agent_id != "codex":
            raise ImplementExecutionError(
                f"step {step_id!r} agent {step.agent_id!r} is not an Implement step"
            )
        if step.intent != WorkflowStepIntent.IMPLEMENT:
            raise ImplementExecutionError(
                f"step {step_id!r} intent {step.intent.value!r} is not an Implement step"
            )
        if step.handoff_mode != HandoffMode.OPEN_NATIVE:
            raise ImplementExecutionError(
                f"step {step_id!r} handoff {step.handoff_mode.value!r} is not native"
            )

    @staticmethod
    def _find_step_index(plan: WorkflowPlan, step_id: str) -> int:
        for i, s in enumerate(plan.steps):
            if s.step_id == step_id:
                return i
        raise ImplementExecutionError(f"unknown step {step_id!r}")

    @staticmethod
    def _find_attached(plan: WorkflowPlan, step_id: str, kind: ArtifactKind) -> ArtifactRef | None:
        for step in plan.steps:
            if step.step_id == step_id:
                for ref in step.attached_artifacts:
                    if ref.kind == kind:
                        return ref
        return None

    @staticmethod
    def _find_plan_ref(plan: WorkflowPlan) -> ArtifactRef | None:
        for step in plan.steps:
            for ref in step.attached_artifacts:
                if ref.kind == ArtifactKind.PLAN:
                    return ref
        return None

    # -- managed exec event consumption -----------------------------------

    def _on_agent_event(self, ev: AgentEvent) -> None:
        if not isinstance(ev, AgentEvent) or self._handled:
            return
        if ev.type == AgentEventType.ERROR:
            if ev.error_code != ErrorCategory.PROTOCOL:
                self._saw_error = True
        elif ev.type == AgentEventType.CANCELLED:
            self._cancelled = True
        self.agent_event.emit(ev)

    def _on_runner_finished(self, text: str, exit_code: int) -> None:
        if self._handled:
            return
        plan, step_id = self._plan, self._step_id
        if plan is None or step_id is None:
            return
        if self._run_token != (plan.workflow_id, step_id):
            return  # stale callback from a previous, cancelled run
        self._handled = True
        if self._cancelled:
            self._cancel(plan)
            return
        if self._saw_error or exit_code != 0:
            self._fail(plan, step_id, "agent_error" if self._saw_error else "process_exit")
            return
        # Managed exec completed without a transport-level fatal error. Step 2
        # stays RUNNING: completion still requires user confirmation + workspace
        # evidence. execution finished != task success.
        self._execution_finished = True
        self._execution_exit_code = int(exit_code)
        self.execution_finished.emit(int(exit_code))
        self.turn_finished.emit()

    def _on_runner_failed(self, _message: str) -> None:
        if self._handled:
            return
        plan, step_id = self._plan, self._step_id
        if plan is None or step_id is None:
            return
        if self._run_token != (plan.workflow_id, step_id):
            return
        self._handled = True
        if self._cancelled:
            self._cancel(plan)
            return
        self._fail(plan, step_id, "agent_error")

    # -- coordinator transitions / state ----------------------------------

    def _fail(self, plan: WorkflowPlan, step_id: str, error_code: str) -> None:
        try:
            plan = self._coordinator.mark_step_failed(plan, step_id, error_code)
        except WorkflowTransitionError:
            pass
        self._plan = plan
        self._reset_run(plan)
        self.turn_finished.emit()

    def _cancel(self, plan: WorkflowPlan) -> None:
        try:
            plan = self._coordinator.cancel_workflow(plan)
        except WorkflowTransitionError:
            pass
        self._plan = plan
        self._reset_run(plan)
        self.turn_finished.emit()

    def _reset_run(self, plan: WorkflowPlan | None) -> None:
        """Clear the run state, keeping ``_plan`` set to ``plan``."""
        self._plan = plan
        self._workflow_id = None
        self._step_id = None
        self._baseline = None
        self._busy = False
        self._completion_attempted = False
        self._execution_finished = False
        self._execution_exit_code = None
        self._handled = True
        self._saw_error = False
        self._cancelled = False
        self._run_token = None

    # -- helpers ----------------------------------------------------------

    def _summary_text(
        self, plan: WorkflowPlan, changes: ChangeSet, plan_ref: ArtifactRef
    ) -> str:
        """Honest deterministic record; never a fabricated Codex transcript."""
        plan_reference = plan_ref.path
        digest = None
        if plan_ref.metadata and isinstance(plan_ref.metadata.get("sha256"), str):
            digest = plan_ref.metadata["sha256"]
        if digest:
            plan_reference = f"{plan_ref.path} (sha256 {digest[:12]})"
        lines = [
            "# Implementation Summary",
            "",
            "Task:",
            plan.original_request.text,
            "",
            "Plan:",
            plan_reference,
            "",
            "Completion:",
            "User confirmed the Codex implementation step completed.",
            "",
            "Changed files:",
        ]
        for path in (*changes.added, *changes.modified, *changes.deleted):
            lines.append(f"- {path}")
        if not (changes.added or changes.modified or changes.deleted):
            lines.append("- (none)")
        lines.extend(
            [
                "",
                "Evidence source:",
                "USER_CONFIRMED + WORKSPACE_SNAPSHOT",
            ]
        )
        return "\n".join(lines) + "\n"
