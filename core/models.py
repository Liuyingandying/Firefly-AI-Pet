"""Typed lifecycle models shared by Firefly core services and UI wiring."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class LifecycleState(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    WAITING = "waiting"
    SUCCESS = "success"
    ERROR = "error"
    SLEEPING = "sleeping"


@dataclass(frozen=True, slots=True)
class AgentState:
    agent_id: str
    state: LifecycleState
    timestamp: int
    source: str

    @classmethod
    def from_payload(cls, agent_id: str, payload: Mapping[str, Any]) -> "AgentState":
        raw_state = payload.get("state")
        try:
            state = LifecycleState(raw_state)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unsupported lifecycle state: {raw_state!r}") from exc

        timestamp = payload.get("timestamp", 0)
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or (isinstance(timestamp, float) and not math.isfinite(timestamp))
        ):
            raise ValueError("Lifecycle timestamp must be numeric")

        source = payload.get("source", "manual")
        if not isinstance(source, str):
            raise ValueError("Lifecycle source must be a string")

        return cls(agent_id, state, int(timestamp), source)


@dataclass(frozen=True, slots=True)
class ResolvedState:
    state: LifecycleState
    agent_id: str | None
    source: str
    timestamp: int
    resolved_at: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ResolvedState":
        try:
            state = LifecycleState(payload.get("state"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Broker returned an unsupported lifecycle state") from exc

        agent_id = payload.get("agent")
        if agent_id is not None and not isinstance(agent_id, str):
            raise ValueError("Resolved agent must be a string or None")

        source = payload.get("source", "manual")
        timestamp = payload.get("timestamp", 0)
        resolved_at = payload.get("resolved_at", 0)
        if not isinstance(source, str):
            raise ValueError("Resolved source must be a string")
        for name, value in (("timestamp", timestamp), ("resolved_at", resolved_at)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise ValueError(f"Resolved {name} must be numeric")

        return cls(state, agent_id, source, int(timestamp), int(resolved_at))

    def to_payload(self) -> dict[str, Any]:
        """Return the existing runtime/state.json schema without additions."""
        return {
            "state": self.state.value,
            "agent": self.agent_id,
            "source": self.source,
            "timestamp": self.timestamp,
            "resolved_at": self.resolved_at,
        }
