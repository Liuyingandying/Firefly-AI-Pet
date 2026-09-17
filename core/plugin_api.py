"""Minimal external Quick Tool plugin API.

A plugin is a package under the configured external plugin root (by default
``%LOCALAPPDATA%\\FireflyAI\\plugins\\<name>``) that exposes ``create_plugin``.
Firefly loads it, calls ``initialize(context)``, registers its manifest into
the Quick Tools registry, and drives ``open()`` and ``shutdown()``.

The public core ships only this contract; all provider-specific behaviour
lives inside each external plugin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from core.quick_tools import QuickToolManifest


@dataclass(slots=True)
class PluginContext:
    """Services handed to a plugin at initialize time.

    Phase 1 (construction) populates only ``registry`` and ``parent`` — the
    exact v1 contract. Firefly later calls
    :meth:`core.plugin_loader.PluginLoader.sync_services` to inject ``bus`` /
    ``memory`` / ``settings`` / ``log`` into the *same* instance, so every
    loaded plugin observes the services without a second ``initialize`` call.
    Re-running ``initialize`` would double-own resources in existing plugins,
    so the loader mutates this context instead.
    """

    registry: Any
    parent: QObject | None = None
    # ---- Extension API v2 optional services (filled via sync_services) ----
    bus: Any = None                  # RuntimeBus — publish plugin.status / domain events
    memory: Any = None               # ExtensionMemory facade — search + remember
    settings: Any = None             # SettingsManager / per-plugin settings access
    log: Callable[[str], None] | None = None


class QuickToolPlugin(QObject):
    """Base class for external Quick Tool plugins."""

    status_changed = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._status = "OFFLINE"
        self._message = ""

    @property
    def manifest(self) -> QuickToolManifest:
        raise NotImplementedError

    def initialize(self, context: PluginContext) -> None:
        """Called once after construction; store or use the context here."""

    def open(self) -> None:
        """Called when the user activates the tool in Quick Tools."""

    def shutdown(self) -> None:
        """Called on Firefly exit; release owned resources here."""

    def status(self) -> str:
        return self._status

    def message(self) -> str:
        return self._message

    @property
    def capability(self) -> str:
        return ""

    @property
    def capability_note(self) -> str:
        return ""

    def _set_status(self, status: str, message: str = "") -> None:
        self._status = status
        self._message = message
        self.status_changed.emit(status, message)
