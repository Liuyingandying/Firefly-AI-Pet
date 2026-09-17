"""Arbitration layer between the legacy StateMonitor path and the new
RuntimeActivity path for PetOverlay display state.

Phase 2C.  The resolver receives two independent inputs, picks the winner
by priority, and emits a single deduplicated display string.  Terminal
states (SUCCESS, ERROR) hold for a configurable timeout and block lower-
priority overrides.  After the timeout the resolver restores the highest-
priority pre-terminal state.

The resolver never reads files, calls models, or touches the network.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

from core.models import LifecycleState, ResolvedState
from core.runtime_state_aggregator import RuntimeActivityState


class _PetState(str, Enum):
    """Normalised display string sent to PetOverlay.apply_state()."""

    IDLE = "idle"
    WORKING = "working"
    SUCCESS = "success"
    ERROR = "error"


# Priority: higher number = higher priority.
_PRIORITY: dict[RuntimeActivityState, int] = {
    RuntimeActivityState.ERROR: 6,
    RuntimeActivityState.SUCCESS: 5,
    RuntimeActivityState.WORKING: 4,
    RuntimeActivityState.TOOL_RUNNING: 3,
    RuntimeActivityState.WAITING_INPUT: 2,
    RuntimeActivityState.IDLE: 1,
}

# Legacy LifecycleState → RuntimeActivityState.
_LEGACY_MAP: dict[LifecycleState, RuntimeActivityState] = {
    LifecycleState.IDLE: RuntimeActivityState.IDLE,
    LifecycleState.THINKING: RuntimeActivityState.WORKING,
    LifecycleState.WORKING: RuntimeActivityState.WORKING,
    LifecycleState.WAITING: RuntimeActivityState.WAITING_INPUT,
    LifecycleState.SUCCESS: RuntimeActivityState.SUCCESS,
    LifecycleState.ERROR: RuntimeActivityState.ERROR,
    LifecycleState.SLEEPING: RuntimeActivityState.IDLE,
}

# Activity display string (from PetActivityController) → RuntimeActivityState.
# Lossy: "working" could be WORKING or TOOL_RUNNING; we map to WORKING (higher
# priority) so the resolver never loses a state change.
_ACTIVITY_REVERSE: dict[str, RuntimeActivityState] = {
    "idle": RuntimeActivityState.IDLE,
    "working": RuntimeActivityState.WORKING,
    "success": RuntimeActivityState.SUCCESS,
    "error": RuntimeActivityState.ERROR,
}

# Detailed activity string (PetActivityController.pet_detail_state_changed)
# → RuntimeActivityState.  Lossless: carries TOOL_RUNNING / WAITING_INPUT.
_DETAIL_REVERSE: dict[str, RuntimeActivityState] = {
    "idle": RuntimeActivityState.IDLE,
    "working": RuntimeActivityState.WORKING,
    "tool_running": RuntimeActivityState.TOOL_RUNNING,
    "waiting_input": RuntimeActivityState.WAITING_INPUT,
    "success": RuntimeActivityState.SUCCESS,
    "error": RuntimeActivityState.ERROR,
}

# RuntimeActivityState → display string (same as PetActivityController).
_DISPLAY: dict[RuntimeActivityState, str] = {
    RuntimeActivityState.IDLE: "idle",
    RuntimeActivityState.WORKING: "working",
    RuntimeActivityState.TOOL_RUNNING: "working",
    RuntimeActivityState.WAITING_INPUT: "idle",
    RuntimeActivityState.SUCCESS: "success",
    RuntimeActivityState.ERROR: "error",
}

# RuntimeActivityState → detailed six-state string (LED hardware detail).
_DETAIL_DISPLAY: dict[RuntimeActivityState, str] = {
    RuntimeActivityState.IDLE: "idle",
    RuntimeActivityState.WORKING: "working",
    RuntimeActivityState.TOOL_RUNNING: "tool_running",
    RuntimeActivityState.WAITING_INPUT: "waiting_input",
    RuntimeActivityState.SUCCESS: "success",
    RuntimeActivityState.ERROR: "error",
}

_TERMINAL_STATES = frozenset(
    {RuntimeActivityState.SUCCESS, RuntimeActivityState.ERROR}
)

_TERMINAL_TIMEOUT_MS = 5_000


class PetStateResolver(QObject):
    """Arbitrate between legacy and activity state sources.

    Parameters
    ----------
    recovery_ms:
        Milliseconds SUCCESS/ERROR hold before recovery (default 5000).
    """

    pet_display_state_changed = Signal(str)
    # Detailed six-state output (idle/working/tool_running/waiting_input/
    # success/error) after the same terminal arbitration.  Emitted in lock
    # step with pet_display_state_changed; consumers map detail states onto
    # richer hardware (LED node) without touching the four-state display.
    pet_detail_state_changed = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        recovery_ms: int = _TERMINAL_TIMEOUT_MS,
    ) -> None:
        super().__init__(parent)
        self._legacy: RuntimeActivityState = RuntimeActivityState.IDLE
        self._activity: RuntimeActivityState = RuntimeActivityState.IDLE
        self._legacy_set = False
        self._activity_set = False
        self._last_display: str | None = None
        self._last_detail: str | None = None
        self._terminal: RuntimeActivityState | None = None
        self._pre_terminal: RuntimeActivityState = RuntimeActivityState.IDLE

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(recovery_ms)
        self._timer.timeout.connect(self._on_recovery)

    # -- public feed methods (wired in app.py) -----------------------------

    def update_legacy_state(self, resolved: ResolvedState) -> None:
        """Feed from ``StateMonitor.resolved_state_changed``."""
        act = _LEGACY_MAP.get(resolved.state)
        if act is None or (self._legacy_set and act == self._legacy):
            return
        self._legacy = act
        self._legacy_set = True
        self._resolve()

    def update_activity_state(self, display: str) -> None:
        """Feed from ``PetActivityController.pet_state_changed`` (4-state)."""
        act = _ACTIVITY_REVERSE.get(display)
        if act is None or (self._activity_set and act == self._activity):
            return
        self._activity = act
        self._activity_set = True
        self._resolve()

    def update_detail_state(self, detail: str) -> None:
        """Feed from ``PetActivityController.pet_detail_state_changed``.

        Lossless variant of :meth:`update_activity_state`: carries
        TOOL_RUNNING / WAITING_INPUT through to the arbitration unchanged,
        so the detailed output (LED) sees the true six-state activity.
        """
        act = _DETAIL_REVERSE.get(detail)
        if act is None or (self._activity_set and act == self._activity):
            return
        self._activity = act
        self._activity_set = True
        self._resolve()

    # -- resolve -----------------------------------------------------------

    def _resolve(self) -> None:
        """Pick the winner and emit if the display string changed."""

        # ---- 1.  pick the higher-priority source --------------------------
        legacy_prio = _PRIORITY.get(self._legacy, 0)
        activity_prio = _PRIORITY.get(self._activity, 0)
        candidate = self._legacy if legacy_prio >= activity_prio else self._activity

        # ---- 2.  terminal check -------------------------------------------
        if self._terminal is not None:
            # Terminal mode active: only another terminal state can override.
            if candidate in _TERMINAL_STATES:
                if candidate != self._terminal:
                    self._enter_terminal(candidate)
                # else: same terminal state — keep the existing timer.
            # else: non-terminal candidate — blocked during terminal.
            return

        # ---- 3.  normal (non-terminal) resolution -------------------------
        if candidate in _TERMINAL_STATES:
            self._enter_terminal(candidate)
        else:
            self._emit(candidate)

    # -- terminal lifecycle -------------------------------------------------

    def _enter_terminal(self, state: RuntimeActivityState) -> None:
        self._terminal = state
        # Pre-terminal: the best non-terminal state from the *other* source.
        other = self._activity if state is self._legacy else self._legacy
        self._pre_terminal = (
            other if other not in _TERMINAL_STATES else RuntimeActivityState.IDLE
        )
        self._timer.start()
        self._emit(state)

    def _on_recovery(self) -> None:
        self._terminal = None
        target = self._pre_terminal
        self._pre_terminal = RuntimeActivityState.IDLE
        self._emit(target)

    # -- emit (with dedup; four-state display and six-state detail) --------

    def _emit(self, state: RuntimeActivityState) -> None:
        display = _DISPLAY.get(state)
        if display is not None and display != self._last_display:
            self._last_display = display
            self.pet_display_state_changed.emit(display)
        detail = _DETAIL_DISPLAY.get(state)
        if detail is not None and detail != self._last_detail:
            self._last_detail = detail
            self.pet_detail_state_changed.emit(detail)