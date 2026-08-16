"""Thin application service that wraps the persistent WorkspaceStore.

The manager is the single entry point UI and future services (Agent launch,
Quick Ask, sessions) use to read or change the current workspace. It owns one
WorkspaceStore and does not duplicate any persistence, dedup, or atomic-write
logic. It stays Qt-free so it can be unit-tested without a GUI runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ui.workspace_store import WorkspaceStore


class WorkspaceManager:
    def __init__(self, store: WorkspaceStore | None = None):
        self._store = store if store is not None else WorkspaceStore()
        self._listeners: list[Callable[[Path], None]] = []

    def current(self) -> Path:
        return self._store.current_workspace

    def recents(self) -> list[Path]:
        return self._store.recent_workspaces

    def is_valid(self, workspace: Path | str) -> bool:
        try:
            path = Path(workspace).expanduser()
        except (TypeError, ValueError):
            return False
        return path.is_dir()

    def set_current(self, workspace: Path | str) -> Path:
        """Switch to a validated workspace; raises ValueError for invalid paths."""
        path = self._store.set_workspace(workspace)
        self._notify(path)
        return path

    def add_or_select(self, workspace: Path | str) -> Path | None:
        """Validate then switch. Returns the resolved path or None if invalid."""
        if not self.is_valid(workspace):
            return None
        return self.set_current(workspace)

    def connect(self, callback: Callable[[Path], None]) -> None:
        self._listeners.append(callback)

    def disconnect(self, callback: Callable[[Path], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, path: Path) -> None:
        for listener in list(self._listeners):
            listener(path)
