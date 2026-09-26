"""External Quick Tool plugin loader.

Scans the configured plugin root plus any ``FIREFLY_PLUGIN_PATH`` roots
for plugin packages, loads them, and registers their manifests into the Quick
Tools registry.  A missing root is not an error (Firefly starts normally); a
malformed plugin is isolated so it never takes Firefly down.

Plugin layout: each plugin root contains one directory per plugin.  A plugin
directory must be a Python package (``__init__.py``) exposing
``create_plugin(parent=None) -> QuickToolPlugin`` in ``plugin.py``.
"""

from __future__ import annotations

import ast
import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject

from core.plugin_api import PluginContext, QuickToolPlugin
from core.path_config import PLUGIN_ROOT_ENV, load_path_config
from core.quick_tools import QuickToolManifest, QuickToolsRegistry

log = logging.getLogger("firefly.plugin_loader")

DEFAULT_PLUGIN_ROOT = load_path_config().plugin_root
PLUGIN_PATH_ENV = "FIREFLY_PLUGIN_PATH"

# Managed plugin catalog — the ONLY plugins Firefly registers and runs.
# Discovery stays broad (scan whatever roots exist), but registration is
# gated by this allowlist: an unmanaged plugin is never imported, executed,
# registered, started, shown, or persisted. This replaces any legacy
# "exclude plugin X" blacklist: the loader only needs to know which plugins
# are supported, never which ones were retired.
MANAGED_PLUGIN_IDS: tuple[str, ...] = (
    "firefly-video",
    "firefly-camera-vision",
    "learning-focus",
    "tju-info-retrieval",
    "firefly-voice",
)

# Top-level packages extensions may not import. ``app`` is the shell singleton
# (the real integrity boundary); ``ui`` is deliberately NOT blocked because the
# existing private QuickTool plugin imports ``ui.theme`` — a legacy path kept
# for compatibility until plugins migrate to a thin public root.
_BLOCKED_IMPORTS: tuple[str, ...] = ("app",)


class _BlockedImportFinder:
    """Meta-path guard rejecting forbidden top-level imports.

    Scoped to the ``load()`` call so Firefly's own modules (which legitimately
    import ``app`` / ``ui``) are never affected; the guard is removed in a
    ``finally`` block.
    """

    def __init__(self, blocked: tuple[str, ...]) -> None:
        self._blocked = set(blocked)

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".")[0]
        if root in self._blocked:
            raise ImportError(
                f"import of '{fullname}' is not allowed from an extension"
            )
        return None


