"""Lightweight in-process registry for Firefly Quick Tools.

The registry deliberately contains no discovery, persistence, networking, or
dynamic imports.  A tool is an application-owned manifest plus an in-process
open handler and status provider.  External Quick Tool plugins register into
this registry through :class:`core.plugin_loader.PluginLoader`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable


log = logging.getLogger("firefly.quick_tools")


@dataclass(frozen=True, slots=True)
class QuickToolManifest:
    id: str
    name: str
    description: str
    icon: str
    version: str = "0.0.0"
    # ---- Extension API v2 optional metadata (all backward compatible) ----
    capabilities: tuple[str, ...] = ()  # "camera" / "vision" / "hardware" / "background"
    min_api: str = "1"  # minimum Extension API version this plugin requires
    config_defaults: dict = field(default_factory=dict)
    author: str = ""
    repository: str = ""

    def __post_init__(self) -> None:
        if not all((self.id.strip(), self.name.strip(), self.description.strip(), self.icon.strip())):
            raise ValueError("Quick Tool manifest fields must be non-empty")


@dataclass(frozen=True, slots=True)
class QuickToolRegistration:
    manifest: QuickToolManifest
    open_handler: Callable[[], None]
    status_provider: Callable[[], str]
    # Optional Qt Signal(str, str) emitted when the tool's status changes.
    status_changed: Any = None
    # Optional short capability line and note shown on the Quick Tools card.
    capability: str = ""
    capability_note: str = ""


class QuickToolsRegistry:
    """Small ordered registry owned by the current Firefly process."""

    def __init__(self) -> None:
        self._tools: dict[str, QuickToolRegistration] = {}

    def register(
        self,
        manifest: QuickToolManifest,
        *,
        open_handler: Callable[[], None],
        status_provider: Callable[[], str],
        status_changed: Any = None,
        capability: str = "",
        capability_note: str = "",
    ) -> None:
        if manifest.id in self._tools:
            raise ValueError(f"Quick Tool already registered: {manifest.id}")
        self._tools[manifest.id] = QuickToolRegistration(
            manifest=manifest,
            open_handler=open_handler,
            status_provider=status_provider,
            status_changed=status_changed,
            capability=capability,
            capability_note=capability_note,
        )

    def all(self) -> tuple[QuickToolRegistration, ...]:
        return tuple(self._tools.values())

    def get(self, tool_id: str) -> QuickToolRegistration | None:
        return self._tools.get(tool_id)

    def open(self, tool_id: str) -> bool:
        registration = self.get(tool_id)
        if registration is None:
            return False
        try:
            registration.open_handler()
        except Exception as exc:
            # Do not include exception text: plugin exceptions can contain
            # provider input or other secrets.  The type and plugin id are
            # enough to diagnose the failing boundary safely.
            log.error(
                "Quick Tool open failed plugin_id=%s exception_type=%s message=handler raised",
                tool_id,
                type(exc).__name__,
            )
            return False
        return True
