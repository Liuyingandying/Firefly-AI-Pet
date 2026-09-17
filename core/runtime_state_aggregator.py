"""AI Runtime state aggregator for Firefly (Phase 2A).

Subscribes to RuntimeBus event-channel topics (``agent.event``,
``workflow.event``, ``session.change``, ``notification``,
``plugin.status``) and aggregates them into a single AI activity state
published on the bus as ``RuntimeEvent(kind='runtime.activity')``.

The aggregator never reads files, never calls models, and never touches
StateMonitor, hook, or ``runtime/sources``.
"""

from __future__ import annotations

import time
from enum import Enum

from PySide6.QtCore import QObject, QTimer

from core.agent_events import AgentEventType, WORKING_STATUSES, STATUS_RUNNING_TOOL
from core.runtime_bus import RuntimeBus, RuntimeEvent
from core.workflow_models import WorkflowEventType


class RuntimeActivityState(str, Enum):
    """High-level AI activity state displayed on the Firefly pet.

    The aggregator transitions between these values based on incoming
    RuntimeBus events and publishes every change back onto the bus.
    SUCCESS / ERROR are terminal states that auto-recover after 5 s.
    """

    IDLE = "idle"
    WORKING = "working"
    TOOL_RUNNING = "tool_running"
    WAITING_INPUT = "waiting_input"
    SUCCESS = "success"
    ERROR = "error"


_RECOVERY_MS = 5_000

# Why a terminal state was entered — decides the recovery target (Phase 5G-1).
# agent_finished: turn/workflow-level completion; nothing more is expected, so
# recovery goes to IDLE. Restoring the pre-completion WORKING here produced
# zombie "working" displays after the AI turn had actually ended.
# workflow_step: one step of a continuing workflow finished; the pre-step
# activity is restored because more workflow work is expected.
_TERMINAL_REASON_AGENT = "agent_finished"
_TERMINAL_REASON_WORKFLOW_STEP = "workflow_step"

_TERMINAL_STATES = frozenset(
    {RuntimeActivityState.SUCCESS, RuntimeActivityState.ERROR}
)