class PluginLoader(QObject):
    """Load external Quick Tool plugins into a :class:`QuickToolsRegistry`."""

    def __init__(
        self,
        registry: QuickToolsRegistry,
        parent: QObject | None = None,
        *,
        persistence: Any = None,
    ) -> None:
        super().__init__(parent)
        self._registry = registry
        self._plugins: list[QuickToolPlugin] = []
        self._plugins_by_id: dict[str, QuickToolPlugin] = {}
        self._context = PluginContext(registry=registry, parent=parent)
        # In-process enablement source of truth. Persistence (when provided)
        # only layers on top — a disabled plugin stays disabled even without a
        # persistence store.
        self._enabled: dict[str, bool] = {}
        # Optional persisted enablement store (any object exposing
        # ``plugin_enabled(id) -> bool`` / ``set_plugin_enabled(id, bool)``,
        # e.g. core.settings_manager.SettingsManager). None = default enabled.
        self._persistence = persistence

    # -- plugin enablement (single source of truth) -----------------------

    def is_plugin_enabled(self, plugin_id: str) -> bool:
        """Whether the user enabled this plugin (persisted, default True)."""
        if plugin_id in self._enabled:
            return bool(self._enabled[plugin_id])
        if self._persistence is not None:
            return bool(self._persistence.plugin_enabled(plugin_id))
        return True

    def plugin(self, plugin_id: str) -> QuickToolPlugin | None:
        """Read-only accessor for a loaded plugin instance (by id)."""
        return self._plugins_by_id.get(plugin_id)

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> bool:
        """Record the new enablement (in-process + persisted when available)
        and hot-apply it when the plugin has a live start/stop lifecycle.
        Returns True when the change took effect live, False when it only
        recorded (no hot lifecycle)."""
        enabled = bool(enabled)
        self._enabled[plugin_id] = enabled
        if self._persistence is not None:
            self._persistence.set_plugin_enabled(plugin_id, enabled)
        plugin = self._plugins_by_id.get(plugin_id)
        if plugin is None:
            return False
        if enabled:
            start = getattr(plugin, "start", None)
            if not callable(start):
                return True  # exposure-only plugin: nothing to activate
            try:
                start()
                return True
            except Exception:  # noqa: BLE001 - isolated per plugin
                log.error("plugin start failed id=%s", plugin_id)
                return False
        stop = getattr(plugin, "stop", None)
        if not callable(stop):
            return True  # no resident resources; exposure-only
        try:
            stop()
            return True
        except Exception:  # noqa: BLE001 - isolated per plugin
            log.error("plugin stop failed id=%s", plugin_id)
            return False

    @staticmethod
    def _probe_plugin_id(plugin_dir: Path) -> str | None:
        """Read a plugin's id WITHOUT importing or executing its code.

        Parses ``plugin.py`` with the AST only: the ``QuickToolManifest(
        id=...)`` keyword when it is a string literal or a module-level
        constant (e.g. ``PLUGIN_ID = "..."``). Never imports the module, so
        an unmanaged plugin's business code cannot run during discovery.
        """
        try:
            source = (plugin_dir / "plugin.py").read_text(
                encoding="utf-8", errors="ignore"
            )
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            return None
        module_constants: dict[str, str] = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                module_constants[node.targets[0].id] = node.value.value
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "QuickToolManifest"
            ):
                for keyword in node.keywords:
                    if keyword.arg != "id":
                        continue
                    if (
                        isinstance(keyword.value, ast.Constant)
                        and isinstance(keyword.value.value, str)
                    ):
                        return keyword.value.value
                    if (
                        isinstance(keyword.value, ast.Name)
                        and keyword.value.id in module_constants
                    ):
                        return module_constants[keyword.value.id]
        return module_constants.get("PLUGIN_ID")

    def discover_roots(self) -> list[Path]:
        primary = os.environ.get(PLUGIN_ROOT_ENV, "").strip()
        candidates = [Path(primary) if primary else DEFAULT_PLUGIN_ROOT]
        raw = os.environ.get(PLUGIN_PATH_ENV, "")
        for part in raw.split(os.pathsep):
            part = part.strip()
            if part:
                candidates.append(Path(part))
        roots: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = os.path.normcase(str(candidate.resolve(strict=False)))
            if key in seen:
                continue
            seen.add(key)
            roots.append(candidate)
        return roots

    def load(self) -> list[QuickToolManifest]:
        """Discover and load plugins; returns the manifests that were registered.

        Discovery and registration are separate: every plugin directory is
        probed for its id via AST (no import), but only plugins in the
        managed catalog are imported, registered, and later started. An
        unmanaged plugin is ignored before its implementation ever executes.
        """
        loaded: list[QuickToolManifest] = []
        guard = _BlockedImportFinder(_BLOCKED_IMPORTS)
        sys.meta_path.insert(0, guard)
        try:
            for root in self.discover_roots():
                if not root.is_dir():
                    continue
                candidates: list[tuple[Path, str | None]] = []
                for plugin_dir in (p for p in root.iterdir() if p.is_dir()):
                    if not (plugin_dir / "__init__.py").is_file():
                        continue
                    if not (plugin_dir / "plugin.py").is_file():
                        continue
                    plugin_id = self._probe_plugin_id(plugin_dir)
                    candidates.append((plugin_dir, plugin_id))
                # Product order is the managed catalog order, independent of
                # directory spelling or filesystem enumeration order.
                catalog_order = {
                    plugin_id: index for index, plugin_id in enumerate(MANAGED_PLUGIN_IDS)
                }
                candidates.sort(
                    key=lambda item: (
                        catalog_order.get(item[1], len(catalog_order)),
                        item[0].name,
                    )
                )
                for plugin_dir, plugin_id in candidates:
                    if plugin_id is None or plugin_id not in MANAGED_PLUGIN_IDS:
                        # Unmanaged: stop before importing any implementation.
                        log.info("ignored unmanaged plugin")
                        continue
                    try:
                        plugin = self._load_plugin(plugin_dir)
                    except Exception as exc:  # isolate a malformed plugin
                        log.error(
                            "plugin load failed dir=%s exception_type=%s",
                            plugin_dir.name,
                            type(exc).__name__,
                        )
                        continue
                    try:
                        self._registry.register(
                            plugin.manifest,
                            open_handler=plugin.open,
                            status_provider=plugin.status,
                            status_changed=plugin.status_changed,
                            capability=plugin.capability,
                            capability_note=plugin.capability_note,
                        )
                    except Exception as exc:
                        log.error(
                            "plugin registration failed dir=%s exception_type=%s",
                            plugin_dir.name,
                            type(exc).__name__,
                        )
                        try:
                            plugin.shutdown()
                        except Exception:
                            pass
                        continue
                    self._plugins.append(plugin)
                    self._plugins_by_id[plugin.manifest.id] = plugin
                    loaded.append(plugin.manifest)
        finally:
            try:
                sys.meta_path.remove(guard)
            except ValueError:
                pass
        return loaded

    # -- Extension API v2 lifecycle (two-phase injection + start/stop) ------

    def sync_services(
        self,
        *,
        bus: Any = None,
        memory: Any = None,
        settings: Any = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        """Inject runtime services into the shared context (two-phase).

        Called once by app.py after the RuntimeBus and MemoryService exist.
        The context instance handed to every plugin at ``initialize()`` is
        mutated in place, so no plugin needs a second ``initialize`` call
        (re-running it would double-own resources in existing plugins).
        """
        self._context.bus = bus
        self._context.memory = memory
        self._context.settings = settings
        self._context.log = log

    def start_all(self) -> None:
        """Start background work on every enabled extension (optional hook).

        Only plugins defining ``start`` are affected; plain QuickTool plugins
        are untouched. Disabled plugins are not started. Exceptions are
        isolated and never logged with provider text.
        """
        for plugin in self._plugins:
            if not self.is_plugin_enabled(plugin.manifest.id):
                continue
            start = getattr(plugin, "start", None)
            if not callable(start):
                continue
            try:
                start()
            except Exception as exc:
                log.error(
                    "plugin start failed id=%s exception_type=%s message=handler raised",
                    plugin.manifest.id,
                    type(exc).__name__,
                )

    def stop_all(self) -> None:
        """Stop background work on every loaded extension, reverse order.

        Must run before ``runtime_bus.close()`` so plugin events are still
        deliverable during teardown.
        """
        for plugin in reversed(self._plugins):
            stop = getattr(plugin, "stop", None)
            if not callable(stop):
                continue
            try:
                stop()
            except Exception as exc:
                log.error(
                    "plugin stop failed id=%s exception_type=%s message=handler raised",
                    plugin.manifest.id,
                    type(exc).__name__,
                )

    def _load_plugin(self, plugin_dir: Path) -> QuickToolPlugin:
        root = str(plugin_dir.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        name = plugin_dir.name
        try:
            module = importlib.import_module(f"{name}.plugin")
        except Exception:
            # A failed submodule import leaves the parent package cached in
            # sys.modules with a __path__ pinned to this directory; a later
            # scan (or another plugin root) using the same directory name
            # would silently re-import from here. Drop it so each discovery
            # is self-contained.
            self._forget_plugin_modules(name)
            raise
        factory = getattr(module, "create_plugin", None)
        if not callable(factory):
            self._forget_plugin_modules(name)
            raise ValueError(f"{name}/plugin.py must define create_plugin(parent=None)")
        plugin = factory(parent=self)
        if not isinstance(plugin, QuickToolPlugin):
            self._forget_plugin_modules(name)
            raise TypeError(f"{name} create_plugin returned a non-QuickToolPlugin")
        plugin.initialize(self._context)
        # Discovery is done: drop the package (and all its submodules) from
        # sys.modules. The loaded plugin keeps its module graph alive through
        # instance references, so this is safe at runtime and prevents a
        # same-named plugin in another root from reusing a stale package.
        self._forget_plugin_modules(name)
        return plugin

    @staticmethod
    def _forget_plugin_modules(name: str) -> None:
        """Drop a plugin package and all its submodules from sys.modules."""
        prefix = f"{name}."
        for key in [k for k in sys.modules if k == name or k.startswith(prefix)]:
            sys.modules.pop(key, None)

    def shutdown(self) -> None:
        for plugin in reversed(self._plugins):
            try:
                plugin.shutdown()
            except Exception as exc:
                log.error("plugin shutdown failed id=%s error=%s", plugin.manifest.id, exc)
        self._plugins.clear()
