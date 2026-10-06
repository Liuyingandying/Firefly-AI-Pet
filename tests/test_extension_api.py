"""Firefly Extension API v2 (Phase 1) — long-lived capability plugin tests.

Covers: extension discovery + manifest metadata, background lifecycle
(start/stop ordering, optional hooks), two-phase service injection
(bus/memory/settings via ``sync_services``), ``plugin.status`` bus publishing,
the narrow ``ExtensionMemory`` facade, the app-import guard, and backward
compatibility of the v1 QuickTool contract.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from core.extension_api import (
    EXT_STATUS_READY,
    ExtensionMemory,
    FireflyExtension,
    PluginStatusEvent,
    publish_plugin_status,
)
from core.plugin_loader import PluginLoader
from core.quick_tools import QuickToolManifest, QuickToolsRegistry
from core.runtime_bus import RuntimeBus

DUMMY_EXTENSION = '''\
"""Dummy long-lived extension used by the public-core tests."""

from core.extension_api import FireflyExtension
from core.quick_tools import QuickToolManifest


class DummyExtension(FireflyExtension):
    capabilities = ("camera", "background")

    @property
    def manifest(self):
        return QuickToolManifest(
            id="dummy-ext",
            name="Dummy Extension",
            description="A dummy capability plugin",
            icon="camera",
            version="2.0.0",
            capabilities=self.capabilities,
            min_api="2",
            config_defaults={"fps": 30},
        )

    def initialize(self, context):
        super().initialize(context)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True
        self.bus_at_start = self.context.bus

    def stop(self):
        self.stopped = True


def create_plugin(parent=None):
    return DummyExtension(parent)
'''

V1_PLUGIN = '''\
"""Minimal v1 QuickTool plugin (no start/stop, no capabilities)."""

from core.plugin_api import QuickToolPlugin
from core.quick_tools import QuickToolManifest


class V1Plugin(QuickToolPlugin):
    @property
    def manifest(self):
        return QuickToolManifest(
            id="v1", name="V1", description="v1 tool", icon="star", version="1.0.0"
        )

    def initialize(self, context):
        self._context = context
        self._opened = False
        self._shutdown = False

    def open(self):
        self._opened = True

    def shutdown(self):
        self._shutdown = True

    def status(self):
        return "READY"


def create_plugin(parent=None):
    return V1Plugin(parent)
'''

BLOCKED_APP_PLUGIN = 'import app  # must be rejected by the import guard\n'

GOOD_PLUGIN = DUMMY_EXTENSION.replace('"dummy-ext"', '"good-ext"').replace(
    "Dummy Extension", "Good Extension"
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_root(tmp_path: Path, name: str, source: str) -> Path:
    plugin_dir = tmp_path / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")
    return tmp_path / "plugins"


def _isolated_loader(tmp_path, monkeypatch, env_root: Path | None = None):
    monkeypatch.setattr("core.plugin_loader.DEFAULT_PLUGIN_ROOT", tmp_path / "no_such_root")
    # These extension-api tests drive synthetic plugins; join the catalog.
    monkeypatch.setattr(
        "core.plugin_loader.MANAGED_PLUGIN_IDS",
        ("dummy-ext", "good-ext", "v1", "x", "y"),
    )
    if env_root is None:
        monkeypatch.delenv("FIREFLY_PLUGIN_PATH", raising=False)
    else:
        monkeypatch.setenv("FIREFLY_PLUGIN_PATH", str(env_root))
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry)
    return registry, loader


# ---------------------------------------------------------------- discovery

def test_extension_is_discovered_with_manifest_metadata(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "dummy_ext", DUMMY_EXTENSION)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    try:
        manifests = loader.load()
        assert [m.id for m in manifests] == ["dummy-ext"]
        manifest = manifests[0]
        assert manifest.capabilities == ("camera", "background")
        assert manifest.min_api == "2"
        assert manifest.config_defaults == {"fps": 30}
        plugin = loader._plugins[0]
        assert isinstance(plugin, FireflyExtension)
        assert plugin.context is not None
        assert registry.get("dummy-ext") is not None
    finally:
        loader.shutdown()


def test_v1_plugin_without_start_stop_is_unaffected(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "v1", V1_PLUGIN)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    try:
        loader.load()
        loader.start_all()  # must be a no-op for v1 plugins
        loader.stop_all()   # must be a no-op for v1 plugins
        assert registry.open("v1") is True
        plugin = loader._plugins[0]
        assert plugin._opened is True
        loader.shutdown()
        assert plugin._shutdown is True
    finally:
        loader.shutdown()


# ------------------------------------------------------- service injection

def test_sync_services_injects_into_shared_context(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "dummy_ext", DUMMY_EXTENSION)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    try:
        loader.load()
        assert loader._context.bus is None  # phase 1: nothing injected yet
        fake_memory = object()
        fake_settings = object()
        loader.sync_services(bus="THE-BUS", memory=fake_memory, settings=fake_settings)
        ctx = loader._context
        assert ctx.bus == "THE-BUS"
        assert ctx.memory is fake_memory
        assert ctx.settings is fake_settings
        plugin = loader._plugins[0]
        assert plugin.context is ctx  # same instance — no second initialize()
    finally:
        loader.shutdown()


# ------------------------------------------------------- lifecycle order

class _LoggingPlugin:
    def __init__(self, log, name):
        self.log = log
        self.name = name
        self.manifest = type("M", (), {"id": name})()

    def start(self):
        self.log.append(("start", self.name))

    def stop(self):
        self.log.append(("stop", self.name))

    def shutdown(self):
        pass


def test_start_all_starts_in_order_stops_in_reverse(tmp_path, monkeypatch):
    registry, loader = _isolated_loader(tmp_path, monkeypatch)
    log: list[tuple[str, str]] = []
    loader._plugins = [_LoggingPlugin(log, "a"), _LoggingPlugin(log, "b")]
    try:
        loader.start_all()
        loader.stop_all()
        assert log == [("start", "a"), ("start", "b"), ("stop", "b"), ("stop", "a")]
    finally:
        loader.shutdown()


def test_start_failure_is_isolated(tmp_path, monkeypatch, caplog):
    registry, loader = _isolated_loader(tmp_path, monkeypatch)
    calls: list[str] = []

    class Boom:
        manifest = type("M", (), {"id": "boom"})()

        def start(self):
            raise RuntimeError("secret-start-boom")

        def shutdown(self):
            pass

    class Ok:
        manifest = type("M", (), {"id": "ok"})()

        def __init__(self, calls):
            self.calls = calls

        def start(self):
            self.calls.append("ok")

        def shutdown(self):
            pass

    loader._plugins = [Boom(), Ok(calls)]
    try:
        with caplog.at_level("ERROR", logger="firefly.plugin_loader"):
            loader.start_all()
            loader.stop_all()  # Boom has no stop -> skipped
        assert calls == ["ok"]
        assert "exception_type=RuntimeError" in caplog.text
        assert "secret-start-boom" not in caplog.text
    finally:
        loader.shutdown()


# ------------------------------------------------------- bus publishing

def test_publish_status_reaches_runtime_bus(qapp, tmp_path, monkeypatch):
    root = _make_root(tmp_path, "dummy_ext", DUMMY_EXTENSION)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    bus = RuntimeBus()
    events = []
    unsub = bus.subscribe_event(events.append)
    try:
        loader.load()
        loader.sync_services(bus=bus)
        plugin = loader._plugins[0]
        plugin.publish_status(EXT_STATUS_READY, "local")
        qapp.processEvents()
        assert [e.kind for e in events] == ["plugin.status"]
        event = events[0]
        assert event.source == "dummy-ext"
        assert isinstance(event.payload, PluginStatusEvent)
        assert event.payload.status == EXT_STATUS_READY
        assert event.payload.message == "local"
        # publishing without an injected bus must be a safe no-op
        assert publish_plugin_status(None, "x", EXT_STATUS_READY) is None
    finally:
        unsub()
        bus.close()
        loader.shutdown()


# ------------------------------------------------------- memory facade

class _FakeMemory:
    def __init__(self):
        self.seen = []

    def search(self, query, *, limit=5):
        return [f"hit:{query}"] * limit

    def suggest_memory(self, content, metadata):
        self.seen.append((content, metadata["category"], metadata["trigger"], False))
        return {"id": "rec1"}


def test_extension_memory_facade_delegates_read_and_write():
    fake = _FakeMemory()
    facade = ExtensionMemory(fake)
    assert facade.search("hello", limit=2) == ["hit:hello"] * 2
    facade.remember("saw a person")
    assert fake.seen == [("saw a person", "user_fact", "extension", False)]
    facade.remember("saw a dog", category="project", trigger="camera")
    assert fake.seen[-1] == ("saw a dog", "project", "camera", False)


def test_extension_memory_facade_degrades_without_service():
    facade = ExtensionMemory(None)
    assert facade.search("anything") == []
    assert facade.remember("anything") is None
    facade = ExtensionMemory(object())  # service without search/remember
    assert facade.search("anything") == []
    assert facade.remember("anything") is None


# ------------------------------------------------------- manifest metadata

def test_manifest_optional_fields_default_and_roundtrip():
    manifest = QuickToolManifest(
        id="x",
        name="X",
        description="desc",
        icon="star",
        capabilities=("vision",),
        min_api="2",
        config_defaults={"mode": "auto"},
        author="me",
        repository="http://r",
    )
    assert manifest.capabilities == ("vision",)
    assert manifest.min_api == "2"
    assert manifest.config_defaults == {"mode": "auto"}
    assert manifest.author == "me"
    assert manifest.repository == "http://r"

    plain = QuickToolManifest(id="y", name="Y", description="d", icon="star")
    assert plain.capabilities == ()
    assert plain.min_api == "1"
    assert plain.config_defaults == {}


# ------------------------------------------------------- import guard

def test_app_import_is_blocked_and_isolated(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "bad", BLOCKED_APP_PLUGIN)
    _make_root(tmp_path, "good", GOOD_PLUGIN)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    try:
        manifests = loader.load()
        assert [m.id for m in manifests] == ["good-ext"]
        assert registry.get("bad") is None
    finally:
        loader.shutdown()


def test_import_guard_allows_ui_for_legacy_plugin_compat(tmp_path, monkeypatch):
    from core.plugin_loader import _BLOCKED_IMPORTS

    assert _BLOCKED_IMPORTS == ("app",)
    assert "ui" not in _BLOCKED_IMPORTS
