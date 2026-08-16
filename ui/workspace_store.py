"""Persist Firefly Companion workspace and Quick Ask preferences safely.

This module is intentionally Qt-free so it can be unit-tested without a GUI
runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_DIR / "config"
SETTINGS_FILE = CONFIG_DIR / "ui_settings.json"
MAX_RECENT_WORKSPACES = 5
VALID_EFFORTS = {"low", "medium", "high"}


class WorkspaceStore:
    def __init__(self, settings_file: Path | str | None = None, default_workspace: Path | str | None = None):
        self.settings_file = Path(settings_file) if settings_file else SETTINGS_FILE
        self.default_workspace = Path(default_workspace) if default_workspace else PROJECT_DIR
        self._settings = self._load()
        self._normalize()

    def _load(self) -> dict:
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _normalize(self) -> None:
        current = self._settings.get("current_workspace")
        recents = self._settings.get("recent_workspaces")
        if not isinstance(current, str) or not current.strip():
            current = str(self.default_workspace)
        if not isinstance(recents, list):
            recents = []

        cleaned: list[str] = []
        for item in [current, *recents]:
            if not isinstance(item, str) or not item.strip():
                continue
            value = str(Path(item).expanduser())
            key = os.path.normcase(os.path.normpath(value))
            if any(os.path.normcase(os.path.normpath(x)) == key for x in cleaned):
                continue
            cleaned.append(value)

        effort = str(self._settings.get("quick_ask_effort") or "low").lower()
        if effort not in VALID_EFFORTS:
            effort = "low"
        conversation = self._settings.get("conversation_enabled", True)
        conversation = conversation if isinstance(conversation, bool) else True

        self._settings = {
            "current_workspace": cleaned[0] if cleaned else str(self.default_workspace),
            "recent_workspaces": cleaned[:MAX_RECENT_WORKSPACES],
            "quick_ask_effort": effort,
            "conversation_enabled": conversation,
        }

    @property
    def current_workspace(self) -> Path:
        return Path(self._settings["current_workspace"])

    @property
    def recent_workspaces(self) -> list[Path]:
        return [Path(x) for x in self._settings["recent_workspaces"]]

    @property
    def quick_ask_effort(self) -> str:
        return str(self._settings.get("quick_ask_effort", "low"))

    @property
    def conversation_enabled(self) -> bool:
        return bool(self._settings.get("conversation_enabled", True))

    def set_workspace(self, workspace: Path | str) -> Path:
        path = Path(workspace).expanduser()
        if not path.exists() or not path.is_dir():
            raise ValueError(f"Workspace does not exist: {path}")
        path = path.resolve()

        recents = [path]
        target_key = os.path.normcase(os.path.normpath(str(path)))
        for item in self.recent_workspaces:
            key = os.path.normcase(os.path.normpath(str(item)))
            if key == target_key:
                continue
            recents.append(item)
            if len(recents) >= MAX_RECENT_WORKSPACES:
                break

        self._settings["current_workspace"] = str(path)
        self._settings["recent_workspaces"] = [str(x) for x in recents]
        self.save()
        return path

    def set_quick_ask_effort(self, effort: str) -> None:
        effort = str(effort).lower()
        if effort not in VALID_EFFORTS:
            raise ValueError(f"Unsupported effort: {effort}")
        self._settings["quick_ask_effort"] = effort
        self.save()

    def set_conversation_enabled(self, enabled: bool) -> None:
        self._settings["conversation_enabled"] = bool(enabled)
        self.save()

    def save(self) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_file.with_name(self.settings_file.name + ".tmp")
        tmp.write_text(
            json.dumps(self._settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.settings_file)
