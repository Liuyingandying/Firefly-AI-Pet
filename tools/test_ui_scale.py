"""UI Scale Mode (right-click + wheel) — offline tests.

No online calls, no real browser, no disk writes to the real config. Covers the
scale math, persistence, mode toggling, shell sizing and anchor preservation,
plus the real character render-size scaling and the responsive compact dock.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.settings_manager import SettingsManager
from ui import theme


def _reset_scale() -> None:
    theme.set_ui_scale(theme.DEFAULT_SCALE)


def _settle(app) -> None:
    app.processEvents()


def _fresh_dock():
    from ui.agent_dock import AgentDock

    return AgentDock()


def _fresh_toolbar():
    from ui.vertical_toolbar import VerticalToolbar

    return VerticalToolbar()


def _fresh_pet():
    from ui.pet_overlay import PetOverlay

    state_gif = {
        "idle": "idle.gif",
        "working": "running.gif",
        "sleeping": "idle.gif",
    }
    return PetOverlay(PROJECT_DIR / "assets" / "animations", state_gif)


# -- theme scale math (A/B/C/D/I) ---------------------------------------

def test_default_scale() -> None:
    _reset_scale()
    assert theme.ui_scale() == 1.0


def test_wheel_up_step() -> None:
    _reset_scale()
    theme.set_ui_scale(theme.ui_scale() + theme.SCALE_STEP)
    assert theme.ui_scale() == 1.05


def test_wheel_down_step() -> None:
    _reset_scale()
    theme.set_ui_scale(theme.ui_scale() - theme.SCALE_STEP)
    assert theme.ui_scale() == 0.95


def test_clamp_bounds() -> None:
    _reset_scale()
    theme.set_ui_scale(0.55)
    assert theme.ui_scale() == 0.60
    theme.set_ui_scale(1.45)
    assert theme.ui_scale() == 1.40
    _reset_scale()


def test_nan_and_type_fallback() -> None:
    _reset_scale()
    theme.set_ui_scale(float("nan"))
    assert theme.ui_scale() == 1.0
    theme.set_ui_scale("abc")
    assert theme.ui_scale() == 1.0
    theme.set_ui_scale(True)
    assert theme.ui_scale() == 1.0


# -- persistence (H/I) ---------------------------------------------------

def test_persistence_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "pet_preferences.json"
        SettingsManager(f).set_ui_scale(0.75)
        assert SettingsManager(f).ui_scale == 0.75


def test_malformed_preference_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "pet_preferences.json"
        f.write_text(json.dumps({"ui_scale": "abc"}), encoding="utf-8")
        assert SettingsManager(f).ui_scale == 1.0
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "pet_preferences.json"
        f.write_text(json.dumps({"ui_scale": None}), encoding="utf-8")
        assert SettingsManager(f).ui_scale == 1.0


# -- shell: mode + wheel (E/F/G) -----------------------------------------

def test_no_scale_mode_wheel_noop(shell) -> None:
    _reset_scale()
    shell._scale_mode = False
    shell._on_scale_wheel(1)
    assert theme.ui_scale() == 1.0


def test_scale_mode_wheel_changes(shell) -> None:
    _reset_scale()
    shell._scale_mode = True
    shell._on_scale_wheel(1)
    assert theme.ui_scale() == 1.05
    shell._scale_mode = False


def test_right_click_toggles_mode(shell) -> None:
    shell._scale_mode = False
    shell._on_scale_mode_toggled()
    assert shell._scale_mode is True
    shell._on_scale_mode_toggled()
    assert shell._scale_mode is False


def test_esc_exits_mode(shell) -> None:
    shell._scale_mode = True
    shell._on_scale_exit()
    assert shell._scale_mode is False


# -- shell: sizing + anchoring (J/K/L/M) ---------------------------------

def test_pet_size_scales(shell) -> None:
    _reset_scale()
    shell.pet.apply_scale()
    w1 = shell.pet.width()
    theme.set_ui_scale(0.8)
    w2 = shell.pet.width()
    assert w2 < w1
    assert abs(w2 - w1 * 0.8) <= 2
    _reset_scale()


def test_dock_toolbar_bubble_scale(shell) -> None:
    _reset_scale()
    dock1 = shell.dock.width()
    bar1 = shell.toolbar.width()
    bubble1 = shell.bubble.width()
    theme.set_ui_scale(0.8)
    assert shell.dock.width() < dock1
    assert shell.toolbar.width() < bar1
    assert shell.bubble.width() < bubble1
    _reset_scale()


def test_dock_centered_under_pet(shell) -> None:
    _reset_scale()
    shell.coordinator.reset_position()
    pet = shell.pet.frameGeometry()
    dock = shell.dock.frameGeometry()
    assert abs(dock.center().x() - pet.center().x()) <= 2


def test_toolbar_right_of_pet(shell) -> None:
    _reset_scale()
    shell.coordinator.reset_position()
    pet = shell.pet.frameGeometry()
    toolbar = shell.toolbar.frameGeometry()
    assert abs(toolbar.center().y() - pet.center().y()) <= 2
    assert toolbar.left() >= pet.right() - 1


def test_bubble_upper_left_of_pet(shell) -> None:
    _reset_scale()
    shell.coordinator.reset_position()
    pet = shell.pet.frameGeometry()
    bubble = shell.bubble.frameGeometry()
    assert bubble.right() >= pet.left()
    assert bubble.bottom() >= pet.top()


def test_anchor_preserved_on_scale(shell) -> None:
    _reset_scale()
    shell.coordinator.reset_position()
    geo = shell.pet.frameGeometry()
    before_x = geo.center().x()
    before_bottom = geo.bottom()
    theme.set_ui_scale(0.8)  # triggers coordinator.apply_ui_scale via listener
    geo = shell.pet.frameGeometry()
    assert abs(before_x - geo.center().x()) <= 2
    assert abs(before_bottom - geo.bottom()) <= 2
    _reset_scale()


# -- pet: real render size (A/B/C) ---------------------------------------

def test_pet_render_size_scales(app: QApplication) -> None:
    """A: the rendered character (movie frame) truly shrinks with ui_scale."""
    pet = _fresh_pet()
    pet.show()
    try:
        _reset_scale()
        pet.apply_scale()
        _settle(app)
        s1 = pet._movie.currentPixmap().size()

        theme.set_ui_scale(0.8)
        pet.apply_scale()
        _settle(app)
        s08 = pet._movie.currentPixmap().size()

        theme.set_ui_scale(0.6)
        pet.apply_scale()
        _settle(app)
        s06 = pet._movie.currentPixmap().size()

        assert s1.width() > s08.width() > s06.width()
        assert s1.height() > s08.height() > s06.height()
        assert abs(s08.width() - round(s1.width() * 0.8)) <= 2
        assert abs(s08.height() - round(s1.height() * 0.8)) <= 2
        assert abs(s06.width() - round(s1.width() * 0.6)) <= 2
        assert abs(s06.height() - round(s1.height() * 0.6)) <= 2
    finally:
        pet.shutdown()
        _reset_scale()


def test_pet_frame_within_viewport(app: QApplication) -> None:
    """B: the rendered frame is fully inside the PetOverlay viewport (no crop)."""
    pet = _fresh_pet()
    pet.show()
    try:
        for scale in (1.0, 0.8, 0.6):
            theme.set_ui_scale(scale)
            pet.apply_scale()
            _settle(app)
            pix = pet._movie.currentPixmap().size()
            label = pet._label.size()
            # the label (viewport for the frame) matches the rendered frame
            assert label.width() == pix.width()
            assert label.height() == pix.height()
            # and the label sits fully inside the pet window
            assert pet.rect().contains(pet._label.geometry())
    finally:
        pet.shutdown()
        _reset_scale()


def test_pet_render_roundtrip(app: QApplication) -> None:
    """C: 1.0 -> .8 -> .6 -> 1.0 restores the exact rendered size."""
    pet = _fresh_pet()
    pet.show()
    try:
        _reset_scale()
        pet.apply_scale()
        _settle(app)
        s1 = pet._movie.currentPixmap().size()

        for scale in (0.8, 0.6):
            theme.set_ui_scale(scale)
            pet.apply_scale()
            _settle(app)

        theme.set_ui_scale(1.0)
        pet.apply_scale()
        _settle(app)
        assert pet._movie.currentPixmap().size() == s1
    finally:
        pet.shutdown()
        _reset_scale()


# -- dock: responsive compact mode (D/E/F/G/I/J/K/L) ---------------------

def test_dock_full_at_090_compact_at_085(app: QApplication) -> None:
    """D/E: 0.90 is full, 0.85 is compact."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.90)
        dock.apply_scale()
        _settle(app)
        assert dock._compact is False

        theme.set_ui_scale(0.85)
        dock.apply_scale()
        _settle(app)
        assert dock._compact is True
    finally:
        dock.close()
        _reset_scale()


