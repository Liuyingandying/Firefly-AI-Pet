"""Structured lifecycle notifications for Firefly.

Consumes the same structured AgentState the rest of the app already receives
from StateMonitor.agent_state_changed, decides whether a lightweight user
notice is warranted, and emits a structured NotificationEvent. Qt-free,
application-service style (callback listeners, no QWidget) so it is easy to
unit-test without a display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import AgentState, LifecycleState

AGENT_NAMES = {"claude": "Claude", "codex": "Codex"}
NOTIFY_AGENTS = frozenset(AGENT_NAMES)

# Only success and error warrant a normal notification. waiting is owned by
# the PermissionCard; thinking/working are too noisy; idle/sleeping are not
# notices at all (idle/sleeping reset the per-agent episode bookkeeping).
NOTIFIABLE_STATES = {LifecycleState.SUCCESS, LifecycleState.ERROR}
RESET_STATES = {LifecycleState.IDLE, LifecycleState.SLEEPING}

# A success within this many ms of a prior notification is suppressed. Errors
# are never cooldown-suppressed: a real new error always gets through.
SUCCESS_COOLDOWN_MS = 3_000

# Mirrors the state_broker priority scale so the UI layer can merge pending
# events (error outranks success) with one comparison.
PRIORITIES = {"success": 5, "error": 6}

MESSAGES = {
    "success": {
        "claude": ("Claude finished", "Everything looks good."),
        "codex": ("Codex finished", "Codex completed the task."),
    },
    "error": {
        "claude": ("Claude ran into a problem", "Open the agent to check details."),
        "codex": ("Codex ran into a problem", "Open the agent to check details."),
    },
}


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    agent_id: str
    kind: str  # "success" | "error"
    title: str
    message: str
    timestamp: int
    priority: int


class NotificationManager:
    """Dedupe and suppress AgentState transitions into NotificationEvents.

    Episode rule: a new success/error is notified once per episode. An episode
    ends when the agent reports idle or sleeping. Repeated snapshots of the
    same kind inside an episode never re-notify.
    """

    def __init__(self, success_cooldown_ms: int = SUCCESS_COOLDOWN_MS) -> None:
        self._success_cooldown_ms = success_cooldown_ms
        self._listeners: list[Callable[[NotificationEvent], None]] = []
        self._baseline_done: set[str] = set()
        self._notified: dict[str, tuple[str, int]] = {}
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Application-level gate: state is still consumed and bookkept, but
        success/error events are not emitted. Re-enabling never replays the
        notices that were suppressed while disabled."""
        self._enabled = bool(enabled)

    # -- listeners ------------------------------------------------------

    def connect(self, callback: Callable[[NotificationEvent], None]) -> None:
        self._listeners.append(callback)

    def disconnect(self, callback: Callable[[NotificationEvent], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    # -- consumption ----------------------------------------------------

    def on_agent_state(self, state: AgentState) -> None:
        agent_id = state.agent_id
        if agent_id not in NOTIFY_AGENTS:
            return

        # First state per agent only establishes the baseline: historical
        # success/error already in source files at startup are not new notices.
        # A baseline that is already a notice still records it so repeated
        # snapshots of the same kind are not re-notified later in the episode.
        if agent_id not in self._baseline_done:
            self._baseline_done.add(agent_id)
            if state.state in NOTIFIABLE_STATES:
                self._notified[agent_id] = (state.state.value, state.timestamp)
            return

        if state.state in RESET_STATES:
            self._notified.pop(agent_id, None)
            return

        if state.state not in NOTIFIABLE_STATES:
            return

        kind = state.state.value
        last_kind, last_ts = self._notified.get(agent_id, (None, None))
        if kind == last_kind:
            return  # already notified this episode

        if (
            kind == "success"
            and last_ts is not None
            and state.timestamp - last_ts < self._success_cooldown_ms
        ):
            return

        self._notified[agent_id] = (kind, state.timestamp)
        title, message = MESSAGES[kind][agent_id]
        self._emit(
            NotificationEvent(
                agent_id=agent_id,
                kind=kind,
                title=title,
                message=message,
                timestamp=state.timestamp,
                priority=PRIORITIES[kind],
            )
        )

    def _emit(self, event: NotificationEvent) -> None:
        if not self._enabled:
            return
        for listener in list(self._listeners):
            listener(event)
