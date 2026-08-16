"""Keep the Windows system awake while an agent is actually busy.

Prevents system sleep during long agent work without touching the user's power
plan or keeping the display on. Uses the official Win32 API
SetThreadExecutionState via ctypes (no new pip dependency). Qt-free and
injectable so tests can substitute a fake backend and non-Windows CI is a safe
no-op.
"""

from __future__ import annotations

import ctypes
import os

from .models import AgentState, LifecycleState

# ES_* flags for SetThreadExecutionState.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# These states keep the workflow active even when an agent is only waiting on
# the user: the overall task is still running.
BUSY_STATES = {
    LifecycleState.THINKING,
    LifecycleState.WORKING,
    LifecycleState.WAITING,
}

# ChatGPT has no telemetry and manual is test-only; only claude/codex drive
# the aggregate keep-awake decision.
MANAGED_AGENTS = ("claude", "codex")


class KeepAwakeBackend:
    """Thin Win32 wrapper so the service logic stays platform-agnostic."""

    def __init__(self, *, platform: str | None = None) -> None:
        self._fn = None
        if (platform if platform is not None else os.name) == "nt":
            try:
                fn = ctypes.windll.kernel32.SetThreadExecutionState
                fn.restype = ctypes.c_uint
                self._fn = fn
            except (AttributeError, OSError):
                self._fn = None

    def acquire(self) -> None:
        if self._fn is not None:
            self._fn(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    def release(self) -> None:
        if self._fn is not None:
            self._fn(ES_CONTINUOUS)


class KeepAwakeService:
    """Aggregate per-agent lifecycle into an idempotent acquire/release switch.

    Only acquires once when the aggregate becomes busy and releases once when
    it becomes idle; repeated busy/idle updates never re-hit the Win32 API.
    """

    def __init__(
        self,
        backend: KeepAwakeBackend | None = None,
        managed_agents: tuple[str, ...] = MANAGED_AGENTS,
    ) -> None:
        self._backend = backend if backend is not None else KeepAwakeBackend()
        self._managed = frozenset(managed_agents)
        self._states: dict[str, LifecycleState] = {}
        self._active = False
        self._enabled = True

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Recompute the aggregate immediately on every state change.

        Disabling releases an in-flight acquire at once but keeps recording
        agent lifecycle. Re-enabling recomputes busy from the latest states and
        acquires right away if any managed agent is still busy.
        """
        self._enabled = bool(enabled)
        self._refresh()

    def on_agent_state(self, state: AgentState) -> None:
        if state.agent_id not in self._managed:
            return
        self._states[state.agent_id] = state.state
        self._refresh()

    def _refresh(self) -> None:
        if not self._enabled:
            if self._active:
                self._backend.release()
                self._active = False
            return
        busy = any(state in BUSY_STATES for state in self._states.values())
        if busy and not self._active:
            self._backend.acquire()
            self._active = True
        elif not busy and self._active:
            self._backend.release()
            self._active = False

    def shutdown(self) -> None:
        if self._active:
            self._backend.release()
            self._active = False
        self._states.clear()