def test_compact_labels_hidden_icons_visible(app: QApplication) -> None:
    """F/G: compact hides labels, keeps icons visible."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.85)
        dock.apply_scale()
        _settle(app)
        for item in dock._items.values():
            assert not item._name_label.isVisible(), item.agent_id
            assert item._icon.isVisible(), item.agent_id
    finally:
        dock.close()
        _reset_scale()


def test_compact_buttons_clickable(app: QApplication) -> None:
    """H: all three compact buttons still emit activated on click."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.85)
        dock.apply_scale()
        _settle(app)
        clicked: list[str] = []
        for item in dock._items.values():
            item.activated.connect(lambda aid, _acc=clicked: _acc.append(aid))
        for item in dock._items.values():
            QTest.mouseClick(item, Qt.LeftButton, pos=item.rect().center())
            _settle(app)
        assert sorted(clicked) == ["chatgpt", "claude", "codex"]
    finally:
        dock.close()
        _reset_scale()


def test_compact_tooltips(app: QApplication) -> None:
    """I: compact tooltips are exactly the agent names."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.85)
        dock.apply_scale()
        _settle(app)
        assert dock._items["claude"].toolTip() == "Claude"
        assert dock._items["codex"].toolTip() == "Codex"
        assert dock._items["chatgpt"].toolTip() == "ChatGPT"
    finally:
        dock.close()
        _reset_scale()


def test_compact_status_dot_visible(app: QApplication) -> None:
    """J: the status dot remains visible in compact mode."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.85)
        dock.apply_scale()
        _settle(app)
        for item in dock._items.values():
            assert item._dot.isVisible(), item.agent_id
            assert item._dot.width() > 0 and item._dot.height() > 0
    finally:
        dock.close()
        _reset_scale()


