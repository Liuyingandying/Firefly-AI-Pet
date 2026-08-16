"""WorkflowCoordinator for Phase 9D.1 — logical workflow state machine only.

This module decides *what the next step is*, *whether a step may start*, *which
artifacts a step needs*, and *how a step's result transitions the workflow*. It
never launches an agent: no QProcess, no ProcessLauncher, no QuickAskRunner, no
Claude/Codex CLI, no provider JSON parsing, no UI, no permission approval, and
no writes to config/runtime. Confirmation and execution are strictly separated:
``confirm_step`` only moves a step to READY — a future executor (9D.2+) consumes
READY.

Completion semantics are conservative: a step succeeds only when (a) it was
actually RUNNING, (b) every ``expected_artifacts`` kind is attached, and (c) the
provided :class:`StepCompletionEvidence` source is allowed for that step. In
particular the Codex IMPLEMENT step cannot be completed by a managed/lifecycle
claim — no observer lifecycle is ever authoritative.

Transitions are immutable: every method returns a new :class:`WorkflowPlan`
(registered under its workflow_id) and emits deterministic
:class:`WorkflowEvent` instances to connected listeners.
"""

from __future__ import annotations

import dataclasses
from typing import Callable

from core.routing_models import (
    DEFAULT_CAPABILITY_REGISTRY,
    AgentCapability,
    CapabilityRegistry,
    HandoffMode,
    TaskRequest,
)
from core.workflow_models import (
    ArtifactKind,
    ArtifactRef,
    CompletionSource,
    StepCompletionEvidence,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowKind,
    WorkflowPlan,
    WorkflowState,
    WorkflowStep,
    WorkflowStepIntent,
    WorkflowStepState,
    _now_ms,
    make_workflow_id,
)

# Completion evidence accepted for a Claude managed step (Claude FINAL can be a
# MANAGED_AGENT_RESULT; user/artifact evidence always valid).
_CLAUDIECE_SOURCES = frozenset(
    {
        CompletionSource.MANAGED_AGENT_RESULT,
        CompletionSource.USER_CONFIRMED,
        CompletionSource.ARTIFACT_VALIDATED,
    }
)

# Completion evidence accepted for the Codex IMPLEMENT step. No managed Codex
# path is online-verified, and observer lifecycle is never authoritative: only
# user confirmation or validated artifact evidence may complete it.
_CODEX_IMPLEMENT_SOURCES = frozenset(
    {CompletionSource.USER_CONFIRMED, CompletionSource.ARTIFACT_VALIDATED}
)

_STEP_INTENT_CAPABILITY = {
    WorkflowStepIntent.PLAN: AgentCapability.ANALYSIS,
    WorkflowStepIntent.IMPLEMENT: AgentCapability.CODING,
    WorkflowStepIntent.REVIEW: AgentCapability.REVIEW,
}


class WorkflowTransitionError(ValueError):
    """An illegal workflow/step transition or a failed gate."""


