"""Shadow-mode controller that maps RuntimeActivityState to pet display strings.

Phase 2B adapter between ``RuntimeBus`` (``runtime.activity`` topic) and
the existing ``PetOverlay.apply_state`` API.  The controller never imports
PetOverlay, never touches animations, and never reads files.

**Shadow mode** — the old ``StateMonitor.resolved_state_changed → pet.apply_state``
path continues to run alongside this controller.  After validation the old
path can be removed.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from core.runtime_bus import RuntimeBus, RuntimeEvent
from core.runtime_state_aggregator import RuntimeActivityState


class PetActivityController(QObject):
    """Map ``RuntimeActivityState`` values to pet display strings.

    Subscribes to the ``runtime.activity`` topic on the RuntimeBus event
    channel and emits ``pet_state_changed(str)`` when the mapped display
    string changes.  Deduplicates: identical consecutive strings are never
    re-emitted.

    Additionally emits ``pet_detail_state_changed(str)`` with the unmapped
    six-state value (idle/working/tool_running/waiting_input/success/error)
    so detail-aware consumers (LED hardware) can distinguish TOOL_RUNNING
    from WORKING and WAITING_INPUT from IDLE without altering the four-state
    display contract.
    """

    pet_state_changed = Signal(str)
    pet_detail_state_changed = Signal(str)

    _MAPPING: dict[RuntimeActivityState, str] = {
        RuntimeActivityState.IDLE: "idle",
        RuntimeActivityState.WORKING: "working",
        RuntimeActivityState.TOOL_RUNNING: "working",
        RuntimeActivityState.WAITING_INPUT: "idle",
        RuntimeActivityState.SUCCESS: "success",
        RuntimeActivityState.ERROR: "error",
    }

    _DETAIL_MAPPING: dict[RuntimeActivityState, str] = {
        RuntimeActivityState.IDLE: "idle",
        RuntimeActivityState.WORKING: "working",
        RuntimeActivityState.TOOL_RUNNING: "tool_running",
        RuntimeActivityState.WAITING_INPUT: "waiting_input",
        RuntimeActivityState.SUCCESS: "success",
        RuntimeActivityState.ERROR: "error",
    }

    def __init__(self, bus: RuntimeBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._current_display: str | None = None
        self._current_detail: str | None = None
        self._unsub = bus.subscribe_event(self._on_event)

    def close(self) -> None:
        """Unsubscribe from the bus."""
        self._unsub()

    def _on_event(self, event: RuntimeEvent) -> None:
        if event.kind != "runtime.activity":
            return
        payload = event.payload
        if not isinstance(payload, RuntimeActivityState):
            return
        display = self._MAPPING.get(payload)
        if display is None:
            return
        if display != self._current_display:
            self._current_display = display
            self.pet_state_changed.emit(display)
        detail = self._DETAIL_MAPPING.get(payload)
        if detail is not None and detail != self._current_detail:
            self._current_detail = detail
            self.pet_detail_state_changed.emit(detail)