def test_compact_restores_labels_at_090(app: QApplication) -> None:
    """K: 0.85 -> 0.90 restores the labels."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.85)
        dock.apply_scale()
        _settle(app)
        assert not dock._items["claude"]._name_label.isVisible()

        theme.set_ui_scale(0.90)
        dock.apply_scale()
        _settle(app)
        for item in dock._items.values():
            assert item._name_label.isVisible(), item.agent_id
    finally:
        dock.close()
        _reset_scale()


def test_compact_dock_narrower_than_full(app: QApplication) -> None:
    """L: compact dock base size is clearly narrower than the full dock."""
    assert theme.COMPACT_DOCK_SIZE.width() < theme.DOCK_SIZE.width() // 2

    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.90)
        dock.apply_scale()
        _settle(app)
        full_width = dock.width()

        theme.set_ui_scale(0.85)
        dock.apply_scale()
        _settle(app)
        compact_width = dock.width()

        assert compact_width < full_width
    finally:
        dock.close()
        _reset_scale()


# -- toolbar + popovers (M/N) --------------------------------------------

def test_toolbar_icon_follows_scale(app: QApplication) -> None:
    """M: toolbar icon shell truly follows the ui_scale."""
    tb = _fresh_toolbar()
    tb.show()
    try:
        theme.set_ui_scale(1.0)
        tb.apply_scale()
        _settle(app)
        i1 = tb._items["companion"]._icon.width()

        theme.set_ui_scale(0.8)
        tb.apply_scale()
        _settle(app)
        i08 = tb._items["companion"]._icon.width()

        assert i08 < i1
        assert abs(i08 - round(i1 * 0.8)) <= 1
    finally:
        tb.close()
        _reset_scale()


def test_popovers_keep_fixed_readable_size(app: QApplication) -> None:
    """N: Settings/Workspace/ShortAsk popovers stay fixed-size (no scale)."""
    from ui.settings_popover import SettingsPopover

    with tempfile.TemporaryDirectory() as td:
        theme.set_ui_scale(1.0)
        popover = SettingsPopover(SettingsManager(Path(td) / "prefs.json"))
        popover.show()
        _settle(app)
        w1 = popover.width()
        popover.close()

        theme.set_ui_scale(0.6)
        popover = SettingsPopover(SettingsManager(Path(td) / "prefs.json"))
        popover.show()
        _settle(app)
        w2 = popover.width()
        popover.close()

        assert w1 == w2 == theme.POPOVER_WIDTH
        _reset_scale()


# -- internal geometry regression (uniform visual scaling) ---------------

def test_labels_visible_full_hidden_compact(app: QApplication) -> None:
    """Labels visible at full scales, hidden (icon-only) at compact scales."""
    dock = _fresh_dock()
    dock.show()
    try:
        for scale in (1.0, 0.95, 0.90):
            theme.set_ui_scale(scale)
            dock.apply_scale()
            _settle(app)
            for item in dock._items.values():
                assert item._name_label.isVisible(), f"{item.agent_id} label hidden at {scale}"
        for scale in (0.85, 0.80, 0.60):
            theme.set_ui_scale(scale)
            dock.apply_scale()
            _settle(app)
            for item in dock._items.values():
                assert not item._name_label.isVisible(), f"{item.agent_id} label shown at {scale}"
    finally:
        dock.close()
        _reset_scale()


def test_icon_dot_separator_proportional(app: QApplication) -> None:
    """D/E/F: icon, status dot and separator geometry track the scale exactly."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.8)
        dock.apply_scale()
        _settle(app)
        claude = dock._items["claude"]
        assert claude._icon.width() == theme.scaled_px(27)
        assert claude._dot.width() == theme.scaled_px(10)
        assert dock._separators[0].height() == theme.scaled_px(27)
        assert dock._separators[0].width() == theme.scaled_px(1)
    finally:
        dock.close()
        _reset_scale()


