"""Provider protocol parsers that emit neutral :class:`AgentEvent` objects.

Each adapter knows exactly one provider's stream format and nothing else: no
process management, no session ownership, no UI, no display language.
QuickAskRunner selects one adapter per turn via :func:`make_adapter`.
"""

from __future__ import annotations

from .agent_events import (
    STATUS_COMPLETED,
    STATUS_CONNECTING,
    STATUS_ERROR,
    STATUS_GENERATING,
    STATUS_ORGANIZING,
    STATUS_PROCESSING,
    STATUS_READING,
    STATUS_RECONNECTING,
    STATUS_RUNNING_TOOL,
    STATUS_THINKING,
    AgentCapabilities,
    AgentEvent,
    AgentEventAdapter,
    AgentEventType,
    ErrorCategory,
)

# Codex provider/network retry markers observed in the 9D.6-H1 controlled exec
# smoke ("Reconnecting… 2/5..5/5 (request timed out)"). Only events carrying one
# of these are downgraded to a reconnecting STATUS; everything else stays a hard
# ERROR. Deliberately narrow — never swallows a real turn.failed / fatal error.
_RECONNECT_HINTS = ("reconnecting",)


def _is_transient_reconnect(message: str) -> bool:
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _RECONNECT_HINTS)


def _extract_claude_text(event: dict) -> str | None:
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts) or None


class ClaudeStreamAdapter(AgentEventAdapter):
    """Claude Code ``--output-format stream-json`` (print mode)."""

    agent_id = "claude"

    def feed_event(self, event: dict) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id and not self._session_emitted():
            out.append(self.event(AgentEventType.SESSION, session_id=session_id))

        kind = event.get("type")

        if kind == "system":
            out.append(self.event(AgentEventType.STATUS, status=STATUS_CONNECTING))
            return out

        if kind == "stream_event":
            stream_event = event.get("event")
            if isinstance(stream_event, dict):
                event_type = stream_event.get("type")
                delta = stream_event.get("delta")
                if event_type == "content_block_delta" and isinstance(delta, dict):
                    if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                        out.append(self.event(AgentEventType.TEXT_DELTA, text=delta["text"]))
                        out.append(self.event(AgentEventType.STATUS, status=STATUS_GENERATING))
                    elif delta.get("type") in {"thinking_delta", "signature_delta"}:
                        out.append(self.event(AgentEventType.STATUS, status=STATUS_THINKING))
                elif event_type == "message_start":
                    out.append(self.event(AgentEventType.STATUS, status=STATUS_THINKING))
            return out

        if kind == "assistant":
            text = _extract_claude_text(event)
            if text:
                out.append(self.event(AgentEventType.FINAL, text=text))
            out.append(self.event(AgentEventType.STATUS, status=STATUS_ORGANIZING))
            return out

        if kind == "result":
            if event.get("is_error"):
                result = event.get("result")
                message = str(result or event.get("subtype") or "Claude query failed")
                self._saw_error = True
                out.append(self.event(AgentEventType.ERROR, text=message, error_code=ErrorCategory.PROVIDER))
                out.append(self.event(AgentEventType.STATUS, status=STATUS_ERROR))
            else:
                result = event.get("result")
                if isinstance(result, str):
                    out.append(self.event(AgentEventType.FINAL, text=result))
                out.append(self.event(AgentEventType.STATUS, status=STATUS_COMPLETED))
            return out

        return out

    def _session_emitted(self) -> bool:
        """Latch so a session id seen on many lines emits one SESSION event."""
        if not hasattr(self, "_session_latched"):
            self._session_latched = False
        if self._session_latched:
            return True
        self._session_latched = True
        return False


