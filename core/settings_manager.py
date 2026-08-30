"""Qt-free persistence for Firefly product preferences (Phase 8B.4).

Reads and writes only Firefly's own pet preferences in a dedicated JSON file.
The file is intentionally separate from WorkspaceStore's ui_settings.json so
the two services can never overwrite each other's fields. Tolerates a missing
or malformed config by falling back to defaults. Qt-free application service:
callback listeners, no QWidget, no UI control.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable


PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_DIR / "config"
PREFERENCES_FILE = CONFIG_DIR / "pet_preferences.json"
_USER_CONFIG_ROOT = Path(
    os.environ.get("LOCALAPPDATA") or (Path.home() / ".config")
)
SCREEN_VISION_PREFERENCES_FILE = (
    _USER_CONFIG_ROOT / "Firefly_AI_Pet" / "screen_vision.json"
)

DEFAULT_PREFERENCES = {
    "notifications_enabled": True,
    "keep_awake_enabled": True,
    "greeting_on_startup": True,
    "ui_scale": 1.0,
    "screen_vision_fast_mode": True,
}


def _sanitize_ui_scale(value) -> float:
    """Return a valid float for ui_scale, falling back to 1.0 on garbage.

    Only type/NaN is normalized here; the authoritative MIN/MAX clamp lives in
    ``ui.theme.set_ui_scale`` (kept out of core to avoid a core->ui dependency).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return 1.0
    return value


class SettingsManager:
    """Load, mutate, and persist Firefly product preferences."""

    def __init__(
        self,
        preferences_file: Path | str | None = None,
        screen_vision_preferences_file: Path | str | None = None,
    ):
        self.preferences_file = Path(preferences_file) if preferences_file else PREFERENCES_FILE
        self.screen_vision_preferences_file = (
            Path(screen_vision_preferences_file)
            if screen_vision_preferences_file is not None
            else (
                self.preferences_file
                if preferences_file is not None
                else SCREEN_VISION_PREFERENCES_FILE
            )
        )
        self._preferences = dict(DEFAULT_PREFERENCES)
        self._listeners: list[Callable[[dict], None]] = []
        self._preferences = self._load()
        if self.screen_vision_preferences_file != self.preferences_file:
            screen_settings = self._load_file(self.screen_vision_preferences_file)
            fast_mode = screen_settings.get("screen_vision_fast_mode")
            if isinstance(fast_mode, bool):
                self._preferences["screen_vision_fast_mode"] = fast_mode
        self._normalize()

    # -- state ----------------------------------------------------------

    def _load(self) -> dict:
        return self._load_file(self.preferences_file)

    @staticmethod
    def _load_file(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _normalize(self) -> None:
        merged = dict(DEFAULT_PREFERENCES)
        for key, default in DEFAULT_PREFERENCES.items():
            value = self._preferences.get(key, default)
            if key == "ui_scale":
                merged[key] = _sanitize_ui_scale(value)
            elif isinstance(value, bool):
                merged[key] = value
        # Preserve unknown keys so future preference namespaces survive rewrites.
        for key, value in self._preferences.items():
            if key not in merged:
                merged[key] = value
        self._preferences = merged

    # -- preferences ----------------------------------------------------

    @property
    def notifications_enabled(self) -> bool:
        return bool(self._preferences["notifications_enabled"])

    @property
    def keep_awake_enabled(self) -> bool:
        return bool(self._preferences["keep_awake_enabled"])

    @property
    def greeting_on_startup(self) -> bool:
        return bool(self._preferences["greeting_on_startup"])

    @property
    def screen_vision_fast_mode(self) -> bool:
        """FAST routing for screen vision (DeepSeek-first). Default ON."""
        return bool(self._preferences["screen_vision_fast_mode"])

    def set_screen_vision_fast_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._preferences.get("screen_vision_fast_mode") == enabled:
            return
        self._preferences["screen_vision_fast_mode"] = enabled
        if self.screen_vision_preferences_file == self.preferences_file:
            self.save()
        else:
            self._save_file(
                self.screen_vision_preferences_file,
                {"screen_vision_fast_mode": enabled},
            )
        self._notify()

    def ui_scale(self) -> float:
        return float(self._preferences["ui_scale"])

    def set_notifications_enabled(self, enabled: bool) -> None:
        self._set("notifications_enabled", enabled)

    def set_keep_awake_enabled(self, enabled: bool) -> None:
        self._set("keep_awake_enabled", enabled)

    def set_greeting_on_startup(self, enabled: bool) -> None:
        self._set("greeting_on_startup", enabled)

    def set_ui_scale(self, value: float) -> None:
        value = _sanitize_ui_scale(value)
        if self._preferences.get("ui_scale") == value:
            return
        self._preferences["ui_scale"] = value
        self.save()
        self._notify()

    def _set(self, key: str, value: bool) -> None:
        value = bool(value)
        if self._preferences.get(key) == value:
            return
        self._preferences[key] = value
        self.save()
        self._notify()

    # -- listeners ------------------------------------------------------

    def connect(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def disconnect(self, callback: Callable[[dict], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self) -> None:
        snapshot = dict(self._preferences)
        for listener in list(self._listeners):
            listener(snapshot)

    # -- persistence ----------------------------------------------------

    def save(self) -> None:
        self._save_file(self.preferences_file, self._preferences)

    @staticmethod
    def _save_file(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
