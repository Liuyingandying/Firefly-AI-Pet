"""Quick Tools Plugin Management P0.1 — managed-plugin discovery allowlist.

Discovery and registration are separated: the loader probes plugin ids via
AST (never importing) and registers ONLY the managed catalog plugins. An
unmanaged plugin is never imported, registered, started, shown in Quick
Tools, or persisted. The legacy "exclude a retired plugin" environment
blacklist is fully removed — nothing in the loader reads it.

Covers:
  1. managed catalog is exactly the three supported plugins
  2-3. the three register and appear in Quick Tools
  4-5. managed + disabled → not started; managed + enabled → started
  6-10. unmanaged-test-plugin: not registered / not imported / not started /
        not in Quick Tools / no enabled preference
 11-12. the legacy blacklist env absent vs present → identical results
 13. FIREFLY_PLUGIN_PATH cannot bypass the managed catalog
 14. P0 On/Off persistence regression
 15. availability and enabled stay independent
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from core.plugin_loader import PluginLoader, MANAGED_PLUGIN_IDS
from core.quick_tools import QuickToolsRegistry
from core.settings_manager import SettingsManager
from ui.quick_tools_popover import QuickToolsPopover

UNMANAGED_ID = "unmanaged-test-plugin"
LEGACY_BLACKLIST_ENV = "FIREFLY_RETIRED_PLUGINS"


def _plugin_source(plugin_id: str, *, explode_on_import: bool = False) -> str:
    """A plugin whose start/stop count calls; optionally writes a marker on
    import so a test can prove the module was never imported."""
    marker = ""
    if explode_on_import:
        marker = (
            "import os\n"
            f"open({os.environ.get('P0_TEST_MARKER', '')!r}, 'w', encoding='utf-8').write('imported')\n"
        )
    return f'''"""Managed plugin fixture."""
from core.plugin_api import QuickToolPlugin
from core.quick_tools import QuickToolManifest

{marker}
PLUGIN_ID = "{plugin_id}"


class FixturePlugin(QuickToolPlugin):
    @property
    def manifest(self):
        return QuickToolManifest(id=PLUGIN_ID, name="{plugin_id}", description="d", icon="star")

    def initialize(self, context):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def status(self):
        return "READY"


def create_plugin(parent=None):
    return FixturePlugin(parent)
'''


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _make_plugin_dir(root: Path, name: str, source: str) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")
    return plugin_dir


def _full_root(tmp_path: Path, marker_path: Path) -> Path:
    """A root with the three managed plugins + one unmanaged test plugin."""
    os.environ["P0_TEST_MARKER"] = str(marker_path)
    root = tmp_path / "plugins"
    for plugin_id in MANAGED_PLUGIN_IDS:
        _make_plugin_dir(root, plugin_id, _plugin_source(plugin_id))
    _make_plugin_dir(
        root, "unmanaged_test", _plugin_source(UNMANAGED_ID, explode_on_import=True)
    )
    return root


def _loader(tmp_path, monkeypatch, *, root: Path | None, env: bool) -> tuple:
    monkeypatch.setattr("core.plugin_loader.DEFAULT_PLUGIN_ROOT", tmp_path / "no_root")
    if root is not None:
        monkeypatch.setenv("FIREFLY_PLUGIN_PATH", str(root))
    else:
        monkeypatch.delenv("FIREFLY_PLUGIN_PATH", raising=False)
    if env:
        monkeypatch.setenv(LEGACY_BLACKLIST_ENV, "does-not-matter")
    else:
        monkeypatch.delenv(LEGACY_BLACKLIST_ENV, raising=False)
    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry, persistence=settings)
    loader.load()
    return registry, loader, settings


# ---------------------------------------------------------------------------
# 1-3. catalog, registration, Quick Tools presence
# ---------------------------------------------------------------------------


def test_catalog_is_exactly_the_supported_set() -> None:
    # TJU Info Retrieval joined the managed catalog (open_ui 三入口 contract).
    assert tuple(MANAGED_PLUGIN_IDS) == (
        "firefly-video",
        "firefly-camera-vision",
        "learning-focus",
        "tju-info-retrieval",
        "firefly-voice",
    )


def test_three_managed_plugins_register(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    ids = [r.manifest.id for r in registry.all()]
    assert sorted(ids) == sorted(MANAGED_PLUGIN_IDS)


def test_three_managed_plugins_appear_in_quick_tools(qapp, tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    pop = QuickToolsPopover(registry)
    assert set(pop._cards) == set(MANAGED_PLUGIN_IDS)
    pop.close()


# ---------------------------------------------------------------------------
# 4-5. enabled gates start
# ---------------------------------------------------------------------------


def test_managed_disabled_not_started(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    settings.set_plugin_enabled("firefly-video", False)
    loader.start_all()
    plugin = loader._plugins_by_id["firefly-video"]
    assert plugin.started == 0
    assert loader.is_plugin_enabled("firefly-video") is False


def test_managed_enabled_started(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    loader.start_all()
    for plugin_id in MANAGED_PLUGIN_IDS:
        assert loader._plugins_by_id[plugin_id].started >= 1


# ---------------------------------------------------------------------------
# 6-10. unmanaged-test-plugin is inert
# ---------------------------------------------------------------------------


def test_unmanaged_not_registered(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    assert registry.get(UNMANAGED_ID) is None
    assert UNMANAGED_ID not in loader._plugins_by_id


def test_unmanaged_implementation_never_imported(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "imported.marker"
    root = _full_root(tmp_path, marker)
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    assert not marker.exists(), "unmanaged plugin implementation was imported"


def test_unmanaged_never_started(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    loader.start_all()
    assert UNMANAGED_ID not in loader._plugins_by_id


def test_unmanaged_not_in_quick_tools(qapp, tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    pop = QuickToolsPopover(registry)
    assert UNMANAGED_ID not in pop._cards
    assert pop.card_status(UNMANAGED_ID) == ""
    pop.close()


def test_unmanaged_creates_no_enabled_preference(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    # Toggle a MANAGED plugin so the preferences file exists with only
    # managed keys; the unmanaged plugin must never gain one.
    loader.set_plugin_enabled("firefly-video", True)
    prefs = settings.preferences_file.read_text(encoding="utf-8")
    assert f"plugins.{UNMANAGED_ID}.enabled" not in prefs
    assert f"plugins.firefly-video.enabled" in prefs
    assert settings.plugin_enabled(UNMANAGED_ID) is True  # no stored preference


# ---------------------------------------------------------------------------
# 11-12. legacy blacklist env is fully inert
# ---------------------------------------------------------------------------


def test_absent_legacy_env_registers_only_managed(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    assert sorted(r.manifest.id for r in registry.all()) == sorted(MANAGED_PLUGIN_IDS)


def test_present_legacy_env_identical_results(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=True
    )
    assert sorted(r.manifest.id for r in registry.all()) == sorted(MANAGED_PLUGIN_IDS)


# ---------------------------------------------------------------------------
# 13. FIREFLY_PLUGIN_PATH cannot bypass the catalog
# ---------------------------------------------------------------------------


def test_plugin_path_cannot_bypass_catalog(tmp_path, monkeypatch) -> None:
    # A SEPARATE root containing ONLY the unmanaged plugin, added explicitly
    # via FIREFLY_PLUGIN_PATH — still not registered.
    extra_root = tmp_path / "extra"
    _make_plugin_dir(
        extra_root, "unmanaged_test", _plugin_source(UNMANAGED_ID)
    )
    monkeypatch.setattr("core.plugin_loader.DEFAULT_PLUGIN_ROOT", tmp_path / "no_root")
    monkeypatch.setenv("FIREFLY_PLUGIN_PATH", str(extra_root))
    registry = QuickToolsRegistry()
    loader = PluginLoader(registry)
    loader.load()
    assert registry.get(UNMANAGED_ID) is None
    assert registry.all() == ()


# ---------------------------------------------------------------------------
# 14. P0 On/Off persistence regression
# ---------------------------------------------------------------------------


def test_on_off_persistence_regression(tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    settings = SettingsManager(preferences_file=tmp_path / "prefs.json")
    settings.set_plugin_enabled("learning-focus", False)
    registry, loader, _ = _loader(tmp_path, monkeypatch, root=root, env=False)
    assert loader.is_plugin_enabled("learning-focus") is False
    loader.start_all()
    assert loader._plugins_by_id["learning-focus"].started == 0
    loader.set_plugin_enabled("learning-focus", True)  # hot ON
    assert loader.is_plugin_enabled("learning-focus") is True
    assert loader._plugins_by_id["learning-focus"].started >= 1


# ---------------------------------------------------------------------------
# 15. availability and enabled independent
# ---------------------------------------------------------------------------


def test_availability_independent_of_enabled(qapp, tmp_path, monkeypatch) -> None:
    root = _full_root(tmp_path, tmp_path / "m")
    registry, loader, _settings = _loader(
        tmp_path, monkeypatch, root=root, env=False
    )
    pop = QuickToolsPopover(
        registry,
        plugin_is_enabled=loader.is_plugin_enabled,
        plugin_set_enabled=loader.set_plugin_enabled,
    )
    card = pop._cards["firefly-video"]
    card.set_status("READY")
    card.set_enabled(False)
    assert card.status_text == "READY"
    assert card.is_enabled is False
    pop.close()
