"""Qt-free, versioned persistence for short-term Firefly conversations.

The store keeps an independent bounded message window and optional summary
text per session. It never generates summaries and has no memory/provider/UI
dependencies.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Sequence


STORE_VERSION = 1
DEFAULT_SESSION_ID = "firefly-main"
DEFAULT_MAX_MESSAGES = 40
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONVERSATION_PATH = (
    PROJECT_DIR / "runtime" / "companion" / "conversation.json"
)
_ALLOWED_ROLES = frozenset({"user", "assistant"})


@dataclass(frozen=True, slots=True)
class Turn:
    """One persisted conversation message."""

    role: str
    content: str
    ts: int

    def __post_init__(self) -> None:
        if self.role not in _ALLOWED_ROLES:
            raise ValueError("role must be 'user' or 'assistant'")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content must be a non-empty string")
        if isinstance(self.ts, bool) or not isinstance(self.ts, int) or self.ts <= 0:
            raise ValueError("ts must be a positive integer timestamp")
        object.__setattr__(self, "content", self.content.strip())

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content, "ts": self.ts}

    def to_chat_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}

    @classmethod
    def from_dict(cls, data: Any) -> Turn:
        if not isinstance(data, Mapping):
            raise ValueError("turn data must be a mapping")
        return cls(role=data.get("role"), content=data.get("content"), ts=data.get("ts"))


@dataclass(frozen=True, slots=True)
class _SessionState:
    turns: tuple[Turn, ...] = ()
    summary: str | None = None


class ConversationStore:
    """Atomic local store for bounded, session-isolated chat windows."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        max_messages: int = DEFAULT_MAX_MESSAGES,
    ) -> None:
        if isinstance(max_messages, bool) or not isinstance(max_messages, int):
            raise ValueError("max_messages must be a positive integer")
        if max_messages <= 0:
            raise ValueError("max_messages must be a positive integer")
        self.path = Path(path) if path is not None else DEFAULT_CONVERSATION_PATH
        self.max_messages = max_messages
        self._active_session_id = _session_id(session_id)
        self._lock = RLock()
        self._sessions = self._load()

    @property
    def active_session_id(self) -> str:
        return self._active_session_id

    def switch_session(self, session_id: str) -> None:
        """Select the default session used by calls without ``session_id``."""
        self._active_session_id = _session_id(session_id)

    def create_session(self, session_id: str) -> str:
        normalized = _session_id(session_id)
        with self._lock:
            if normalized in self._sessions:
                return normalized
            proposed = dict(self._sessions)
            proposed[normalized] = _SessionState()
            self._persist(proposed)
            self._sessions = proposed
        return normalized

    def list_sessions(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions)

    def delete_session(self, session_id: str) -> bool:
        normalized = _session_id(session_id)
        with self._lock:
            if normalized not in self._sessions:
                return False
            proposed = dict(self._sessions)
            del proposed[normalized]
            self._persist(proposed)
            self._sessions = proposed
            return True

    def append_turn(
        self,
        role: str,
        content: str,
        *,
        session_id: str | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        turn = Turn(role=role, content=content, ts=_timestamp(timestamp_ms))
        self._append((turn,), session_id=session_id)

    append_message = append_turn

    def append_exchange(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Atomically append one complete user/assistant exchange."""
        start_ts = _timestamp(timestamp_ms)
        turns = (
            Turn("user", user_content, start_ts),
            Turn("assistant", assistant_content, start_ts + 1),
        )
        self._append(turns, session_id=session_id)

    def load_working_window(
        self, *, session_id: str | None = None
    ) -> list[Turn]:
        normalized = self._resolve_session_id(session_id)
        with self._lock:
            state = self._sessions.get(normalized, _SessionState())
            return list(state.turns[-self.max_messages :])

    load_recent_window = load_working_window

    def window_size(self, *, session_id: str | None = None) -> int:
        return len(self.load_working_window(session_id=session_id))

    def has_history(self, *, session_id: str | None = None) -> bool:
        return self.window_size(session_id=session_id) > 0

    def clear(self, *, session_id: str | None = None) -> None:
        """Clear one session's window and summary without touching other data."""
        normalized = self._resolve_session_id(session_id)
        with self._lock:
            if normalized not in self._sessions:
                return
            proposed = dict(self._sessions)
            proposed[normalized] = _SessionState()
            self._persist(proposed)
            self._sessions = proposed

    def get_summary(self, *, session_id: str | None = None) -> str | None:
        """Return caller-provided summary text; no summary is generated here."""
        normalized = self._resolve_session_id(session_id)
        with self._lock:
            return self._sessions.get(normalized, _SessionState()).summary

    def set_summary(
        self, summary: str | None, *, session_id: str | None = None
    ) -> None:
        """Persist externally generated summary text, or clear it with None."""
        if summary is not None:
            if not isinstance(summary, str):
                raise ValueError("summary must be a string or None")
            summary = summary.strip() or None
        normalized = self._resolve_session_id(session_id)
        with self._lock:
            current = self._sessions.get(normalized, _SessionState())
            proposed = dict(self._sessions)
            proposed[normalized] = _SessionState(current.turns, summary)
            self._persist(proposed)
            self._sessions = proposed

    def _append(
        self, turns: Sequence[Turn], *, session_id: str | None = None
    ) -> None:
        normalized = self._resolve_session_id(session_id)
        with self._lock:
            current = self._sessions.get(normalized, _SessionState())
            bounded = (current.turns + tuple(turns))[-self.max_messages :]
            proposed = dict(self._sessions)
            proposed[normalized] = _SessionState(bounded, current.summary)
            self._persist(proposed)
            self._sessions = proposed

    def _resolve_session_id(self, session_id: str | None) -> str:
        return self._active_session_id if session_id is None else _session_id(session_id)

    def _load(self) -> dict[str, _SessionState]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}
        if not isinstance(data, Mapping):
            return {}
        version = data.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or version > STORE_VERSION
        ):
            return {}
        raw_sessions = data.get("sessions")
        if not isinstance(raw_sessions, Mapping):
            return {}

        sessions: dict[str, _SessionState] = {}
        for raw_id, raw_state in raw_sessions.items():
            try:
                session_id = _session_id(raw_id)
            except ValueError:
                continue
            if not isinstance(raw_state, Mapping):
                continue
            raw_turns = raw_state.get("turns")
            if not isinstance(raw_turns, list):
                continue
            turns: list[Turn] = []
            for item in raw_turns:
                try:
                    turns.append(Turn.from_dict(item))
                except (TypeError, ValueError):
                    continue
            summary = raw_state.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                summary = None
            else:
                summary = summary.strip()
            sessions[session_id] = _SessionState(
                tuple(turns[-self.max_messages :]), summary
            )
        return sessions

    def _persist(self, sessions: Mapping[str, _SessionState]) -> None:
        payload = {
            "version": STORE_VERSION,
            "sessions": {
                session_id: {
                    "turns": [turn.to_dict() for turn in state.turns],
                    "summary": state.summary,
                }
                for session_id, state in sorted(sessions.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self._atomic_replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _atomic_replace(src: Path, dst: Path) -> None:
        """Atomic replace with Windows WinError 5 retry."""
        import time

        max_retries = 5
        for attempt in range(max_retries):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if attempt < max_retries - 1:
                    time.sleep(0.1 * (attempt + 1))
                else:
                    raise


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("session_id must be a non-empty string")
    normalized = value.strip()
    if len(normalized) > 128:
        raise ValueError("session_id must not exceed 128 characters")
    return normalized


def _timestamp(value: int | None) -> int:
    timestamp = time.time_ns() // 1_000_000 if value is None else value
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
        raise ValueError("timestamp_ms must be a positive integer")
    return timestamp


__all__ = [
    "DEFAULT_CONVERSATION_PATH",
    "DEFAULT_MAX_MESSAGES",
    "DEFAULT_SESSION_ID",
    "STORE_VERSION",
    "ConversationStore",
    "Turn",
]
