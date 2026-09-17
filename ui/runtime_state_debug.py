"""Phase 1B shadow consumer for RuntimeBus state-link validation.

Records both the old-path (StateMonitor ``resolved_state_changed``) and
new-path (``RuntimeBus.subscribe_state``) snapshots and compares them for
consistency.  This is a *temporary verification tool* — it does not modify
``PetOverlay``, animation logic, or any state source.

The shadow is wired into ``app.py`` alongside the existing connects and
never disrupts the old signal path.  Once Phase 1B validation passes, the
app.py wiring and this module can be removed together.
"""

from __future__ import annotations

from PySide6.QtCore import QObject

from core.models import ResolvedState
from core.runtime_bus import RuntimeState


class RuntimeStateVerifier(QObject):
    """Records the last snapshot from the old code path (ResolvedState) and
    the new code path (RuntimeState from the bus), then compares them.

    Attributes
    ----------
    last_resolved : ResolvedState | None
        Most recent snapshot from ``resolved_state_changed`` (old path).
    last_runtime_state : RuntimeState | None
        Most recent snapshot from ``RuntimeBus.subscribe_state`` (new path).
    consistency_failures : list[dict]
        Every mismatch found between the two paths.  Empty = fully consistent.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.last_resolved: ResolvedState | None = None
        self.last_runtime_state: RuntimeState | None = None
        self.consistency_failures: list[dict] = []

    # -- feed points (wired in app.py) -----------------------------------

    def on_old_path(self, state: ResolvedState) -> None:
        """Feed from StateMonitor.resolved_state_changed (old path)."""
        self.last_resolved = state
        self._check()

    def on_new_path(self, state: RuntimeState) -> None:
        """Feed from RuntimeBus.subscribe_state (new path)."""
        self.last_runtime_state = state
        self._check()

    # -- consistency -----------------------------------------------------

    def _check(self) -> None:
        """Compare old and new when both are available."""
        if self.last_resolved is None or self.last_runtime_state is None:
            return

        rs = self.last_resolved
        rts = self.last_runtime_state

        expected_state = rs.state.value if hasattr(rs.state, "value") else str(rs.state)

        failures: list[str] = []
        if rts.state != expected_state:
            failures.append(f"state: {rts.state!r} != {expected_state!r}")
        if rts.timestamp != rs.timestamp:
            failures.append(f"timestamp: {rts.timestamp} != {rs.timestamp}")
        if rts.agent_id != rs.agent_id:
            failures.append(f"agent_id: {rts.agent_id} != {rs.agent_id}")
        if rts.source != rs.source:
            failures.append(f"source: {rts.source} != {rs.source}")

        if failures:
            self.consistency_failures.append(
                {
                    "resolved": rs,
                    "runtime_state": rts,
                    "failures": failures,
                }
            )