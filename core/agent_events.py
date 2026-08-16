"""Unified AgentEvent model for Firefly Quick Ask / Short Ask turns.

Provider-specific stream parsing lives in :mod:`core.agent_adapters`
(``ClaudeStreamAdapter`` / ``CodexJsonlAdapter``). This module defines only the
neutral vocabulary: event types, a small error taxonomy, optional capability
flags, and the minimal adapter contract. It is Qt-free, never starts a process,
never owns a session, never renders UI, and never emits display language.

Event flow (Phase 8C.2):

    provider CLI line -> adapter.feed_line() -> list[AgentEvent]
                                        -> QuickAskRunner -> SessionManager / UI
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class AgentEventType(str, Enum):
    STARTED = "started"
    SESSION = "session"
    STATUS = "status"
    TEXT_DELTA = "text_delta"
    TOOL = "tool"
    PERMISSION = "permission"
    FINAL = "final"
    ERROR = "error"
    CANCELLED = "cancelled"


class ErrorCategory(str, Enum):
    PROCESS_START = "process_start"
    CONFIGURATION = "configuration"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    AUTH = "auth"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROVIDER = "provider"
    UNKNOWN = "unknown"


# Short-lived, machine-readable STATUS tokens. Display text lives in the UI
# layer (ui/process_launcher.py), never here.
STATUS_CONNECTING = "connecting"
STATUS_THINKING = "thinking"
STATUS_GENERATING = "generating"
STATUS_ORGANIZING = "organizing"
STATUS_READING = "reading"
STATUS_PROCESSING = "processing"
STATUS_RUNNING_TOOL = "running_tool"
STATUS_RECONNECTING = "reconnecting"
STATUS_COMPLETED = "completed"
STATUS_ERROR = "error"

# STATUS tokens that mean the model is actively producing work (telemetry T4).
WORKING_STATUSES = frozenset(
    {
        STATUS_THINKING,
        STATUS_GENERATING,
        STATUS_ORGANIZING,
        STATUS_READING,
        STATUS_PROCESSING,
        STATUS_RUNNING_TOOL,
    }
)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One provider-neutral interaction event.

    ``metadata`` is reserved for small, allowlisted, non-sensitive provider
    information. Raw provider JSON is never embedded here by default.
    """

    agent_id: str
    type: AgentEventType
    timestamp: int
    text: str | None = None
    status: str | None = None
    session_id: str | None = None
    tool_name: str | None = None
    error_code: str | None = None
    metadata: dict | None = field(default=None)

    @classmethod
    def make(cls, agent_id: str, type_: AgentEventType, **kwargs) -> "AgentEvent":
        return cls(agent_id=agent_id, type=type_, timestamp=_now_ms(), **kwargs)


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """Very light capability flags; useful for future provider selection."""

    streaming: bool
    resume: bool
    cancel: bool
    tools: bool
    permissions: bool
    managed_session: bool


class AgentEventAdapter(ABC):
    """Minimal provider protocol -> AgentEvent contract.

    Implementations are stateless between turns except for a single
    ``_saw_error`` latch (used by ``finalize`` to avoid double-reporting).
    Subclasses set ``agent_id`` and implement :meth:`feed_event`.
    """

    agent_id: str = "unknown"

    def __init__(self) -> None:
        self._saw_error = False

    def feed_line(self, line: str) -> list[AgentEvent]:
        """Parse one raw protocol line. Malformed input never crashes."""
        line = line.strip()
        if not line:
            return []
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            self._saw_error = True
            return [
                self.event(
                    AgentEventType.ERROR,
                    text="malformed JSON line",
                    error_code=ErrorCategory.PROTOCOL,
                )
            ]
        if not isinstance(data, dict):
            self._saw_error = True
            return [
                self.event(
                    AgentEventType.ERROR,
                    text="non-object JSON line",
                    error_code=ErrorCategory.PROTOCOL,
                )
            ]
        return self.feed_event(data)

    @abstractmethod
    def feed_event(self, event: dict) -> list[AgentEvent]:
        """Map one already-parsed protocol object to AgentEvents."""

    def finalize(self, exit_code: int) -> list[AgentEvent]:
        """End-of-turn synthesis: report a failed exit if nothing else did."""
        if exit_code != 0 and not self._saw_error:
            self._saw_error = True
            return [
                self.event(
                    AgentEventType.ERROR,
                    text=f"process exited with code {exit_code}",
                    error_code=ErrorCategory.PROVIDER,
                )
            ]
        return []

    def event(self, type_: AgentEventType, **kwargs) -> AgentEvent:
        return AgentEvent.make(self.agent_id, type_, **kwargs)
