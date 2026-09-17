"""Minimal global-hotkey bridge (Windows RegisterHotKey + Qt native events).

Firefly previously had no global-hotkey infrastructure; this adds the
standard Windows mechanism (``RegisterHotKey``/``UnregisterHotKey``) bridged
through Qt's ``QAbstractNativeEventFilter`` — NOT a keyboard hook framework.
A hotkey exists only while registered: permanent ones are registered at
shell startup, temporary ones (e.g. Escape while the PDF selection overlay
is active) are registered on demand and must be unregistered when the
overlay exits, so the key is never swallowed after the mode ends.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter

log = logging.getLogger("firefly.global_hotkey")

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None

# fsModifiers values (WinUser.h)
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

# Virtual-key codes used by the PDF OCR Overlay entry.
VK_ESCAPE = 0x1B
VK_E = 0x45

WM_HOTKEY = 0x0312


class GlobalHotkeyManager(QAbstractNativeEventFilter):
    """Register/Unregister OS-wide hotkeys and dispatch WM_HOTKEY.

    Instances are not QObjects: the callback table replaces Qt signals.
    ``register`` returns the hotkey id (or ``None`` on conflict); the same
    id is passed to the callback when the key combination is pressed.
    """

    def __init__(self) -> None:
        QAbstractNativeEventFilter.__init__(self)
        self._items: dict[int, Callable[[int], None]] = {}
        self._next_id = 0x4000  # avoid the system-reserved id range

    def register(self, modifiers: int, vk: int, callback: Callable[[int], None]) -> int | None:
        """Register one global hotkey. Returns its id, or None on failure
        (e.g. another app owns the combination)."""
        if _user32 is None:
            return None
        hotkey_id = self._next_id
        self._next_id += 1
        if not _user32.RegisterHotKey(None, hotkey_id, int(modifiers), int(vk)):
            log.warning("[Hotkey] RegisterHotKey failed mods=%s vk=%s", modifiers, hex(vk))
            return None
        self._items[hotkey_id] = callback
        log.info("[Hotkey] registered id=%s mods=%s vk=%s", hotkey_id, modifiers, hex(vk))
        return hotkey_id

    def unregister(self, hotkey_id: int) -> None:
        """Unregister one hotkey; safe to call with an unknown/None id."""
        if not hotkey_id or hotkey_id not in self._items:
            return
        if _user32 is not None:
            _user32.UnregisterHotKey(None, hotkey_id)
        del self._items[hotkey_id]
        log.info("[Hotkey] unregistered id=%s", hotkey_id)

    def unregister_all(self) -> None:
        for hotkey_id in list(self._items):
            self.unregister(hotkey_id)

    def nativeEventFilter(self, event_type, message) -> tuple:
        if event_type not in (b"windows_generic_MSG", "windows_generic_MSG"):
            return (False, 0)
        try:
            msg = ctypes.cast(
                int(message), ctypes.POINTER(ctypes.wintypes.MSG)
            ).contents
            if msg.message == WM_HOTKEY:
                hotkey_id = int(msg.wParam)
                callback = self._items.get(hotkey_id)
                if callback is not None:
                    callback(hotkey_id)
                    return (True, 0)
        except Exception:  # noqa: BLE001 - never let the filter crash Qt
            log.exception("[Hotkey] native event filter error")
        return (False, 0)


__all__ = [
    "GlobalHotkeyManager",
    "MOD_ALT",
    "MOD_CONTROL",
    "MOD_SHIFT",
    "MOD_WIN",
    "VK_ESCAPE",
    "VK_E",
]