def test_children_within_item(app: QApplication) -> None:
    """G: icon and dot stay inside their AgentItem rect."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(0.8)
        dock.apply_scale()
        _settle(app)
        for item in dock._items.values():
            # allow a 2px rounding tolerance for icon/dot edges at non-integer scales
            bounds = item.rect().adjusted(-2, -2, 2, 2)
            assert bounds.contains(item._icon.geometry())
            assert bounds.contains(item._dot.geometry())
    finally:
        dock.close()
        _reset_scale()


def test_items_no_overlap(app: QApplication) -> None:
    """H: consecutive AgentItems do not overlap horizontally."""
    dock = _fresh_dock()
    dock.show()
    try:
        for scale in (0.85, 0.8):
            theme.set_ui_scale(scale)
            dock.apply_scale()
            _settle(app)
            items = list(dock._items.values())
            for left, right in zip(items, items[1:]):
                assert left.geometry().right() <= right.geometry().x() + 1
    finally:
        dock.close()
        _reset_scale()


def test_toolbar_buttons_normal(app: QApplication) -> None:
    """I: toolbar buttons keep non-zero geometry and icons at scale."""
    tb = _fresh_toolbar()
    tb.show()
    try:
        theme.set_ui_scale(0.8)
        tb.apply_scale()
        _settle(app)
        for item in tb._items.values():
            assert item.width() > 0 and item.height() > 0
            assert item._icon.width() > 0 and item._icon.height() > 0
    finally:
        tb.close()
        _reset_scale()


def test_no_cumulative_drift(app: QApplication) -> None:
    """J: 1.0 -> .95 -> .90 -> .85 -> 1.0 restores exact original geometry."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(1.0)
        dock.apply_scale()
        _settle(app)
        w0 = dock.width()
        icon0 = dock._items["claude"]._icon.width()
        for s in (0.95, 0.90, 0.85):
            theme.set_ui_scale(s)
            dock.apply_scale()
            _settle(app)
        theme.set_ui_scale(1.0)
        dock.apply_scale()
        _settle(app)
        assert dock.width() == w0
        assert dock._items["claude"]._icon.width() == icon0
    finally:
        dock.close()
        _reset_scale()


