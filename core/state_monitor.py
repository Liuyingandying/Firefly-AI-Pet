"""Read-only source monitor and resolved-state compatibility publisher."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

import state_broker

from .models import AgentState, ResolvedState


POLL_INTERVAL_MS = 150


class StateMonitor(QObject):
    """Normalize source snapshots and publish UI-safe state signals.

    Source files are strictly read-only here. The existing broker remains the
    sole authority for the single resolved Pet state and all priority/TTL rules.
    """

    agent_state_changed = Signal(str, object)
    resolved_state_changed = Signal(object)

    def __init__(
        self,
        sources_dir: str | Path = state_broker.SOURCES_DIR,
        state_file: str | Path | None = None,
        *,
        interval_ms: int = POLL_INTERVAL_MS,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.sources_dir = Path(sources_dir)
        self.state_file = (
            Path(state_file)
            if state_file is not None
            else self.sources_dir.parent / "state.json"
        )
        self._last_agent_states: dict[str, AgentState] = {}
        self._last_resolved_write_key: tuple | None = None
        self._last_resolved_signal_key: tuple | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.poll_once)

    @property
    def is_active(self) -> bool:
        return self._timer.isActive()

    def start(self) -> None:
        self.poll_once()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def poll_once(self, now_ms: int | None = None) -> ResolvedState | None:
        """Poll all sources once; malformed or missing files are ignored safely."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)

        for agent_id in state_broker.AGENTS:
            agent_state = self._read_agent_state(agent_id)
            if agent_state is None:
                continue
            previous = self._last_agent_states.get(agent_id)
            self._last_agent_states[agent_id] = agent_state
            if previous is None or previous.state != agent_state.state:
                self.agent_state_changed.emit(agent_id, agent_state)

        try:
            payload = state_broker.resolve(now_ms=now_ms, sources_dir=self.sources_dir)
            resolved = ResolvedState.from_payload(payload)
        except (OSError, OverflowError, TypeError, ValueError):
            return None

        key = (
            resolved.state,
            resolved.agent_id,
            resolved.source,
            resolved.timestamp,
        )
        if key != self._last_resolved_write_key:
            self._last_resolved_write_key = key
            self._write_resolved(resolved)

        signal_key = (resolved.state, resolved.agent_id)
        if signal_key != self._last_resolved_signal_key:
            self._last_resolved_signal_key = signal_key
            self.resolved_state_changed.emit(resolved)
        return resolved

    def _read_agent_state(self, agent_id: str) -> AgentState | None:
        path = self.sources_dir / f"{agent_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return AgentState.from_payload(agent_id, payload)
        except (OSError, OverflowError, UnicodeError, json.JSONDecodeError, ValueError):
            return None

    def _write_resolved(self, resolved: ResolvedState) -> None:
        """Atomically preserve the existing runtime/state.json contract."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_name(self.state_file.name + ".tmp")
            temporary.write_text(
                json.dumps(resolved.to_payload(), indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_file)
        except OSError:
            pass
