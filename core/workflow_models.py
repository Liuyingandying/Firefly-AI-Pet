"""Qt-free, provider-free workflow models for Phase 9D.1.

Pure data contracts and state vocabulary only. Nothing here starts a process,
reads runtime state, imports Qt, or touches the network. The stateful
:class:`WorkflowCoordinator` in :mod:`core.workflow_coordinator` consumes these
models; artifact storage, execution, and UI belong to later phases (9D.2+).

The vocabulary is deliberately small and linear: one workflow kind, strictly
ordered steps, a fixed artifact vocabulary, and a completion-evidence model
that cannot express "the external lifecycle said success" — observer lifecycle
is never authoritative workflow truth.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.routing_models import HandoffMode, TaskRequest


def _now_ms() -> int:
    return int(time.time() * 1000)


def make_workflow_id() -> str:
    return uuid.uuid4().hex[:12]


class WorkflowKind(str, Enum):
    """Supported workflow templates. First version: exactly one."""

    PLAN_IMPLEMENT_REVIEW = "plan_implement_review"


class WorkflowState(str, Enum):
    """Workflow-level lifecycle.

    SUCCEEDED never means "an agent process was spawned": every required step
    must have met the coordinator's success conditions first.
    """

    CREATED = "created"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStepState(str, Enum):
    """Per-step lifecycle.

    Typical path: PENDING -> AWAITING_CONFIRMATION -> READY -> RUNNING
    -> SUCCEEDED. Failure: RUNNING -> FAILED. Cancel: (PENDING /
    AWAITING_CONFIRMATION / READY / RUNNING) -> CANCELLED; later PENDING steps
    -> SKIPPED.
    """

    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class WorkflowStepIntent(str, Enum):
    """Small workflow-intent vocabulary (distinct from routing TaskIntent)."""

    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"


class ArtifactKind(str, Enum):
    """Stable artifact vocabulary. Deliberately small, not an open-ended set."""

    PLAN = "plan"
    IMPLEMENTATION_SUMMARY = "implementation_summary"
    CHANGED_FILES = "changed_files"
    REVIEW = "review"


class CompletionSource(str, Enum):
    """Why the coordinator accepted a step's completion.

    Deliberately NO "external lifecycle" / observer member: a provider hook
    reporting success (e.g. Codex Stop) is never, by itself, evidence that an
    implementation step succeeded.
    """

    MANAGED_AGENT_RESULT = "managed_agent_result"
    USER_CONFIRMED = "user_confirmed"
    ARTIFACT_VALIDATED = "artifact_validated"


@dataclass(frozen=True, slots=True)
class StepCompletionEvidence:
    """Minimal record of why the coordinator accepted a step as succeeded."""

    source: CompletionSource
    summary: str = ""
    metadata: dict | None = field(default=None)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Reference to a produced artifact.

    Phase 9D.1 defines the contract only: no file is created and no
    ArtifactStore exists yet. ``path`` may later point at
    ``runtime/artifacts/<workflow_id>/plan.md``.
    """

    artifact_id: str
    kind: ArtifactKind
    producer_step_id: str
    path: str
    created_at: int
    metadata: dict | None = field(default=None)


def make_artifact_ref(
    kind: ArtifactKind,
    producer_step_id: str,
    path: str,
    *,
    metadata: dict | None = None,
    created_at: int | None = None,
) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=uuid.uuid4().hex[:12],
        kind=kind,
        producer_step_id=producer_step_id,
        path=str(path),
        created_at=created_at if created_at is not None else _now_ms(),
        metadata=metadata,
    )


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One strictly-ordered workflow step.

    ``expected_artifacts`` gates success: every kind must be attached before
    the step may be marked succeeded. ``required_artifacts`` gates activation:
    the step may only be confirmed once its required kinds are attached (by
    earlier steps). ``completion_sources`` is the evidence policy for this
    step.
    """

    step_id: str
    agent_id: str
    intent: WorkflowStepIntent
    handoff_mode: HandoffMode
    requires_confirmation: bool
    state: WorkflowStepState
    created_at: int
    updated_at: int
    title: str | None = None
    expected_artifacts: tuple[ArtifactKind, ...] = ()
    required_artifacts: tuple[ArtifactKind, ...] = ()
    completion_sources: frozenset[CompletionSource] = frozenset(
        {
            CompletionSource.MANAGED_AGENT_RESULT,
            CompletionSource.USER_CONFIRMED,
            CompletionSource.ARTIFACT_VALIDATED,
        }
    )
    attached_artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowPlan:
    """A workflow in a given state.

    Immutable: every coordinator transition returns a new WorkflowPlan. Holds
    the original TaskRequest (``original_request.text`` is the user task), never
    a transcript, chat history, session id, or API credential.
    """

    workflow_id: str
    kind: WorkflowKind
    original_request: TaskRequest
    workspace: str | None
    steps: tuple[WorkflowStep, ...]
    state: WorkflowState
    current_step_index: int
    created_at: int
    updated_at: int


class WorkflowEventType(str, Enum):
    """Application-level workflow lifecycle events (never provider events)."""

    WORKFLOW_CREATED = "workflow_created"
    STEP_CONFIRMATION_REQUIRED = "step_confirmation_required"
    STEP_READY = "step_ready"
    STEP_STARTED = "step_started"
    ARTIFACT_ATTACHED = "artifact_attached"
    STEP_SUCCEEDED = "step_succeeded"
    STEP_FAILED = "step_failed"
    STEP_CANCELLED = "step_cancelled"
    WORKFLOW_SUCCEEDED = "workflow_succeeded"
    WORKFLOW_FAILED = "workflow_failed"
    WORKFLOW_CANCELLED = "workflow_cancelled"


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    """One application-level workflow lifecycle event.

    Distinct from AgentEvent (one agent turn) and HandoffState (transport
    lifecycle). Never embeds provider raw JSON, display language, session ids,
    or secrets.
    """

    workflow_id: str
    type: WorkflowEventType
    timestamp: int
    step_id: str | None = None
    step_index: int = -1
    agent_id: str | None = None
    artifact: ArtifactRef | None = None
    evidence: StepCompletionEvidence | None = None
    error_code: str | None = None
    metadata: dict | None = field(default=None)