class WorkflowCoordinator:
    """Stateful, Qt-free workflow orchestrator (logical transitions only)."""

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._registry = registry if registry is not None else DEFAULT_CAPABILITY_REGISTRY
        self._clock = clock if clock is not None else _now_ms
        self._listeners: list[Callable[[WorkflowEvent], None]] = []
        self._plans: dict[str, WorkflowPlan] = {}

    # -- listeners --------------------------------------------------------

    def connect(self, callback: Callable[[WorkflowEvent], None]) -> None:
        self._listeners.append(callback)

    def disconnect(self, callback: Callable[[WorkflowEvent], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _emit(self, event: WorkflowEvent) -> None:
        for listener in list(self._listeners):
            listener(event)

    # -- reads ------------------------------------------------------------

    def get_plan(self, workflow_id: str) -> WorkflowPlan | None:
        return self._plans.get(workflow_id)

    def get_current_step(self, plan: WorkflowPlan) -> WorkflowStep:
        return plan.steps[plan.current_step_index]

    def required_artifacts_satisfied(self, plan: WorkflowPlan, step_id: str) -> bool:
        _, step = self._find_step(plan, step_id)
        attached = {a.kind for s in plan.steps for a in s.attached_artifacts}
        return all(kind in attached for kind in step.required_artifacts)

    # -- create -----------------------------------------------------------

    def create_plan_implement_review(self, request: TaskRequest) -> WorkflowPlan:
        """Build the fixed PLAN_IMPLEMENT_REVIEW workflow.

        Claude Plan -> Codex Implement -> Claude Review. Every step is
        user-confirmed; step 1 starts AWAITING_CONFIRMATION and later steps
        advance to AWAITING_CONFIRMATION only after their predecessor succeeds
        with all required artifacts attached. Nothing is executed here.
        """
        if not isinstance(request, TaskRequest):
            raise WorkflowTransitionError("request must be a TaskRequest")
        if not (request.text or "").strip():
            raise WorkflowTransitionError("A workflow requires a non-empty task request.")
        now = self._clock()
        workflow_id = make_workflow_id()
        steps = (
            self._build_step(
                step_id="step_1",
                agent_id="claude",
                intent=WorkflowStepIntent.PLAN,
                handoff_mode=HandoffMode.SHORT_TALK,
                title="Plan",
                requires_confirmation=True,
                expected_artifacts=(ArtifactKind.PLAN,),
                required_artifacts=(),
                completion_sources=_CLAUDIECE_SOURCES,
                state=WorkflowStepState.AWAITING_CONFIRMATION,
                now=now,
            ),
            self._build_step(
                step_id="step_2",
                agent_id="codex",
                intent=WorkflowStepIntent.IMPLEMENT,
                handoff_mode=HandoffMode.OPEN_NATIVE,
                title="Implement",
                requires_confirmation=True,
                expected_artifacts=(
                    ArtifactKind.IMPLEMENTATION_SUMMARY,
                    ArtifactKind.CHANGED_FILES,
                ),
                required_artifacts=(ArtifactKind.PLAN,),
                completion_sources=_CODEX_IMPLEMENT_SOURCES,
                state=WorkflowStepState.PENDING,
                now=now,
            ),
            self._build_step(
                step_id="step_3",
                agent_id="claude",
                intent=WorkflowStepIntent.REVIEW,
                handoff_mode=HandoffMode.SHORT_TALK,
                title="Review",
                requires_confirmation=True,
                expected_artifacts=(ArtifactKind.REVIEW,),
                required_artifacts=(ArtifactKind.PLAN, ArtifactKind.CHANGED_FILES),
                completion_sources=_CLAUDIECE_SOURCES,
                state=WorkflowStepState.PENDING,
                now=now,
            ),
        )
        plan = WorkflowPlan(
            workflow_id=workflow_id,
            kind=WorkflowKind.PLAN_IMPLEMENT_REVIEW,
            original_request=request,
            workspace=request.workspace,
            steps=steps,
            state=WorkflowState.WAITING_FOR_USER,
            current_step_index=0,
            created_at=now,
            updated_at=now,
        )
        self._plans[workflow_id] = plan
        self._emit(
            WorkflowEvent(
                workflow_id=workflow_id,
                type=WorkflowEventType.WORKFLOW_CREATED,
                timestamp=now,
                step_index=-1,
            )
        )
        self._emit(
            WorkflowEvent(
                workflow_id=workflow_id,
                type=WorkflowEventType.STEP_CONFIRMATION_REQUIRED,
                timestamp=now,
                step_id=steps[0].step_id,
                step_index=0,
                agent_id=steps[0].agent_id,
            )
        )
        return plan

    def _build_step(
        self,
        *,
        step_id: str,
        agent_id: str,
        intent: WorkflowStepIntent,
        handoff_mode: HandoffMode,
        title: str,
        requires_confirmation: bool,
        expected_artifacts: tuple[ArtifactKind, ...],
        required_artifacts: tuple[ArtifactKind, ...],
        completion_sources: frozenset[CompletionSource],
        state: WorkflowStepState,
        now: int,
    ) -> WorkflowStep:
        self._validate_capabilities(agent_id, intent, handoff_mode)
        return WorkflowStep(
            step_id=step_id,
            agent_id=agent_id,
            intent=intent,
            handoff_mode=handoff_mode,
            requires_confirmation=requires_confirmation,
            state=state,
            created_at=now,
            updated_at=now,
            title=title,
            expected_artifacts=expected_artifacts,
            required_artifacts=required_artifacts,
            completion_sources=completion_sources,
        )

    def _validate_capabilities(
        self,
        agent_id: str,
        intent: WorkflowStepIntent,
        handoff_mode: HandoffMode,
    ) -> None:
        """Static capability check only. Live state (busy, session) is never read."""
        profile = self._registry.profile(agent_id)
        if profile is None:
            raise WorkflowTransitionError(f"Unknown agent for workflow: {agent_id!r}")
        capability = _STEP_INTENT_CAPABILITY[intent]
        if not profile.has(capability):
            raise WorkflowTransitionError(
                f"Agent {agent_id!r} lacks capability {capability.value!r} for a {intent.value} step"
            )
        if handoff_mode == HandoffMode.SHORT_TALK and not profile.managed_short_talk:
            raise WorkflowTransitionError(f"Agent {agent_id!r} has no managed Short Talk")
        if handoff_mode == HandoffMode.OPEN_NATIVE and not profile.has(
            AgentCapability.NATIVE_LAUNCH
        ):
            raise WorkflowTransitionError(f"Agent {agent_id!r} lacks native launch")

    # -- transitions ------------------------------------------------------

    def request_confirmation(self, plan: WorkflowPlan) -> WorkflowPlan:
        """Advance the current PENDING step to AWAITING_CONFIRMATION.

        In the PLAN_IMPLEMENT_REVIEW template this is already done at creation
        (step 1) and after each success (next step); the method exists for
        flows that start a workflow with the current step still PENDING.
        """
        self._assert_not_terminal(plan)
        idx = plan.current_step_index
        step = plan.steps[idx]
        if step.state == WorkflowStepState.AWAITING_CONFIRMATION:
            return plan
        if step.state != WorkflowStepState.PENDING:
            raise WorkflowTransitionError(
                f"Step {step.step_id} is {step.state.value}; cannot request confirmation"
            )
        if not self.required_artifacts_satisfied(plan, step.step_id):
            raise WorkflowTransitionError(
                f"Step {step.step_id} requires artifacts that are not attached"
            )
        now = self._clock()
        new_plan = self._replace_step(
            plan, idx, now=now, state=WorkflowStepState.AWAITING_CONFIRMATION
        )
        self._register(new_plan)
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.STEP_CONFIRMATION_REQUIRED,
                timestamp=now,
                step_id=step.step_id,
                step_index=idx,
                agent_id=step.agent_id,
            )
        )
        return new_plan

    def confirm_step(self, plan: WorkflowPlan, step_id: str) -> WorkflowPlan:
        """AWAITING_CONFIRMATION -> READY. Never starts an agent."""
        self._assert_not_terminal(plan)
        idx, step = self._find_step(plan, step_id)
        if step.state != WorkflowStepState.AWAITING_CONFIRMATION:
            raise WorkflowTransitionError(
                f"Step {step_id} is {step.state.value}; only AWAITING_CONFIRMATION can be confirmed"
            )
        if not step.requires_confirmation:
            raise WorkflowTransitionError(f"Step {step_id} does not require confirmation")
        if not self.required_artifacts_satisfied(plan, step_id):
            raise WorkflowTransitionError(
                f"Step {step_id} requires artifacts that are not attached"
            )
        now = self._clock()
        new_plan = self._replace_step(plan, idx, now=now, state=WorkflowStepState.READY)
        self._register(new_plan)
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.STEP_READY,
                timestamp=now,
                step_id=step.step_id,
                step_index=idx,
                agent_id=step.agent_id,
            )
        )
        return new_plan

    def mark_step_started(self, plan: WorkflowPlan, step_id: str) -> WorkflowPlan:
        """READY -> RUNNING. The executor layer consumes READY in a later phase."""
        self._assert_not_terminal(plan)
        idx, step = self._find_step(plan, step_id)
        if step.state != WorkflowStepState.READY:
            raise WorkflowTransitionError(
                f"Step {step_id} is {step.state.value}; only READY can start"
            )
        now = self._clock()
        new_plan = self._replace_step(plan, idx, now=now, state=WorkflowStepState.RUNNING)
        self._register(new_plan)
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.STEP_STARTED,
                timestamp=now,
                step_id=step.step_id,
                step_index=idx,
                agent_id=step.agent_id,
            )
        )
        return new_plan

    def attach_artifact(
        self, plan: WorkflowPlan, step_id: str, artifact: ArtifactRef
    ) -> WorkflowPlan:
        """Attach one of the step's expected output artifacts while it runs."""
        self._assert_not_terminal(plan)
        idx, step = self._find_step(plan, step_id)
        if not isinstance(artifact, ArtifactRef):
            raise WorkflowTransitionError("artifact must be an ArtifactRef")
        if step.state != WorkflowStepState.RUNNING:
            raise WorkflowTransitionError(
                f"Step {step_id} is {step.state.value}; artifacts attach only while RUNNING"
            )
        if artifact.producer_step_id != step_id:
            raise WorkflowTransitionError("artifact producer does not match the step")
        if artifact.kind not in step.expected_artifacts:
            raise WorkflowTransitionError(
                f"Step {step_id} does not expect artifact kind {artifact.kind.value}"
            )
        if any(a.artifact_id == artifact.artifact_id for a in step.attached_artifacts):
            raise WorkflowTransitionError(f"Artifact {artifact.artifact_id} is already attached")
        now = self._clock()
        new_step = dataclasses.replace(
            step,
            attached_artifacts=step.attached_artifacts + (artifact,),
            updated_at=now,
        )
        steps = list(plan.steps)
        steps[idx] = new_step
        new_plan = dataclasses.replace(plan, steps=tuple(steps), updated_at=now)
        self._register(new_plan)
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.ARTIFACT_ATTACHED,
                timestamp=now,
                step_id=step.step_id,
                step_index=idx,
                agent_id=step.agent_id,
                artifact=artifact,
            )
        )
        return new_plan

    def mark_step_succeeded(
        self,
        plan: WorkflowPlan,
        step_id: str,
        evidence: StepCompletionEvidence,
    ) -> WorkflowPlan:
        """RUNNING -> SUCCEEDED, gated on artifacts and allowed evidence.

        On success of a non-final step, the next step advances to
        AWAITING_CONFIRMATION (its required artifacts must already be
        attached); the workflow returns to WAITING_FOR_USER. The final step
        transitions the workflow to SUCCEEDED.
        """
        self._assert_not_terminal(plan)
        idx, step = self._find_step(plan, step_id)
        if step.state != WorkflowStepState.RUNNING:
            raise WorkflowTransitionError(
                f"Step {step_id} is {step.state.value}; only RUNNING can succeed"
            )
        if not isinstance(evidence, StepCompletionEvidence):
            raise WorkflowTransitionError("evidence must be a StepCompletionEvidence")
        if evidence.source not in step.completion_sources:
            raise WorkflowTransitionError(
                f"Completion source {evidence.source.value} is not allowed for step {step_id}"
            )
        missing = [
            kind.value
            for kind in step.expected_artifacts
            if not any(a.kind == kind for a in step.attached_artifacts)
        ]
        if missing:
            raise WorkflowTransitionError(
                f"Step {step_id} is missing expected artifacts: {missing}"
            )
        now = self._clock()
        steps = list(plan.steps)
        is_last = idx == len(steps) - 1
        next_step = None
        if not is_last:
            next_step = steps[idx + 1]
            attached = {a.kind for s in steps for a in s.attached_artifacts}
            if not all(kind in attached for kind in next_step.required_artifacts):
                raise WorkflowTransitionError(
                    f"Step {next_step.step_id} requires artifacts that are not attached"
                )
        steps[idx] = dataclasses.replace(step, state=WorkflowStepState.SUCCEEDED, updated_at=now)
        if is_last:
            new_plan = dataclasses.replace(
                plan,
                steps=tuple(steps),
                state=self._derive_workflow_state(tuple(steps)),
                updated_at=now,
            )
        else:
            steps[idx + 1] = dataclasses.replace(
                steps[idx + 1],
                state=WorkflowStepState.AWAITING_CONFIRMATION,
                updated_at=now,
            )
            new_plan = dataclasses.replace(
                plan,
                steps=tuple(steps),
                state=self._derive_workflow_state(tuple(steps)),
                current_step_index=idx + 1,
                updated_at=now,
            )
        self._register(new_plan)
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.STEP_SUCCEEDED,
                timestamp=now,
                step_id=step.step_id,
                step_index=idx,
                agent_id=step.agent_id,
                evidence=evidence,
            )
        )
        if is_last:
            self._emit(
                WorkflowEvent(
                    workflow_id=plan.workflow_id,
                    type=WorkflowEventType.WORKFLOW_SUCCEEDED,
                    timestamp=now,
                    step_index=-1,
                )
            )
        else:
            self._emit(
                WorkflowEvent(
                    workflow_id=plan.workflow_id,
                    type=WorkflowEventType.STEP_CONFIRMATION_REQUIRED,
                    timestamp=now,
                    step_id=next_step.step_id,
                    step_index=idx + 1,
                    agent_id=next_step.agent_id,
                )
            )
        return new_plan

    def mark_step_failed(
        self, plan: WorkflowPlan, step_id: str, error_code: str
    ) -> WorkflowPlan:
        """RUNNING -> FAILED; workflow FAILED; later PENDING steps SKIPPED.

        STOP_ON_FAILURE: no retry, no fallback agent, no continuation.
        """
        self._assert_not_terminal(plan)
        idx, step = self._find_step(plan, step_id)
        if step.state != WorkflowStepState.RUNNING:
            raise WorkflowTransitionError(
                f"Step {step_id} is {step.state.value}; only RUNNING can fail"
            )
        now = self._clock()
        steps = list(plan.steps)
        steps[idx] = dataclasses.replace(step, state=WorkflowStepState.FAILED, updated_at=now)
        for j in range(idx + 1, len(steps)):
            later = steps[j]
            if later.state == WorkflowStepState.PENDING:
                steps[j] = dataclasses.replace(
                    later, state=WorkflowStepState.SKIPPED, updated_at=now
                )
        new_plan = dataclasses.replace(
            plan,
            steps=tuple(steps),
            state=self._derive_workflow_state(tuple(steps)),
            current_step_index=idx,
            updated_at=now,
        )
        self._register(new_plan)
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.STEP_FAILED,
                timestamp=now,
                step_id=step.step_id,
                step_index=idx,
                agent_id=step.agent_id,
                error_code=error_code,
            )
        )
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.WORKFLOW_FAILED,
                timestamp=now,
                step_index=-1,
                error_code=error_code,
            )
        )
        return new_plan

    def cancel_workflow(self, plan: WorkflowPlan) -> WorkflowPlan:
        """Cancel the workflow from a non-terminal state.

        The current step (PENDING / AWAITING_CONFIRMATION / READY / RUNNING)
        becomes CANCELLED; later PENDING steps become SKIPPED. No process is
        killed here — an executor owns real cancel in a later phase.
        """
        self._assert_not_terminal(plan)
        idx = plan.current_step_index
        step = plan.steps[idx]
        now = self._clock()
        steps = list(plan.steps)
        if step.state in (
            WorkflowStepState.PENDING,
            WorkflowStepState.AWAITING_CONFIRMATION,
            WorkflowStepState.READY,
            WorkflowStepState.RUNNING,
        ):
            steps[idx] = dataclasses.replace(step, state=WorkflowStepState.CANCELLED, updated_at=now)
        for j in range(idx + 1, len(steps)):
            later = steps[j]
            if later.state == WorkflowStepState.PENDING:
                steps[j] = dataclasses.replace(
                    later, state=WorkflowStepState.SKIPPED, updated_at=now
                )
        new_plan = dataclasses.replace(
            plan,
            steps=tuple(steps),
            state=self._derive_workflow_state(tuple(steps)),
            updated_at=now,
        )
        self._register(new_plan)
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.STEP_CANCELLED,
                timestamp=now,
                step_id=step.step_id,
                step_index=idx,
                agent_id=step.agent_id,
            )
        )
        self._emit(
            WorkflowEvent(
                workflow_id=plan.workflow_id,
                type=WorkflowEventType.WORKFLOW_CANCELLED,
                timestamp=now,
                step_index=-1,
            )
        )
        return new_plan

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _find_step(plan: WorkflowPlan, step_id: str) -> tuple[int, WorkflowStep]:
        for i, step in enumerate(plan.steps):
            if step.step_id == step_id:
                return i, step
        raise WorkflowTransitionError(f"Unknown step {step_id!r} in workflow {plan.workflow_id}")

    def _replace_step(self, plan: WorkflowPlan, index: int, *, now: int, **changes) -> WorkflowPlan:
        steps = list(plan.steps)
        steps[index] = dataclasses.replace(steps[index], updated_at=now, **changes)
        return dataclasses.replace(
            plan,
            steps=tuple(steps),
            state=self._derive_workflow_state(tuple(steps)),
            updated_at=now,
        )

    @staticmethod
    def _derive_workflow_state(steps: tuple[WorkflowStep, ...]) -> WorkflowState:
        """Derive the workflow-level state from the step states.

        Terminal outcomes first, then any running step, otherwise the workflow
        is waiting for the next user action.
        """
        if any(s.state == WorkflowStepState.CANCELLED for s in steps):
            return WorkflowState.CANCELLED
        if any(s.state == WorkflowStepState.FAILED for s in steps):
            return WorkflowState.FAILED
        if all(s.state == WorkflowStepState.SUCCEEDED for s in steps):
            return WorkflowState.SUCCEEDED
        if any(s.state == WorkflowStepState.RUNNING for s in steps):
            return WorkflowState.RUNNING
        return WorkflowState.WAITING_FOR_USER

    def _register(self, plan: WorkflowPlan) -> None:
        self._plans[plan.workflow_id] = plan

    @staticmethod
    def _assert_not_terminal(plan: WorkflowPlan) -> None:
        if plan.state in (WorkflowState.SUCCEEDED, WorkflowState.FAILED, WorkflowState.CANCELLED):
            raise WorkflowTransitionError(f"Workflow {plan.workflow_id} is already {plan.state.value}")
