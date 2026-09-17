"""Firefly Extension API v2 — long-lived capability plugins (Phase 1).

Extends the Quick Tools plugin contract (``core.plugin_api``) with:

- background lifecycle: ``start()`` / ``stop()`` in addition to ``open()`` /
  ``shutdown()``;
- service injection: ``RuntimeBus`` + a narrow ``ExtensionMemory`` facade +
  settings arrive through :class:`PluginContext` after Firefly is wired
  (``PluginLoader.sync_services``), so plugins never reach into app.py;
- declarative ``capabilities`` on the manifest.

This module is pure architecture: it contains no camera / vision / hardware
code. Future capability plugins (Vision / Camera / Hardware) implement this
contract inside external packages under a private plugin root.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject

from core.plugin_api import PluginContext, QuickToolPlugin

log = logging.getLogger("firefly.extension_api")

# Backward-compatible status vocabulary. "WORKING"/"ERROR" are understood by
# the runtime aggregator's plugin.status branch; "STARTING"/"READY" are
# deliberately inert there (an idle camera must not light the pet).
EXT_STATUS_OFFLINE = "OFFLINE"
EXT_STATUS_STARTING = "STARTING"
EXT_STATUS_READY = "READY"
EXT_STATUS_WORKING = "WORKING"
EXT_STATUS_ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class PluginStatusEvent:
    """Bus payload for ``kind="plugin.status"``.

    Duck-typed by :class:`core.runtime_state_aggregator.RuntimeStateAggregator`
    (``getattr(payload, "status", ...)``), so publishing it requires no change
    to the aggregator.
    """

    status: str
    message: str = ""


def publish_plugin_status(
    context: PluginContext | None,
    plugin_id: str,
    status: str,
    message: str = "",
) -> None:
    """Publish a ``plugin.status`` RuntimeEvent if a bus is injected.

    No-op when the context has no bus (e.g. before ``sync_services``) so a
    plugin can safely call this at any point in its lifecycle.
    """
    bus = getattr(context, "bus", None) if context is not None else None
    if bus is None:
        return
    from core.runtime_bus import RuntimeEvent  # lazy: avoid hard import coupling

    bus.publish_event(
        RuntimeEvent(
            kind="plugin.status",
            source=plugin_id or "extension",
            timestamp=int(time.time() * 1000),
            payload=PluginStatusEvent(status=status, message=message),
        )
    )


class ExtensionMemory:
    """Narrow read/write facade over a MemoryService for extensions.

    Deliberately exposes only ``search`` and ``remember``; repository /
    adapter / dedup internals stay owned by the ``memory/`` domain. A missing
    or read-only backing service degrades gracefully (empty search, no-op
    remember) instead of raising into the plugin.
    """

    def __init__(self, service: Any = None) -> None:
        self._service = service

    def search(self, query: str, *, limit: int = 5) -> list[Any]:
        fn = getattr(self._service, "search", None)
        if fn is None:
            return []
        try:
            return list(fn(query, limit=limit))[:limit]
        except Exception:
            log.debug("extension memory search failed", exc_info=True)
            return []

    def remember(
        self, content: str, *, category: str = "user_fact", trigger: str = "extension"
    ) -> Any:
        fn = getattr(self._service, "remember", None)
        if fn is None:
            return None
        try:
            # asserted_explicit=True: persist a genuine plugin observation
            # through the normal write pipeline (write policy + dedup gate).
            return fn(
                content, category=category, trigger=trigger, asserted_explicit=True
            )
        except Exception:
            log.debug("extension memory remember failed", exc_info=True)
            return None


class FireflyExtension(QuickToolPlugin):
    """Base class for long-lived capability plugins.

    Lifecycle: ``initialize(context)`` -> ``start()`` -> ``open()*`` ->
    ``stop()`` -> ``shutdown()``. ``start()`` / ``stop()`` are optional and
    only invoked when the plugin defines them; plain QuickTool plugins keep
    the v1 lifecycle untouched.
    """

    capabilities: tuple[str, ...] = ()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._context: PluginContext | None = None

    def initialize(self, context: PluginContext) -> None:
        """Store the injected context; subclasses may override to keep a handle."""
        self._context = context

    def start(self) -> None:
        """Called once after Firefly is fully wired; start background work here."""

    def stop(self) -> None:
        """Called on shutdown before the bus closes; stop background work here."""

    @property
    def context(self) -> PluginContext | None:
        return self._context

    def publish_status(self, status: str, message: str = "") -> None:
        """Emit the card status and, when a bus is injected, a plugin.status event."""
        self._set_status(status, message)
        publish_plugin_status(self._context, self.manifest.id, status, message)
