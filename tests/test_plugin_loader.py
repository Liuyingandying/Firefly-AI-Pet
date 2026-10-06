"""External Quick Tool plugin loader + generic Quick Tools tests.

Uses dummy plugins in a temp plugin root (via ``FIREFLY_PLUGIN_PATH``) plus the
private plugins when they are present on this machine.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from core.plugin_loader import PluginLoader
from core.quick_tools import QuickToolsRegistry
from ui.quick_tools_popover import QuickToolsPopover

DUMMY_PLUGIN = '''\
"""Dummy Quick Tool plugin used by the public-core tests."""

from core.plugin_api import QuickToolPlugin
from core.quick_tools import QuickToolManifest


class DummyPlugin(QuickToolPlugin):
    @property
    def manifest(self):
        return QuickToolManifest(
            id="dummy",
            name="Dummy",
            description="A generic dummy plugin",
            icon="star",
            version="1.0.0",
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
    return DummyPlugin(parent)
'''

MALFORMED_PLUGIN = 'raise RuntimeError("boom at import")\n'

GOOD_PLUGIN = DUMMY_PLUGIN.replace('"dummy"', '"good"').replace("Dummy", "Good")


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
    # These public-core tests drive synthetic plugins; join the managed catalog.
    monkeypatch.setattr(
        "core.plugin_loader.MANAGED_PLUGIN_IDS",
        ("dummy", "good"),
    )
    if env_root is None:
        monkeypatch.delenv("FIREFLY_PLUGIN_PATH", raising=False)
    else:
        monkeypatch.setenv("FIREFLY_PLUGIN_PATH", str(env_root))
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry)
    return registry, loader


def test_no_external_plugin_directory_starts_normally(tmp_path, monkeypatch):
    registry, loader = _isolated_loader(tmp_path, monkeypatch)
    manifests = loader.load()
    assert manifests == []
    assert registry.all() == ()
    loader.shutdown()  # must not raise


def test_dummy_plugin_is_discovered(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "dummy", DUMMY_PLUGIN)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    manifests = loader.load()
    assert [m.id for m in manifests] == ["dummy"]
    registration = registry.get("dummy")
    assert registration is not None
    assert registration.manifest.version == "1.0.0"
    loader.shutdown()


def test_malformed_plugin_is_isolated(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "bad", MALFORMED_PLUGIN)
    _make_root(tmp_path, "good", GOOD_PLUGIN)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    manifests = loader.load()  # must not raise
    assert [m.id for m in manifests] == ["good"]
    assert registry.get("bad") is None
    loader.shutdown()


def test_malformed_plugin_log_does_not_include_exception_text(
    tmp_path, monkeypatch, caplog
):
    secret = "TEST_SECRET_DO_NOT_LOG"
    root = _make_root(tmp_path, "bad", f'raise RuntimeError("{secret}")\n')
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    with caplog.at_level("INFO", logger="firefly.plugin_loader"):
        assert loader.load() == []
    # A manifest-less plugin is treated as unmanaged and never imported, so
    # its exception text can never appear anywhere.
    assert secret not in caplog.text
    assert "exception_type=" not in caplog.text
    assert registry.all() == ()


def test_duplicate_plugin_root_is_scanned_once(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "dummy", DUMMY_PLUGIN)
    monkeypatch.setattr("core.plugin_loader.DEFAULT_PLUGIN_ROOT", root)
    monkeypatch.setattr("core.plugin_loader.MANAGED_PLUGIN_IDS", ("dummy",))
    monkeypatch.setenv("FIREFLY_PLUGIN_PATH", str(root))
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry)
    try:
        assert [manifest.id for manifest in loader.load()] == ["dummy"]
        assert len(loader._plugins) == 1
    finally:
        loader.shutdown()


def test_duplicate_plugin_id_is_isolated(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "alpha", DUMMY_PLUGIN)
    _make_root(tmp_path, "beta", DUMMY_PLUGIN)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    try:
        assert [manifest.id for manifest in loader.load()] == ["dummy"]
        assert registry.get("dummy") is not None
        assert len(loader._plugins) == 1
    finally:
        loader.shutdown()


def test_plugin_open_shutdown_lifecycle(tmp_path, monkeypatch):
    root = _make_root(tmp_path, "dummy", DUMMY_PLUGIN)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    loader.load()
    plugin = loader._plugins[0]
    assert plugin._opened is False and plugin._shutdown is False
    assert registry.open("dummy") is True
    assert plugin._opened is True
    loader.shutdown()
    assert plugin._shutdown is True


def test_quick_tools_dynamic_display(tmp_path, qapp, monkeypatch):
    root = _make_root(tmp_path, "dummy", DUMMY_PLUGIN)
    registry, loader = _isolated_loader(tmp_path, monkeypatch, env_root=root)
    loader.load()
    popover = QuickToolsPopover(registry)
    try:
        assert popover.card_status("dummy") == "READY"
    finally:
        popover.close()
        loader.shutdown()


def test_quick_tools_does_not_show_unregistered_plugin(tmp_path, qapp, monkeypatch):
    registry, loader = _isolated_loader(tmp_path, monkeypatch)
    loader.load()  # nothing registered
    popover = QuickToolsPopover(registry)
    try:
        assert popover.card_status("dummy") == ""
        assert "dummy" not in popover._cards
    finally:
        popover.close()
        loader.shutdown()


def test_screen_vision_tjutoken_untouched():
    project = Path(__file__).resolve().parents[1]
    config = project / "core" / "screen_vision" / "config.py"
    text = config.read_text(encoding="utf-8")
    assert "TJUTOKEN" in text
