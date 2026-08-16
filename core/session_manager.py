"""Canonical per-agent, per-workspace native session ownership for Firefly.

SessionManager is the single core service that remembers which native
Claude session / Codex thread belongs to which (agent, workspace) pair. It is
deliberately Qt-free (callback listeners, no QWidget), never renders UI text,
and never executes a CLI. Provider native semantics are preserved: Claude is a
"session", Codex is a "thread".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.session_store import SessionStore, StoredSession


KIND_SESSION = "session"
KIND_THREAD = "thread"

# Provider-native kind per agent. Unknown agents default to "session".
AGENT_KINDS = {
    "claude": KIND_SESSION,
    "codex": KIND_THREAD,
}

# Reliable "this resume id is no longer valid" markers from provider CLIs.
# Only these justify clearing a persisted session; network / auth / transport
# errors must never match (see classify_stale_resume). The last two were
# verified against claude.exe 2.1.233 print-mode stderr on an invalid
# --resume: "--resume requires a valid session ID or session title when used
# with --print ... Provided value ... does not match any session title."
_RESUME_STALE_MARKERS = (
    "session not found",
    "could not find session",
    "unable to find session",
    "no session with id",
    "no session found",
    "invalid session",
    "unknown session",
    "session does not exist",
    "session no longer exists",
    "session expired",
    "expired session",
    "session is no longer valid",
    "requires a valid session id or session title",
    "does not match any session title",
)


def classify_stale_resume(text: str | None) -> bool:
    """True only for errors that prove the native resume id is invalid."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _RESUME_STALE_MARKERS)


def normalized_workspace(workspace: Path | str) -> str:
    """Return a stable, case-normalized absolute key for a workspace path."""
    path = str(Path(workspace).expanduser().resolve())
    return os.path.normcase(os.path.normpath(path))


def workspace_key(agent: str, workspace: Path | str) -> str:
    """Human-readable agent+workspace key (mirrors the Phase 7B scheme)."""
    return f"{str(agent).lower()}|{normalized_workspace(workspace)}"


@dataclass(frozen=True, slots=True)
class SessionKey:
    agent_id: str
    workspace_key: str


@dataclass(frozen=True, slots=True)
class SessionRef:
    agent_id: str
    workspace: Path
    native_session_id: str
    kind: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class SessionChange:
    """Structured change notification; ref is None when a session is cleared."""

    agent_id: str
    ref: SessionRef | None


class SessionManager:
    def __init__(self, store: SessionStore | None = None) -> None:
        self._refs: dict[tuple[str, str], SessionRef] = {}
        self._listeners: list[Callable[[SessionChange], None]] = []
        self._store = store
        # Keys restored from the store on load(); used to report a session's
        # provenance (memory vs persisted) without leaking the id anywhere.
        self._loaded_keys: set[tuple[str, str]] = set()

    # -- persistence -----------------------------------------------------

    def load(self) -> int:
        """Restore persisted sessions into memory; in-memory refs win.

        Returns the number of records restored. Safe to call any time; emits
        a SessionChange per restored record so connected UI refreshes.
        """
        if self._store is None:
            return 0
        restored = 0
        for rec in self._store.load():
            try:
                workspace = Path(rec.workspace)
            except (OSError, ValueError):
                continue
            key = (rec.agent_id, normalized_workspace(workspace))
            if key in self._refs:
                continue
            kind = rec.kind or AGENT_KINDS.get(rec.agent_id, KIND_SESSION)
            ref = SessionRef(
                agent_id=rec.agent_id,
                workspace=workspace,
                native_session_id=rec.native_session_id,
                kind=kind,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
            )
            self._refs[key] = ref
            self._loaded_keys.add(key)
            restored += 1
            self._emit(SessionChange(rec.agent_id, ref))
        return restored

    def source(self, agent: str, workspace: Path | str) -> str | None:
        """Session provenance: "persisted" (restored from store), "memory",
        or None when no session exists. Never exposes the id itself."""
        key = (str(agent).lower(), normalized_workspace(workspace))
        if key not in self._refs:
            return None
        return "persisted" if key in self._loaded_keys else "memory"

    def _persist(self) -> None:
        if self._store is None:
            return
        try:
            self._store.save([self._to_stored(ref) for ref in self._refs.values()])
        except OSError:
            pass  # persistence failure must never break session semantics

    @staticmethod
    def _to_stored(ref: SessionRef) -> StoredSession:
        return StoredSession(
            agent_id=ref.agent_id,
            workspace=str(ref.workspace),
            workspace_key=workspace_key(ref.agent_id, ref.workspace),
            kind=ref.kind,
            native_session_id=ref.native_session_id,
            created_at=ref.created_at,
            updated_at=ref.updated_at,
        )

    # -- mutation -------------------------------------------------------

    def set(
        self,
        agent: str,
        workspace: Path | str,
        native_session_id: str | None,
        kind: str | None = None,
    ) -> SessionRef | None:
        """Record a native session; invalid IDs are a safe no-op."""
        if not isinstance(native_session_id, str) or not native_session_id.strip():
            return None
        native_session_id = native_session_id.strip()
        agent_id = str(agent).lower()
        resolved = Path(workspace).expanduser().resolve()
        ws = os.path.normcase(os.path.normpath(str(resolved)))
        if kind is None:
            kind = AGENT_KINDS.get(agent_id, KIND_SESSION)

        key = (agent_id, ws)
        existing = self._refs.get(key)
        if (
            existing is not None
            and existing.native_session_id == native_session_id
            and existing.kind == kind
        ):
            return existing

        now = int(time.time() * 1000)
        ref = SessionRef(
            agent_id=agent_id,
            workspace=resolved,
            native_session_id=native_session_id,
            kind=kind,
            created_at=now,
            updated_at=now,
        )
        self._refs[key] = ref
        self._loaded_keys.discard(key)
        self._persist()
        self._emit(SessionChange(agent_id, ref))
        return ref

    # -- reads ----------------------------------------------------------

    def get(self, agent: str, workspace: Path | str) -> SessionRef | None:
        return self._refs.get((str(agent).lower(), normalized_workspace(workspace)))

    def get_native_id(self, agent: str, workspace: Path | str) -> str | None:
        ref = self.get(agent, workspace)
        return ref.native_session_id if ref is not None else None

    def has(self, agent: str, workspace: Path | str) -> bool:
        return self.get(agent, workspace) is not None

    def clear(
        self,
        agent: str | None = None,
        workspace: Path | str | None = None,
    ) -> int:
        agent_lower = str(agent).lower() if agent is not None else None
        ws = normalized_workspace(workspace) if workspace is not None else None
        removed = 0
        for key in list(self._refs):
            key_agent, key_ws = key
            if agent_lower is not None and key_agent != agent_lower:
                continue
            if ws is not None and key_ws != ws:
                continue
            del self._refs[key]
            self._loaded_keys.discard(key)
            removed += 1
            self._emit(SessionChange(key_agent, None))
        if removed:
            self._persist()
        return removed

    def for_workspace(self, workspace: Path | str) -> dict[str, SessionRef]:
        ws = normalized_workspace(workspace)
        result: dict[str, SessionRef] = {}
        for (agent_id, key_ws), ref in self._refs.items():
            if key_ws == ws:
                result[agent_id] = ref
        return result

    # -- listeners ------------------------------------------------------

    def connect(self, callback: Callable[[SessionChange], None]) -> None:
        self._listeners.append(callback)

    def disconnect(self, callback: Callable[[SessionChange], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _emit(self, change: SessionChange) -> None:
        for listener in list(self._listeners):
            listener(change)
