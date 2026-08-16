"""Launch-on-startup owner for Firefly (current-user Startup folder).

The Startup shortcut is built from the exact same :func:`core.windows_shortcuts.
launch_spec` as the desktop shortcut, so the two launch definitions can never
drift. The source of truth for "enabled" is the *existence* of the Startup
shortcut on disk — not a preference flag — so a shortcut the user deletes by
hand is reported as off, and Settings always reflects real system state.

No Registry ``Run`` key, no admin rights: only a ``Firefly AI Pet.lnk`` in the
current user's ``Start Menu\\Programs\\Startup`` folder.
"""

from __future__ import annotations

from pathlib import Path

from core import windows_shortcuts

_default_folder: Path | None = None
_default_resolved = False


def default_startup_folder() -> Path | None:
    """The current user's Startup folder, resolved lazily and memoized."""
    global _default_folder, _default_resolved
    if not _default_resolved:
        _default_folder = windows_shortcuts.special_folder("Startup")
        _default_resolved = True
    return _default_folder


class WindowsAutostart:
    """Owns the Startup shortcut for one (possibly injected) Startup folder."""

    def __init__(self, startup_folder: Path | str | None = None):
        self.startup_folder = Path(startup_folder) if startup_folder is not None else None

    def _folder(self) -> Path | None:
        if self.startup_folder is not None:
            return self.startup_folder
        return default_startup_folder()

    def shortcut_path(self) -> Path | None:
        folder = self._folder()
        if folder is None:
            return None
        return folder / f"{windows_shortcuts.SHORTCUT_NAME}.lnk"

    def is_enabled(self) -> bool:
        return windows_shortcuts.shortcut_exists(self.shortcut_path())

    def enable(self) -> None:
        path = self.shortcut_path()
        if path is None:
            raise RuntimeError("Startup folder unavailable.")
        windows_shortcuts.create_shortcut(path, windows_shortcuts.launch_spec())

    def disable(self) -> bool:
        return windows_shortcuts.remove_shortcut(self.shortcut_path())


_autostart = WindowsAutostart()


def is_enabled() -> bool:
    return _autostart.is_enabled()


def enable() -> None:
    _autostart.enable()


def disable() -> bool:
    return _autostart.disable()
