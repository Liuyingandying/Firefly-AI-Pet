"""Pure JSON persistence for Firefly native session records (Phase 8C.4).

SessionStore owns only persistence: ``load`` / ``save`` and the on-disk
schema. Business semantics (per-agent kind, key normalization, when to clear)
live in :class:`core.session_manager.SessionManager`; this module never
imports it, so the round-trip key is always recomputed by the owner, never
re-derived here.

Guarantees:

* atomic write (temp file + ``os.replace``), UTF-8
* versioned top-level schema
* malformed / partial top-level JSON degrades to an empty store, never raises
* a single corrupt record is skipped without losing the others
* unknown future record fields are tolerated
* a conservative total-record cap prunes oldest ``updated_at`` on save

No database, no SQLite, no new dependency. Only the minimal native session id
is stored — never prompts, responses, transcripts, tool arguments, keys,
tokens, environment variables, raw events, or telemetry payloads.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

STORE_VERSION = 1
DEFAULT_MAX_RECORDS = 50


@dataclass(frozen=True, slots=True)
class StoredSession:
    """One persisted session record. ``workspace_key`` is informational only.

    The authoritative lookup key is recomputed by SessionManager from
    ``workspace`` using the same normalization everywhere; ``workspace_key``
    is stored for human-readable debugging and tolerated (never trusted) on
    load.
    """

    agent_id: str
    workspace: str
    kind: str
    native_session_id: str
    created_at: int
    updated_at: int
    workspace_key: str = ""


class SessionStore:
    def __init__(
        self,
        path: Path | str,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> None:
        self.path = Path(path)
        self.max_records = max(1, max_records)

    # -- read ------------------------------------------------------------

    def load(self) -> list[StoredSession]:
        """Return valid records; any unreadable/corrupt file yields []."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []

        version = data.get("version")
        # A future major version means a schema we cannot safely interpret.
        if isinstance(version, int) and version > STORE_VERSION:
            return []

        sessions = data.get("sessions")
        if not isinstance(sessions, list):
            return []

        out: list[StoredSession] = []
        for item in sessions:
            rec = self._parse_record(item)
            if rec is not None:
                out.append(rec)
        return out

    @staticmethod
    def _parse_record(item) -> StoredSession | None:
        if not isinstance(item, dict):
            return None
        agent_id = item.get("agent_id")
        workspace = item.get("workspace")
        native_id = item.get("native_session_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return None
        if not isinstance(workspace, str) or not workspace.strip():
            return None
        if not isinstance(native_id, str) or not native_id.strip():
            return None
        kind = item.get("kind")
        kind = kind if isinstance(kind, str) else ""
        created = item.get("created_at")
        created = created if isinstance(created, int) else 0
        updated = item.get("updated_at")
        updated = updated if isinstance(updated, int) else 0
        ws_key = item.get("workspace_key")
        ws_key = ws_key if isinstance(ws_key, str) else ""
        return StoredSession(
            agent_id=agent_id.strip(),
            workspace=workspace.strip(),
            kind=kind.strip(),
            native_session_id=native_id.strip(),
            created_at=created,
            updated_at=updated,
            workspace_key=ws_key,
        )

    # -- write -----------------------------------------------------------

    def save(self, records: list[StoredSession]) -> None:
        """Persist records atomically; a bounded list, oldest dropped first."""
        ordered = sorted(
            (r for r in records if r is not None),
            key=lambda r: r.updated_at,
            reverse=True,
        )
        kept = ordered[: self.max_records]
        payload = {
            "version": STORE_VERSION,
            "sessions": [
                {
                    "agent_id": r.agent_id,
                    "workspace": r.workspace,
                    "workspace_key": r.workspace_key,
                    "kind": r.kind,
                    "native_session_id": r.native_session_id,
                    "created_at": r.created_at,
                    "updated_at": r.updated_at,
                }
                for r in kept
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