class RuntimeStateAggregator(QObject):
    """Aggregate RuntimeBus events into a single AI activity state.

    Parameters
    ----------
    bus:
        RuntimeBus instance to subscribe to and publish on.
    recovery_ms:
        Milliseconds SUCCESS/ERROR persist before auto-recovery (default 5000).
    """

    def __init__(
        self,
        bus: RuntimeBus,
        parent: QObject | None = None,
        *,
        recovery_ms: int = _RECOVERY_MS,
    ) -> None:
        super().__init__(parent)
        self._bus = bus
        self._current = RuntimeActivityState.IDLE
        self._previous = RuntimeActivityState.IDLE
        self._terminal_reason: str | None = None

        self._recovery_timer = QTimer(self)
        self._recovery_timer.setSingleShot(True)
        self._recovery_timer.setInterval(recovery_ms)
        self._recovery_timer.timeout.connect(self._on_recovery)

        self._unsub = bus.subscribe_event(self._on_event)

    # -- public API ----------------------------------------------------------

    @property
    def current(self) -> RuntimeActivityState:
        return self._current

    def close(self) -> None:
        """Unsubscribe from the bus and stop the recovery timer."""
        self._recovery_timer.stop()
        self._unsub()

    # -- event dispatch ------------------------------------------------------

    def _on_event(self, event: RuntimeEvent) -> None:
        new_state = self._dispatch(event)
        if new_state is not None:
            self._transition(new_state, self._reason_for(event, new_state))

    def _reason_for(
        self, event: RuntimeEvent, state: RuntimeActivityState
    ) -> str | None:
        """Classify why ``state`` is terminal; drives the recovery target.

        Workflow *step* completions (STEP_SUCCEEDED / STEP_FAILED) restore the
        pre-step activity; turn- and workflow-level completions (AgentEvent
        FINAL/ERROR, notifications, WORKFLOW_SUCCEEDED/FAILED) are final and
        recover to IDLE.
        """
        if state not in _TERMINAL_STATES:
            return None
        if event.kind == "workflow.event":
            wf_type = getattr(event.payload, "type", None)
            if wf_type in (
                WorkflowEventType.STEP_SUCCEEDED,
                WorkflowEventType.STEP_FAILED,
            ):
                return _TERMINAL_REASON_WORKFLOW_STEP
        return _TERMINAL_REASON_AGENT

    def _dispatch(self, event: RuntimeEvent) -> RuntimeActivityState | None:
        kind = event.kind
        payload = event.payload

        if kind == "agent.event":
            return self._on_agent_event(payload)
        if kind == "workflow.event":
            return self._on_workflow_event(payload)
        if kind == "notification":
            return self._on_notification(payload)
        if kind == "session.change":
            return self._on_session_change(payload)
        if kind == "plugin.status":
            return self._on_plugin_status(payload)
        return None  # unknown topic → ignored

    # -- per-domain rules (duck-typed payloads, work with frozen dataclasses) -

    def _on_agent_event(self, payload) -> RuntimeActivityState | None:
        ev_type = getattr(payload, "type", None)
        status = getattr(payload, "status", None)
        tool_name = getattr(payload, "tool_name", None)

        if ev_type == AgentEventType.ERROR:
            return RuntimeActivityState.ERROR
        if ev_type == AgentEventType.FINAL:
            return RuntimeActivityState.SUCCESS
        if ev_type == AgentEventType.CANCELLED:
            return RuntimeActivityState.IDLE
        if tool_name or ev_type == AgentEventType.TOOL:
            return RuntimeActivityState.TOOL_RUNNING
        if status == STATUS_RUNNING_TOOL:
            return RuntimeActivityState.TOOL_RUNNING
        if ev_type == AgentEventType.STARTED:
            return RuntimeActivityState.WORKING
        if status and status in WORKING_STATUSES:
            return RuntimeActivityState.WORKING
        return None

    def _on_workflow_event(self, payload) -> RuntimeActivityState | None:
        wf_type = getattr(payload, "type", None)
        if wf_type in (
            WorkflowEventType.WORKFLOW_CREATED,
            WorkflowEventType.STEP_STARTED,
        ):
            return RuntimeActivityState.WORKING
        if wf_type == WorkflowEventType.STEP_CONFIRMATION_REQUIRED:
            return RuntimeActivityState.WAITING_INPUT
        if wf_type in (
            WorkflowEventType.STEP_SUCCEEDED,
            WorkflowEventType.WORKFLOW_SUCCEEDED,
        ):
            return RuntimeActivityState.SUCCESS
        if wf_type in (
            WorkflowEventType.STEP_FAILED,
            WorkflowEventType.WORKFLOW_FAILED,
        ):
            return RuntimeActivityState.ERROR
        if wf_type in (
            WorkflowEventType.STEP_CANCELLED,
            WorkflowEventType.WORKFLOW_CANCELLED,
        ):
            return RuntimeActivityState.IDLE
        return None

    def _on_notification(self, payload) -> RuntimeActivityState | None:
        kind = getattr(payload, "kind", None)
        if kind == "success":
            return RuntimeActivityState.SUCCESS
        if kind == "error":
            return RuntimeActivityState.ERROR
        return None

    def _on_session_change(self, payload) -> RuntimeActivityState | None:
        ref = getattr(payload, "ref", None)
        if ref is not None:
            return RuntimeActivityState.WORKING
        return RuntimeActivityState.IDLE

    def _on_plugin_status(self, payload) -> RuntimeActivityState | None:
        status = getattr(payload, "status", None) or ""
        if status.upper() in ("ONLINE", "ACTIVE", "RUNNING", "WORKING"):
            return RuntimeActivityState.WORKING
        if status.upper() in ("OFFLINE", "ERROR"):
            return RuntimeActivityState.IDLE
        return None

    # -- state machine + recovery timer -------------------------------------

    def _transition(
        self, new_state: RuntimeActivityState, terminal_reason: str | None = None
    ) -> None:
        if new_state == self._current:
            # A later completion fact can upgrade the reason for the same
            # terminal state (STEP_SUCCEEDED followed by turn FINAL — both map
            # to SUCCESS): the recovery must follow the newest fact.
            if (
                new_state in _TERMINAL_STATES
                and terminal_reason == _TERMINAL_REASON_AGENT
                and self._terminal_reason == _TERMINAL_REASON_WORKFLOW_STEP
            ):
                self._terminal_reason = terminal_reason
            return

        if new_state in (RuntimeActivityState.SUCCESS, RuntimeActivityState.ERROR):
            if self._current not in (
                RuntimeActivityState.SUCCESS,
                RuntimeActivityState.ERROR,
            ):
                self._previous = self._current
            self._terminal_reason = terminal_reason
            self._publish(new_state)
            self._recovery_timer.start()
        else:
            self._terminal_reason = None
            self._recovery_timer.stop()
            self._publish(new_state)

    def _publish(self, state: RuntimeActivityState) -> None:
        self._current = state
        self._bus.publish_event(
            RuntimeEvent(
                kind="runtime.activity",
                source="runtime_state_aggregator",
                timestamp=int(time.time() * 1000),
                payload=state,
            )
        )

    def _on_recovery(self) -> None:
        # Workflow *step* completions restore the pre-step activity (the
        # workflow continues); turn/workflow-level completions recover to
        # IDLE — restoring the pre-completion WORKING kept the pet in a
        # zombie "working" state after the AI turn had actually ended.
        if self._terminal_reason == _TERMINAL_REASON_WORKFLOW_STEP:
            target = self._previous
        else:
            target = RuntimeActivityState.IDLE
        self._terminal_reason = None
        self._previous = RuntimeActivityState.IDLE
        self._publish(target)