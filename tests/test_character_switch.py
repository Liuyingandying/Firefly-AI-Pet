"""Character switching acceptance tests (v1.1).

Covers: registry scan, persistence (default firefly / fallback / roundtrip),
per-character loader, animation dir resolution, live pet asset switch, tray
character menu, runtime persona passthrough, and app-level resolution.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from character.character_loader import (
    CharacterLoader,
    current_character_file,
    list_characters,
    load_current_character,
    resolve_animations_dir,
    save_current_character,
)

PROJECT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def tmp_persistence(tmp_path, monkeypatch):
    """Redirect selection persistence to a temp file."""
    path = tmp_path / "current_character.yaml"
    monkeypatch.setattr("character.character_loader.current_character_file",
                        lambda: path)
    return path


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def test_registry_scans_firefly_and_raiden():
    metas = {m.character_id: m for m in list_characters()}
    assert set(metas) >= {"firefly", "raiden"}
    assert metas["firefly"].display_name == "流萤"
    assert metas["raiden"].display_name == "雷电将军"


def test_registry_animations_dirs():
    metas = {m.character_id: m for m in list_characters()}
    assert metas["firefly"].animations_dir is None        # 回退 assets/animations
    assert metas["raiden"].animations_dir is not None
    assert metas["raiden"].animations_dir.name == "animations"


def test_resolve_animations_dir_priority():
    raiden = resolve_animations_dir("raiden", PROJECT_DIR)
    assert raiden == PROJECT_DIR / "assets" / "skins" / "raiden" / "animations"
    assert resolve_animations_dir("firefly", PROJECT_DIR) is None  # 回退旧目录


def test_registry_ignores_non_character_dirs(tmp_path):
    (tmp_path / "not_a_char").mkdir()
    assert list_characters(tmp_path) == []


# --------------------------------------------------------------------------
# persistence (default firefly / fallback)
# --------------------------------------------------------------------------

def test_persistence_default_is_firefly(tmp_persistence):
    assert load_current_character() == "firefly"


def test_persistence_roundtrip(tmp_persistence):
    assert save_current_character("raiden") is True
    assert load_current_character() == "raiden"
    assert save_current_character("firefly") is True
    assert load_current_character() == "firefly"


def test_persistence_invalid_handled_at_app_layer(tmp_persistence):
    """Persistence stores what was saved; unknown ids fall back to firefly
    at the app resolution layer (test_app_resolve... covers it)."""
    save_current_character("ghost")
    assert load_current_character() == "ghost"
    assert load_current_character(default="firefly") == "ghost"  # loader 层不回退
    # 未配置 → 默认 firefly
    tmp_persistence.unlink()
    assert load_current_character() == "firefly"


# --------------------------------------------------------------------------
# per-character loader
# --------------------------------------------------------------------------

def test_raiden_loader_display_name():
    profile = CharacterLoader(
        PROJECT_DIR / "character" / "raiden", character_id="raiden"
    ).load()
    assert profile.display_name == "雷电将军"
    assert profile.assistant_name == "雷电将军"
    assert "雷电将军" in profile.identity_prompt


def test_firefly_loader_display_name():
    profile = CharacterLoader(
        PROJECT_DIR / "character" / "firefly", character_id="firefly"
    ).load()
    assert profile.display_name == "流萤"


# --------------------------------------------------------------------------
# live pet asset switch
# --------------------------------------------------------------------------

def _make_gif(path: Path, color=(200, 80, 220)):
    im = Image.new("RGBA", (64, 64), (*color, 255))
    im.save(path, format="GIF", transparency=0)


def test_pet_set_assets_dir_switches_source(qapp, tmp_path):
    from ui.pet_overlay import PetOverlay

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir(); dir_b.mkdir()
    _make_gif(dir_a / "idle.gif", (100, 200, 100))
    _make_gif(dir_b / "idle.gif", (200, 80, 220))

    pet = PetOverlay(dir_a, {"idle": "idle.gif"})
    assert pet._source_path == dir_a / "idle.gif"

    pet.set_assets_dir(dir_b)
    assert pet._source_path == dir_b / "idle.gif"
    pet.shutdown()


def test_pet_set_display_names_retitles(qapp, tmp_path):
    from ui.pet_overlay import PetOverlay
    from character.character_loader import CharacterDisplayNames

    dir_a = tmp_path / "a"; dir_a.mkdir()
    _make_gif(dir_a / "idle.gif")
    pet = PetOverlay(dir_a, {"idle": "idle.gif"})
    pet.set_display_names(CharacterDisplayNames(brand_name="雷电将军 AI Pet"))
    assert "雷电将军" in pet.windowTitle()
    pet.shutdown()


# --------------------------------------------------------------------------
# tray character menu
# --------------------------------------------------------------------------

def test_tray_builds_character_menu(qapp, tmp_path):
    from ui.system_tray import FireflySystemTray

    calls: list[str] = []

    class _Controller:
        def toggle_firefly(self): pass
        def show_settings(self): pass
        def exit_application(self): pass

    tray = FireflySystemTray(controller=_Controller(), icon_path=str(tmp_path / "x.ico"))
    tray.set_characters(list_characters(), "firefly", on_switch=lambda cid: calls.append(cid))

    texts = [a.text() for a in tray.menu.actions()]
    assert any("切换角色" in t for t in texts), texts
    character_menu = next(a.menu() for a in tray.menu.actions()
                          if a.menu() is not None and "切换角色" in a.text())
    items = [a for a in character_menu.actions()]
    assert len(items) >= 2
    checked = [a for a in items if a.isChecked()]
    assert len(checked) == 1 and "firefly" in checked[0].text()
    items[1].trigger()
    assert calls == ["raiden"] or len(calls) >= 1


# --------------------------------------------------------------------------
# runtime persona passthrough
# --------------------------------------------------------------------------

def test_conversation_runtime_update_character_passthrough():
    from core.conversation_runtime import ConversationRuntime

    class _StubRuntime:
        def __init__(self):
            self.updated = None
        def update_character(self, profile):
            self.updated = profile

    stub = _StubRuntime()
    runtime = ConversationRuntime(companion_runtime=stub)
    fake = object()
    runtime.update_character(fake)
    assert stub.updated is fake


# --------------------------------------------------------------------------
# app-level active-character resolution
# --------------------------------------------------------------------------

def test_app_resolve_active_character_default_firefly(monkeypatch, tmp_path):
    import app as app_module

    path = tmp_path / "current_character.yaml"
    monkeypatch.setattr(app_module, "load_current_character",
                        lambda: "ghost")
    monkeypatch.setattr(app_module, "list_characters",
                        lambda: list_characters())
    assert app_module._resolve_active_character() == "firefly"

    monkeypatch.setattr(app_module, "load_current_character",
                        lambda: "raiden")
    assert app_module._resolve_active_character() == "raiden"
