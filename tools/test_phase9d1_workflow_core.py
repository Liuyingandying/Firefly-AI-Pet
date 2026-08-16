"""Phase 9D.1 — Workflow Core + Confirmation Gates tests.

Covers the 59 required scenarios plus a request_confirmation transition test:
PLAN_IMPLEMENT_REVIEW creation (exactly 3 steps, Claude->Codex->Claude, correct
intents/handoff modes, every step confirmed, step-1 AWAITING_CONFIRMATION /
later PENDING, workflow WAITING_FOR_USER); confirm -> READY without launching an
agent; READY -> RUNNING; PLAN artifact attach + producer; PLAN artifact gate
(no PLAN -> cannot succeed); success advances to Implement AWAITING_CONFIRMATION;
Implement requires PLAN; unconfirmed steps cannot start; IMPLEMENTATION_SUMMARY +
CHANGED_FILES artifact gates; external lifecycle alone can never complete Codex
(CompletionSource has no lifecycle member, MANAGED_AGENT_RESULT is rejected for
Codex) while USER_CONFIRMED is allowed; Review gates; all-succeeded ->
WORKFLOW_SUCCEEDED; step failure -> WORKFLOW_FAILED with later steps SKIPPED and
no automatic retry; cancel from waiting / ready; cancelled workflows cannot
continue; illegal transitions rejected (PENDING->RUNNING, FAILED->RUNNING,
SUCCEEDED->FAILED); wrong step id / wrong artifact kind / wrong producer
rejected; required-artifact gate; deterministic event order; WorkflowEvent
separate from AgentEvent; zero provider-JSON / Qt / subprocess / network
dependency; capability-registry validation; original TaskRequest retained; no
transcript or API-credential fields; deterministic template structure; AgentRouter
and HandoffState stay independent; no config/runtime writes; 0 Claude / 0 Codex
online calls.

Pure core tests: no Qt, no subprocess, no network, no online calls.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import fields
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core.agent_events import AgentEvent, AgentEventType
from core.agent_router import AgentRouter
from core.handoff import HandoffState
from core.routing_models import (
    AgentCapability,
    AgentProfile,
    CapabilityRegistry,
    DEFAULT_CAPABILITY_REGISTRY,
    HandoffMode,
    TaskRequest,
)
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
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
    make_artifact_ref,
)
from test_phase9a_agent_router import _forbidden_in_module

WORKFLOW_FILES = (
    PROJECT_DIR / "core" / "workflow_models.py",
    PROJECT_DIR / "core" / "workflow_coordinator.py",
)


# -- helpers ---------------------------------------------------------------

def _req(text: str = "Refactor the auth module", workspace: str = "E:/Work") -> TaskRequest:
    return TaskRequest(text=text, workspace=workspace)


def _coordinator(registry: CapabilityRegistry | None = None) -> WorkflowCoordinator:
    state = {"t": 1_700_000_000_000}

    def clock() -> int:
        state["t"] += 1
        return state["t"]

    return WorkflowCoordinator(registry=registry, clock=clock)


def _create(coord=None, req=None) -> tuple[WorkflowCoordinator, WorkflowPlan]:
    coord = coord or _coordinator()
    plan = coord.create_plan_implement_review(req or _req())
    return coord, plan


def _evidence(
    source: CompletionSource = CompletionSource.USER_CONFIRMED, summary: str = "ok"
) -> StepCompletionEvidence:
    return StepCompletionEvidence(source=source, summary=summary)


def _artifact(kind: ArtifactKind, producer: str) -> ArtifactRef:
    return make_artifact_ref(
        kind=kind,
        producer_step_id=producer,
        path=f"runtime/artifacts/{producer}/{kind.value}.md",
        created_at=1,
    )


def _full_flow(coord: WorkflowCoordinator, plan: WorkflowPlan) -> WorkflowPlan:
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.IMPLEMENTATION_SUMMARY, s[1].step_id)
    )
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.CHANGED_FILES, s[1].step_id)
    )
    plan = coord.mark_step_succeeded(plan, s[1].step_id, _evidence(CompletionSource.USER_CONFIRMED))
    plan = coord.confirm_step(plan, s[2].step_id)
    plan = coord.mark_step_started(plan, s[2].step_id)
    plan = coord.attach_artifact(plan, s[2].step_id, _artifact(ArtifactKind.REVIEW, s[2].step_id))
    plan = coord.mark_step_succeeded(plan, s[2].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    return plan


# -- 1-9. creation ---------------------------------------------------------

def test_create_plan_implement_review() -> None:
    _coord, plan = _create()
    assert plan.workflow_id
    assert plan.kind == WorkflowKind.PLAN_IMPLEMENT_REVIEW
    assert isinstance(plan, WorkflowPlan)


def test_exactly_three_steps() -> None:
    _coord, plan = _create()
    assert len(plan.steps) == 3


def test_step_agent_order() -> None:
    _coord, plan = _create()
    assert tuple(s.agent_id for s in plan.steps) == ("claude", "codex", "claude")


def test_step_intents() -> None:
    _coord, plan = _create()
    assert tuple(s.intent for s in plan.steps) == (
        WorkflowStepIntent.PLAN,
        WorkflowStepIntent.IMPLEMENT,
        WorkflowStepIntent.REVIEW,
    )


def test_step_handoff_modes() -> None:
    _coord, plan = _create()
    assert tuple(s.handoff_mode for s in plan.steps) == (
        HandoffMode.SHORT_TALK,
        HandoffMode.OPEN_NATIVE,
        HandoffMode.SHORT_TALK,
    )


def test_every_step_requires_confirmation() -> None:
    _coord, plan = _create()
    assert all(s.requires_confirmation for s in plan.steps)


def test_first_step_initial_awaiting_confirmation() -> None:
    _coord, plan = _create()
    assert plan.steps[0].state == WorkflowStepState.AWAITING_CONFIRMATION


def test_later_steps_initial_pending() -> None:
    _coord, plan = _create()
    assert plan.steps[1].state == WorkflowStepState.PENDING
    assert plan.steps[2].state == WorkflowStepState.PENDING


def test_workflow_initial_waiting_for_user() -> None:
    _coord, plan = _create()
    assert plan.state == WorkflowState.WAITING_FOR_USER
    assert plan.current_step_index == 0


# -- 10-16. step 1 confirm / start / artifacts / succeed --------------------

def test_confirm_first_step_readies() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    assert plan.steps[0].state == WorkflowStepState.READY


def test_confirm_does_not_launch_agent() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    assert plan.steps[0].state == WorkflowStepState.READY
    # The coordinator carries no execution machinery at all.
    for forbidden in ("launch", "ask", "spawn", "startDetached", "taskkill", "process"):
        assert not hasattr(coord, forbidden), f"coordinator must not expose {forbidden}"
    hits = _forbidden_in_module(
        WORKFLOW_FILES[1],
        ("QProcess", "ProcessLauncher", "QuickAskRunner", "subprocess", "Popen", "startDetached"),
    )
    assert not hits, f"workflow_coordinator must not launch agents: {hits}"


def test_ready_mark_started_running() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    assert plan.steps[0].state == WorkflowStepState.READY
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    assert plan.steps[0].state == WorkflowStepState.RUNNING
    assert plan.state == WorkflowState.RUNNING


def test_plan_artifact_attach() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    ref = _artifact(ArtifactKind.PLAN, plan.steps[0].step_id)
    plan = coord.attach_artifact(plan, plan.steps[0].step_id, ref)
    assert ref in plan.steps[0].attached_artifacts


def test_plan_artifact_producer_correct() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    ref = _artifact(ArtifactKind.PLAN, plan.steps[0].step_id)
    plan = coord.attach_artifact(plan, plan.steps[0].step_id, ref)
    assert ref.producer_step_id == plan.steps[0].step_id
    assert plan.steps[0].attached_artifacts[0].kind == ArtifactKind.PLAN


def test_no_plan_artifact_cannot_succeed_plan() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    try:
        coord.mark_step_succeeded(plan, plan.steps[0].step_id, _evidence())
    except WorkflowTransitionError:
        return
    raise AssertionError("step must not succeed without its expected artifact")


def test_plan_succeed_with_valid_evidence() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    plan = coord.attach_artifact(plan, plan.steps[0].step_id, _artifact(ArtifactKind.PLAN, plan.steps[0].step_id))
    plan = coord.mark_step_succeeded(plan, plan.steps[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    assert plan.steps[0].state == WorkflowStepState.SUCCEEDED


# -- 17-25. step 2 (Codex Implement) gates ---------------------------------

def test_plan_success_activates_implement() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    plan = coord.attach_artifact(plan, plan.steps[0].step_id, _artifact(ArtifactKind.PLAN, plan.steps[0].step_id))
    plan = coord.mark_step_succeeded(plan, plan.steps[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    assert plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert plan.current_step_index == 1
    assert plan.state == WorkflowState.WAITING_FOR_USER


def test_implement_required_plan() -> None:
    _coord, plan = _create()
    assert plan.steps[1].required_artifacts == (ArtifactKind.PLAN,)


def test_implement_unconfirmed_cannot_start() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    plan = coord.attach_artifact(plan, plan.steps[0].step_id, _artifact(ArtifactKind.PLAN, plan.steps[0].step_id))
    plan = coord.mark_step_succeeded(plan, plan.steps[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    assert plan.steps[1].state == WorkflowStepState.AWAITING_CONFIRMATION
    try:
        coord.mark_step_started(plan, plan.steps[1].step_id)
    except WorkflowTransitionError:
        return
    raise AssertionError("an unconfirmed step must not start")


def test_confirm_implement_ready() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    assert plan.steps[1].state == WorkflowStepState.READY


def test_implement_started_running() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    assert plan.steps[1].state == WorkflowStepState.RUNNING
    assert plan.state == WorkflowState.RUNNING


def test_external_lifecycle_alone_cannot_complete_codex() -> None:
    # Model-level: CompletionSource has NO observer-lifecycle / external member.
    assert {s.value for s in CompletionSource} == {
        "managed_agent_result",
        "user_confirmed",
        "artifact_validated",
    }
    assert not any(
        "lifecycle" in s.value or "external" in s.value or "hook" in s.value
        for s in CompletionSource
    )
    coord, plan = _create()
    # Step 2 (Codex IMPLEMENT) only accepts user / validated-artifact evidence.
    assert plan.steps[1].completion_sources == frozenset(
        {CompletionSource.USER_CONFIRMED, CompletionSource.ARTIFACT_VALIDATED}
    )
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    # MANAGED_AGENT_RESULT claims a managed result that Codex has no path to.
    try:
        coord.mark_step_succeeded(
            plan, s[1].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT)
        )
    except WorkflowTransitionError:
        pass
    else:
        raise AssertionError("Codex step must not accept MANAGED_AGENT_RESULT evidence")
    # The workflow core has no lifecycle dependency at all.
    for path in WORKFLOW_FILES:
        hits = _forbidden_in_module(
            path, ("state_monitor", "agent_events", "agent_adapters", "notification_manager")
        )
        assert not hits, f"{path.name} must not depend on lifecycle/agent events: {hits}"


def test_user_confirmed_is_allowed_codex_evidence() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.IMPLEMENTATION_SUMMARY, s[1].step_id)
    )
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.CHANGED_FILES, s[1].step_id)
    )
    plan = coord.mark_step_succeeded(plan, s[1].step_id, _evidence(CompletionSource.USER_CONFIRMED))
    assert plan.steps[1].state == WorkflowStepState.SUCCEEDED


def test_implementation_summary_artifact_gate() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.CHANGED_FILES, s[1].step_id)
    )
    try:
        coord.mark_step_succeeded(plan, s[1].step_id, _evidence(CompletionSource.USER_CONFIRMED))
    except WorkflowTransitionError:
        return
    raise AssertionError("step must not succeed without IMPLEMENTATION_SUMMARY")


def test_changed_files_artifact_gate() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.IMPLEMENTATION_SUMMARY, s[1].step_id)
    )
    try:
        coord.mark_step_succeeded(plan, s[1].step_id, _evidence(CompletionSource.USER_CONFIRMED))
    except WorkflowTransitionError:
        return
    raise AssertionError("step must not succeed without CHANGED_FILES")


# -- 26-30. step 3 (Claude Review) gates + success -------------------------

def test_implement_success_activates_review() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.IMPLEMENTATION_SUMMARY, s[1].step_id)
    )
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.CHANGED_FILES, s[1].step_id)
    )
    plan = coord.mark_step_succeeded(plan, s[1].step_id, _evidence(CompletionSource.USER_CONFIRMED))
    assert plan.steps[2].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert plan.current_step_index == 2
    assert plan.state == WorkflowState.WAITING_FOR_USER


def test_review_required_plan_and_changed_files() -> None:
    _coord, plan = _create()
    assert plan.steps[2].required_artifacts == (ArtifactKind.PLAN, ArtifactKind.CHANGED_FILES)


def test_review_unconfirmed_cannot_run() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.IMPLEMENTATION_SUMMARY, s[1].step_id)
    )
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.CHANGED_FILES, s[1].step_id)
    )
    plan = coord.mark_step_succeeded(plan, s[1].step_id, _evidence(CompletionSource.USER_CONFIRMED))
    try:
        coord.mark_step_started(plan, s[2].step_id)
    except WorkflowTransitionError:
        return
    raise AssertionError("an unconfirmed review step must not run")


def test_review_artifact_gate() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.IMPLEMENTATION_SUMMARY, s[1].step_id)
    )
    plan = coord.attach_artifact(
        plan, s[1].step_id, _artifact(ArtifactKind.CHANGED_FILES, s[1].step_id)
    )
    plan = coord.mark_step_succeeded(plan, s[1].step_id, _evidence(CompletionSource.USER_CONFIRMED))
    plan = coord.confirm_step(plan, s[2].step_id)
    plan = coord.mark_step_started(plan, s[2].step_id)
    try:
        coord.mark_step_succeeded(plan, s[2].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    except WorkflowTransitionError:
        pass
    else:
        raise AssertionError("review must not succeed without a REVIEW artifact")
    plan = coord.attach_artifact(plan, s[2].step_id, _artifact(ArtifactKind.REVIEW, s[2].step_id))
    plan = coord.mark_step_succeeded(plan, s[2].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    assert plan.steps[2].state == WorkflowStepState.SUCCEEDED


def test_all_succeeded_workflow_succeeded() -> None:
    coord, plan = _create()
    events: list[WorkflowEventType] = []
    coord.connect(lambda e: events.append(e.type))
    plan = _full_flow(coord, plan)
    assert all(s.state == WorkflowStepState.SUCCEEDED for s in plan.steps)
    assert plan.state == WorkflowState.SUCCEEDED
    assert WorkflowEventType.WORKFLOW_SUCCEEDED in events


# -- 31-33. failure semantics ----------------------------------------------

def test_step_failure_workflow_failed() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.mark_step_failed(plan, s[1].step_id, "implementation_error")
    assert plan.steps[1].state == WorkflowStepState.FAILED
    assert plan.state == WorkflowState.FAILED


def test_failure_skips_later_steps() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.mark_step_failed(plan, s[1].step_id, "implementation_error")
    assert plan.steps[2].state == WorkflowStepState.SKIPPED
    assert plan.steps[0].state == WorkflowStepState.SUCCEEDED


def test_no_automatic_retry() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.mark_step_failed(plan, s[1].step_id, "implementation_error")
    # The coordinator has no retry surface.
    for name in dir(coord):
        assert "retry" not in name.lower(), f"coordinator must not expose {name}"
    for action in (
        lambda p: coord.confirm_step(p, s[2].step_id),
        lambda p: coord.mark_step_started(p, s[2].step_id),
        lambda p: coord.mark_step_succeeded(p, s[2].step_id, _evidence()),
    ):
        try:
            action(plan)
        except WorkflowTransitionError:
            continue
        raise AssertionError("a failed workflow must not allow continuation")


# -- 34-36. cancel ----------------------------------------------------------

def test_cancel_from_waiting() -> None:
    coord, plan = _create()
    assert plan.steps[0].state == WorkflowStepState.AWAITING_CONFIRMATION
    plan = coord.cancel_workflow(plan)
    assert plan.state == WorkflowState.CANCELLED
    assert plan.steps[0].state == WorkflowStepState.CANCELLED
    assert plan.steps[1].state == WorkflowStepState.SKIPPED
    assert plan.steps[2].state == WorkflowStepState.SKIPPED


def test_cancel_from_ready() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    assert plan.steps[0].state == WorkflowStepState.READY
    plan = coord.cancel_workflow(plan)
    assert plan.state == WorkflowState.CANCELLED
    assert plan.steps[0].state == WorkflowStepState.CANCELLED
    assert plan.steps[1].state == WorkflowStepState.SKIPPED


def test_cancelled_workflow_cannot_continue() -> None:
    coord, plan = _create()
    plan = coord.cancel_workflow(plan)
    assert plan.state == WorkflowState.CANCELLED
    for action in (
        lambda p: coord.confirm_step(p, p.steps[0].step_id),
        lambda p: coord.mark_step_started(p, p.steps[0].step_id),
        lambda p: coord.mark_step_succeeded(p, p.steps[0].step_id, _evidence()),
    ):
        try:
            action(plan)
        except WorkflowTransitionError:
            continue
        raise AssertionError("a cancelled workflow must not continue")


# -- 37-43. illegal transitions / validation --------------------------------

def test_illegal_pending_to_running() -> None:
    coord, plan = _create()
    # step 2 is PENDING and not current; starting it is illegal.
    try:
        coord.mark_step_started(plan, plan.steps[1].step_id)
    except WorkflowTransitionError:
        return
    raise AssertionError("PENDING -> RUNNING must be rejected")


def test_illegal_failed_to_running() -> None:
    coord, plan = _create()
    s = plan.steps
    plan = coord.confirm_step(plan, s[0].step_id)
    plan = coord.mark_step_started(plan, s[0].step_id)
    plan = coord.attach_artifact(plan, s[0].step_id, _artifact(ArtifactKind.PLAN, s[0].step_id))
    plan = coord.mark_step_succeeded(plan, s[0].step_id, _evidence(CompletionSource.MANAGED_AGENT_RESULT))
    plan = coord.confirm_step(plan, s[1].step_id)
    plan = coord.mark_step_started(plan, s[1].step_id)
    plan = coord.mark_step_failed(plan, s[1].step_id, "x")
    for action in (
        lambda p: coord.mark_step_started(p, s[1].step_id),
        lambda p: coord.mark_step_started(p, s[2].step_id),
    ):
        try:
            action(plan)
        except WorkflowTransitionError:
            continue
        raise AssertionError("FAILED/SKIPPED -> RUNNING must be rejected")


def test_illegal_succeeded_to_failed() -> None:
    coord, plan = _create()
    plan = _full_flow(coord, plan)
    assert plan.state == WorkflowState.SUCCEEDED
    try:
        coord.mark_step_failed(plan, plan.steps[0].step_id, "x")
    except WorkflowTransitionError:
        return
    raise AssertionError("SUCCEEDED -> FAILED must be rejected")


def test_wrong_step_id_rejected() -> None:
    coord, plan = _create()
    for action in (
        lambda: coord.confirm_step(plan, "nope"),
        lambda: coord.mark_step_started(plan, "nope"),
        lambda: coord.mark_step_succeeded(plan, "nope", _evidence()),
    ):
        try:
            action()
        except WorkflowTransitionError:
            continue
        raise AssertionError("an unknown step id must be rejected")


def test_wrong_artifact_kind_rejected() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    try:
        coord.attach_artifact(
            plan, plan.steps[0].step_id, _artifact(ArtifactKind.CHANGED_FILES, plan.steps[0].step_id)
        )
    except WorkflowTransitionError:
        return
    raise AssertionError("a step must reject artifacts it does not expect")


def test_artifact_from_wrong_producer_rejected() -> None:
    coord, plan = _create()
    plan = coord.confirm_step(plan, plan.steps[0].step_id)
    plan = coord.mark_step_started(plan, plan.steps[0].step_id)
    ref = _artifact(ArtifactKind.PLAN, plan.steps[1].step_id)  # claims step 2 produced it
    try:
        coord.attach_artifact(plan, plan.steps[0].step_id, ref)
    except WorkflowTransitionError:
        return
    raise AssertionError("an artifact must match its producing step")


def test_required_artifacts_checked() -> None:
    coord = _coordinator()
    step = WorkflowStep(
        step_id="s1",
        agent_id="claude",
        intent=WorkflowStepIntent.PLAN,
        handoff_mode=HandoffMode.SHORT_TALK,
        requires_confirmation=True,
        state=WorkflowStepState.AWAITING_CONFIRMATION,
        created_at=1,
        updated_at=1,
        required_artifacts=(ArtifactKind.PLAN,),
    )
    plan = WorkflowPlan(
        workflow_id="w-hand",
        kind=WorkflowKind.PLAN_IMPLEMENT_REVIEW,
        original_request=_req(),
        workspace="E:/Work",
        steps=(step,),
        state=WorkflowState.WAITING_FOR_USER,
        current_step_index=0,
        created_at=1,
        updated_at=1,
    )
    assert coord.required_artifacts_satisfied(plan, "s1") is False
    try:
        coord.confirm_step(plan, "s1")
    except WorkflowTransitionError:
        return
    raise AssertionError("a step whose required artifacts are missing must not be confirmed")


# -- 44-49. events / isolation ---------------------------------------------

def test_events_deterministic_order() -> None:
    coord = _coordinator()
    recorded: list[tuple[WorkflowEventType, str | None, int]] = []
    coord.connect(
        lambda e: recorded.append((e.type, e.step_id, e.step_index))
    )
    plan = coord.create_plan_implement_review(_req())
    plan = _full_flow(coord, plan)
    expected = [
        (WorkflowEventType.WORKFLOW_CREATED, None, -1),
        (WorkflowEventType.STEP_CONFIRMATION_REQUIRED, "step_1", 0),
        (WorkflowEventType.STEP_READY, "step_1", 0),
        (WorkflowEventType.STEP_STARTED, "step_1", 0),
        (WorkflowEventType.ARTIFACT_ATTACHED, "step_1", 0),
        (WorkflowEventType.STEP_SUCCEEDED, "step_1", 0),
        (WorkflowEventType.STEP_CONFIRMATION_REQUIRED, "step_2", 1),
        (WorkflowEventType.STEP_READY, "step_2", 1),
        (WorkflowEventType.STEP_STARTED, "step_2", 1),
        (WorkflowEventType.ARTIFACT_ATTACHED, "step_2", 1),
        (WorkflowEventType.ARTIFACT_ATTACHED, "step_2", 1),
        (WorkflowEventType.STEP_SUCCEEDED, "step_2", 1),
        (WorkflowEventType.STEP_CONFIRMATION_REQUIRED, "step_3", 2),
        (WorkflowEventType.STEP_READY, "step_3", 2),
        (WorkflowEventType.STEP_STARTED, "step_3", 2),
        (WorkflowEventType.ARTIFACT_ATTACHED, "step_3", 2),
        (WorkflowEventType.STEP_SUCCEEDED, "step_3", 2),
        (WorkflowEventType.WORKFLOW_SUCCEEDED, None, -1),
    ]
    assert recorded == expected


def test_workflow_event_separate_from_agent_event() -> None:
    assert WorkflowEvent is not AgentEvent
    workflow_values = {t.value for t in WorkflowEventType}
    agent_values = {t.value for t in AgentEventType}
    assert workflow_values.isdisjoint(agent_values), "event vocabularies must not overlap"
    # AgentEvent stays untouched.
    assert AgentEventType.FINAL.value == "final"
    assert AgentEventType.SESSION.value == "session"
    # WorkflowEvent never carries provider/session data.
    event_fields = {f.name for f in fields(WorkflowEvent)}
    for forbidden in ("session_id", "tool_name", "provider_raw", "raw", "status"):
        assert forbidden not in event_fields, f"WorkflowEvent must not carry {forbidden}"
    hits = _forbidden_in_module(WORKFLOW_FILES[0], ("agent_events",))
    assert not hits, "workflow_models must not import agent_events"


def test_no_provider_json_dependency() -> None:
    for path in WORKFLOW_FILES:
        hits = _forbidden_in_module(
            path, ("json", "loads", "dumps", "agent_adapters", "provider", "raw_event")
        )
        assert not hits, f"{path.name} must not parse provider JSON: {hits}"


def test_no_qt_dependency() -> None:
    for path in WORKFLOW_FILES:
        hits = _forbidden_in_module(
            path, ("PySide6", "qtpy", "PyQt", "QProcess", "QWidget", "QObject", "QTimer", "Signal")
        )
        assert not hits, f"{path.name} must stay Qt-free: {hits}"


def test_no_subprocess() -> None:
    for path in WORKFLOW_FILES:
        hits = _forbidden_in_module(
            path, ("subprocess", "Popen", "startDetached", "taskkill", "claude", "codex", "node.exe")
        )
        assert not hits, f"{path.name} must not start processes: {hits}"


def test_no_network() -> None:
    for path in WORKFLOW_FILES:
        hits = _forbidden_in_module(
            path, ("socket", "urllib", "requests", "http", "urlopen", "websocket")
        )
        assert not hits, f"{path.name} must not touch the network: {hits}"


# -- 50-54. registry / retention / determinism ------------------------------

def test_capability_registry_validation() -> None:
    _coord, plan = _create()
    # Default registry satisfies the template.
    assert plan.kind == WorkflowKind.PLAN_IMPLEMENT_REVIEW
    # A registry missing a required capability refuses to build the workflow.
    fake = CapabilityRegistry(
        {
            "claude": AgentProfile(
                agent_id="claude",
                display_name="Claude",
                capabilities=frozenset(
                    {AgentCapability.ANALYSIS, AgentCapability.REVIEW, AgentCapability.MANAGED_SHORT_TALK}
                ),
                available=True,
                managed_short_talk=True,
            ),
            "codex": AgentProfile(
                agent_id="codex",
                display_name="Codex",
                capabilities=frozenset({AgentCapability.NATIVE_LAUNCH}),  # missing CODING
                available=True,
                managed_short_talk=False,
            ),
        }
    )
    try:
        _create(_coordinator(registry=fake))
    except WorkflowTransitionError:
        pass
    else:
        raise AssertionError("a registry without Codex CODING must refuse the workflow")


def test_original_request_retained() -> None:
    req = _req("Refactor the auth module", workspace="E:/Work")
    _coord, plan = _create(req=req)
    assert plan.original_request is req
    assert plan.original_request.text == "Refactor the auth module"
    assert plan.original_request.workspace == "E:/Work"
    assert plan.workspace == "E:/Work"


def test_no_transcript_in_model() -> None:
    _coord, plan = _create()
    for model in (WorkflowPlan, WorkflowStep):
        names = {f.name for f in fields(model)}
        for forbidden in ("transcript", "conversation", "history", "messages"):
            assert forbidden not in names, f"{model.__name__} must not carry {forbidden}"
    assert isinstance(plan.original_request.text, str)


def test_no_api_credential_fields() -> None:
    for model in (WorkflowPlan, WorkflowStep, ArtifactRef, WorkflowEvent, StepCompletionEvidence):
        names = {f.name for f in fields(model)}
        for forbidden in ("api_key", "apikey", "token", "secret", "password", "credential"):
            assert forbidden not in names, f"{model.__name__} must not carry {forbidden}"


def test_template_structure_deterministic() -> None:
    _c1, p1 = _create()
    _c2, p2 = _create(req=_req())
    assert p1.workflow_id != p2.workflow_id
    for a, b in zip(p1.steps, p2.steps):
        assert a.agent_id == b.agent_id
        assert a.intent == b.intent
        assert a.handoff_mode == b.handoff_mode
        assert a.requires_confirmation == b.requires_confirmation
        assert a.expected_artifacts == b.expected_artifacts
        assert a.required_artifacts == b.required_artifacts
        assert a.title == b.title
        assert a.completion_sources == b.completion_sources


# -- 55-59. independence / no writes / zero online --------------------------

def test_agent_router_remains_independent() -> None:
    # The workflow core never consults the router.
    hits = _forbidden_in_module(WORKFLOW_FILES[1], ("AgentRouter", "agent_router", "recommend"))
    assert not hits, f"workflow_coordinator must not depend on AgentRouter: {hits}"
    from unittest.mock import patch

    with patch.object(AgentRouter, "recommend", side_effect=AssertionError("router must not be called")):
        _coord, plan = _create()
    assert plan.kind == WorkflowKind.PLAN_IMPLEMENT_REVIEW


def test_handoff_state_remains_independent() -> None:
    hits = _forbidden_in_module(WORKFLOW_FILES[1], ("HandoffState", "handoff", "HandoffRequest"))
    assert not hits, f"workflow_coordinator must not depend on handoff: {hits}"
    # HandoffState semantics are unchanged: no task-success state.
    assert [s.value for s in HandoffState] == [
        "pending", "launching", "handed_off", "failed", "cancelled",
    ]
    assert "success" not in {s.value for s in HandoffState}


def test_no_config_runtime_writes() -> None:
    coord, plan = _create()
    workflows_json = PROJECT_DIR / "config" / "workflows.json"
    workflows_dir = PROJECT_DIR / "runtime" / "workflows"
    assert not workflows_json.exists()
    assert not workflows_dir.exists()
    plan = _full_flow(coord, plan)
    assert not workflows_json.exists(), "config/workflows.json must never be created in 9D.1"
    assert not workflows_dir.exists(), "runtime/workflows/ must never be created in 9D.1"
    # The coordinator has no file-write primitives at all.
    source = (PROJECT_DIR / "core" / "workflow_coordinator.py").read_text(encoding="utf-8")
    for forbidden in ("open(", "write_text", "mkdir", "os.replace", "Path(", "write_bytes"):
        assert forbidden not in source, f"workflow_coordinator must not write files via {forbidden}"


def test_claude_online_zero() -> None:
    coord, plan = _create()
    plan = _full_flow(coord, plan)
    # No managed Claude execution path exists in the workflow core.
    hits = _forbidden_in_module(
        WORKFLOW_FILES[1], ("QProcess", "subprocess", "launch_agent", "ask", "build_claude_args")
    )
    assert not hits, f"workflow_coordinator must never launch Claude: {hits}"
    assert plan.state == WorkflowState.SUCCEEDED


def test_codex_online_zero() -> None:
    coord, plan = _create()
    plan = _full_flow(coord, plan)
    hits = _forbidden_in_module(
        WORKFLOW_FILES[1], ("QProcess", "subprocess", "launch_agent", "ask", "build_codex_args")
    )
    assert not hits, f"workflow_coordinator must never launch Codex: {hits}"
    assert plan.state == WorkflowState.SUCCEEDED


# -- request_confirmation API ----------------------------------------------

def test_request_confirmation_advances_pending() -> None:
    coord = _coordinator()
    step = WorkflowStep(
        step_id="s1",
        agent_id="claude",
        intent=WorkflowStepIntent.PLAN,
        handoff_mode=HandoffMode.SHORT_TALK,
        requires_confirmation=True,
        state=WorkflowStepState.PENDING,
        created_at=1,
        updated_at=1,
    )
    plan = WorkflowPlan(
        workflow_id="w-pend",
        kind=WorkflowKind.PLAN_IMPLEMENT_REVIEW,
        original_request=_req(),
        workspace="E:/Work",
        steps=(step,),
        state=WorkflowState.CREATED,
        current_step_index=0,
        created_at=1,
        updated_at=1,
    )
    events: list[WorkflowEventType] = []
    coord.connect(lambda e: events.append(e.type))
    plan = coord.request_confirmation(plan)
    assert plan.steps[0].state == WorkflowStepState.AWAITING_CONFIRMATION
    assert plan.state == WorkflowState.WAITING_FOR_USER
    assert events == [WorkflowEventType.STEP_CONFIRMATION_REQUIRED]


def main() -> None:
    tests = [
        test_create_plan_implement_review,
        test_exactly_three_steps,
        test_step_agent_order,
        test_step_intents,
        test_step_handoff_modes,
        test_every_step_requires_confirmation,
        test_first_step_initial_awaiting_confirmation,
        test_later_steps_initial_pending,
        test_workflow_initial_waiting_for_user,
        test_confirm_first_step_readies,
        test_confirm_does_not_launch_agent,
        test_ready_mark_started_running,
        test_plan_artifact_attach,
        test_plan_artifact_producer_correct,
        test_no_plan_artifact_cannot_succeed_plan,
        test_plan_succeed_with_valid_evidence,
        test_plan_success_activates_implement,
        test_implement_required_plan,
        test_implement_unconfirmed_cannot_start,
        test_confirm_implement_ready,
        test_implement_started_running,
        test_external_lifecycle_alone_cannot_complete_codex,
        test_user_confirmed_is_allowed_codex_evidence,
        test_implementation_summary_artifact_gate,
        test_changed_files_artifact_gate,
        test_implement_success_activates_review,
        test_review_required_plan_and_changed_files,
        test_review_unconfirmed_cannot_run,
        test_review_artifact_gate,
        test_all_succeeded_workflow_succeeded,
        test_step_failure_workflow_failed,
        test_failure_skips_later_steps,
        test_no_automatic_retry,
        test_cancel_from_waiting,
        test_cancel_from_ready,
        test_cancelled_workflow_cannot_continue,
        test_illegal_pending_to_running,
        test_illegal_failed_to_running,
        test_illegal_succeeded_to_failed,
        test_wrong_step_id_rejected,
        test_wrong_artifact_kind_rejected,
        test_artifact_from_wrong_producer_rejected,
        test_required_artifacts_checked,
        test_events_deterministic_order,
        test_workflow_event_separate_from_agent_event,
        test_no_provider_json_dependency,
        test_no_qt_dependency,
        test_no_subprocess,
        test_no_network,
        test_capability_registry_validation,
        test_original_request_retained,
        test_no_transcript_in_model,
        test_no_api_credential_fields,
        test_template_structure_deterministic,
        test_agent_router_remains_independent,
        test_handoff_state_remains_independent,
        test_no_config_runtime_writes,
        test_claude_online_zero,
        test_codex_online_zero,
        test_request_confirmation_advances_pending,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            raise
    print(f"Phase 9D.1 workflow core tests passed ({len(tests)} tests).")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