class CodexJsonlAdapter(AgentEventAdapter):
    """Codex CLI ``exec --json`` (JSONL) events."""

    agent_id = "codex"

    def feed_event(self, event: dict) -> list[AgentEvent]:
        kind = event.get("type")
        out: list[AgentEvent] = []

        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id and not self._session_emitted():
                out.append(self.event(AgentEventType.SESSION, session_id=thread_id))
            out.append(self.event(AgentEventType.STATUS, status=STATUS_CONNECTING))
            return out

        if kind == "turn.started":
            out.append(self.event(AgentEventType.STATUS, status=STATUS_THINKING))
            return out

        if kind == "turn.failed":
            message = event.get("message") or event.get("error")
            if isinstance(message, dict):
                message = message.get("message") or str(message)
            self._saw_error = True
            out.append(
                self.event(
                    AgentEventType.ERROR,
                    text=str(message or "Codex turn failed"),
                    error_code=ErrorCategory.PROVIDER,
                )
            )
            out.append(self.event(AgentEventType.STATUS, status=STATUS_ERROR))
            return out

        if kind == "error":
            message = event.get("message") or event.get("error")
            if isinstance(message, dict):
                message = message.get("message") or str(message)
            text = str(message or "Codex turn failed")
            if _is_transient_reconnect(text):
                # Transient provider/network retry; the turn may still complete
                # (9D.6-H1 smoke). Surface it as a status, never a hard ERROR.
                out.append(self.event(AgentEventType.STATUS, status=STATUS_RECONNECTING))
                return out
            self._saw_error = True
            out.append(
                self.event(
                    AgentEventType.ERROR,
                    text=text,
                    error_code=ErrorCategory.PROVIDER,
                )
            )
            out.append(self.event(AgentEventType.STATUS, status=STATUS_ERROR))
            return out

        if kind in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                return out
            item_type = item.get("type")
            if item_type == "agent_message" and kind == "item.completed":
                text = item.get("text")
                if isinstance(text, str):
                    out.append(self.event(AgentEventType.FINAL, text=text))
                out.append(self.event(AgentEventType.STATUS, status=STATUS_ORGANIZING))
            elif item_type == "command_execution":
                out.append(self.event(AgentEventType.TOOL, tool_name="command_execution"))
                out.append(
                    self.event(
                        AgentEventType.STATUS,
                        status=STATUS_READING if kind == "item.started" else STATUS_ORGANIZING,
                    )
                )
            elif item_type == "reasoning":
                out.append(self.event(AgentEventType.STATUS, status=STATUS_THINKING))
            elif item_type in {"file_change", "mcp_tool_call", "web_search"}:
                out.append(self.event(AgentEventType.TOOL, tool_name=str(item_type)))
                out.append(self.event(AgentEventType.STATUS, status=STATUS_PROCESSING))
            elif item_type == "plan_update":
                out.append(self.event(AgentEventType.STATUS, status=STATUS_PROCESSING))
            return out

        if kind == "turn.completed":
            out.append(self.event(AgentEventType.STATUS, status=STATUS_COMPLETED))
        return out

    def _session_emitted(self) -> bool:
        if not hasattr(self, "_session_latched"):
            self._session_latched = False
        if self._session_latched:
            return True
        self._session_latched = True
        return False


CLAUDE_CAPABILITIES = AgentCapabilities(
    streaming=True,
    resume=True,
    cancel=True,
    tools=False,
    permissions=False,
    managed_session=True,
)

CODEX_CAPABILITIES = AgentCapabilities(
    streaming=False,
    resume=True,
    cancel=True,
    tools=True,
    permissions=False,
    managed_session=True,
)


def make_adapter(agent_id: str) -> AgentEventAdapter | None:
    """Return the adapter for an agent, or None for unknown agents."""
    agent = str(agent_id).lower()
    if agent == "claude":
        return ClaudeStreamAdapter()
    if agent == "codex":
        return CodexJsonlAdapter()
    return None


def capabilities_for(agent_id: str) -> AgentCapabilities | None:
    agent = str(agent_id).lower()
    if agent == "claude":
        return CLAUDE_CAPABILITIES
    if agent == "codex":
        return CODEX_CAPABILITIES
    return None