def test_roundtrip_extremes(app: QApplication) -> None:
    """K: 1.0 -> .6 -> 1.4 -> 1.0 restores exact original geometry."""
    dock = _fresh_dock()
    dock.show()
    try:
        theme.set_ui_scale(1.0)
        dock.apply_scale()
        _settle(app)
        w0 = dock.width()
        for s in (0.6, 1.4):
            theme.set_ui_scale(s)
            dock.apply_scale()
            _settle(app)
        theme.set_ui_scale(1.0)
        dock.apply_scale()
        _settle(app)
        assert dock.width() == w0
    finally:
        dock.close()
        _reset_scale()


def main() -> None:
    app = QApplication.instance() or QApplication([])
    from app import VisualShell

    test_default_scale()
    test_wheel_up_step()
    test_wheel_down_step()
    test_clamp_bounds()
    test_nan_and_type_fallback()
    test_persistence_roundtrip()
    test_malformed_preference_fallback()

    with tempfile.TemporaryDirectory() as td:
        shell = VisualShell(
            None,
            sessions_file=Path(td) / "sessions.json",
            workspace_settings_file=Path(td) / "ui_settings.json",
        )
        try:
            test_no_scale_mode_wheel_noop(shell)
            test_scale_mode_wheel_changes(shell)
            test_right_click_toggles_mode(shell)
            test_esc_exits_mode(shell)
            test_pet_size_scales(shell)
            test_dock_toolbar_bubble_scale(shell)
            test_dock_centered_under_pet(shell)
            test_toolbar_right_of_pet(shell)
            test_bubble_upper_left_of_pet(shell)
            test_anchor_preserved_on_scale(shell)
        finally:
            shell.shutdown()

    # Real character render-size scaling + responsive dock + shell regression.
    test_pet_render_size_scales(app)
    test_pet_frame_within_viewport(app)
    test_pet_render_roundtrip(app)
    test_dock_full_at_090_compact_at_085(app)
    test_compact_labels_hidden_icons_visible(app)
    test_compact_buttons_clickable(app)
    test_compact_tooltips(app)
    test_compact_status_dot_visible(app)
    test_compact_restores_labels_at_090(app)
    test_compact_dock_narrower_than_full(app)
    test_toolbar_icon_follows_scale(app)
    test_popovers_keep_fixed_readable_size(app)
    test_labels_visible_full_hidden_compact(app)
    test_icon_dot_separator_proportional(app)
    test_children_within_item(app)
    test_items_no_overlap(app)
    test_toolbar_buttons_normal(app)
    test_no_cumulative_drift(app)
    test_roundtrip_extremes(app)

    _reset_scale()
    print("UI scale tests passed.")


if __name__ == "__main__":
    main()
