"""Targeted acceptance for Firefly UI Consolidation P0."""

from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtWidgets import QApplication

from ui.agent_dock import AgentDock, LAUNCHERS
from ui.pagelens_panel import PageLensPanel
from ui.settings_popover import SettingsPopover
from ui.vertical_toolbar import TOOLBAR_ACTIONS, VerticalToolbar


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _Settings:
    notifications_enabled = True
    keep_awake_enabled = False
    greeting_on_startup = True
    screen_vision_fast_mode = False

    def connect(self, callback):
        self.callback = callback

    def set_notifications_enabled(self, value): self.notifications_enabled = value
    def set_keep_awake_enabled(self, value): self.keep_awake_enabled = value
    def set_greeting_on_startup(self, value): self.greeting_on_startup = value
    def set_screen_vision_fast_mode(self, value): self.screen_vision_fast_mode = value


class _Autostart:
    @staticmethod
    def is_enabled(): return False
    @staticmethod
    def enable(): return None
    @staticmethod
    def disable(): return None


def test_primary_toolbar_has_only_frozen_actions_and_no_empty_slots():
    """Scratchpad v1 (任务书 §14) 在主导航条追加了 note 入口：4+1。"""
    _app()
    expected = ["companion", "scratchpad", "pagelens", "workspace", "settings"]
    assert [action[0] for action in TOOLBAR_ACTIONS] == expected
    toolbar = VerticalToolbar()
    try:
        assert list(toolbar._items) == expected
        assert len(toolbar._separators) == len(expected) - 1
        assert toolbar.height() < 583
    finally:
        toolbar.close()


def test_agent_dock_still_exposes_all_four_direct_launchers():
    _app()
    expected = ["claude", "codex", "qwen", "zcode"]
    assert [launcher[0] for launcher in LAUNCHERS] == expected
    dock = AgentDock()
    try:
        assert list(dock._items) == expected
    finally:
        dock.close()


def test_pagelens_idle_is_compact_and_has_natural_language_secondary_actions():
    app = _app()
    panel = PageLensPanel()
    try:
        panel.show_panel()
        app.processEvents()
        assert panel._scroll.isHidden()
        assert panel._footer.isHidden()
        assert panel._image_toolbar.isHidden()
        assert panel._select_btn.text() == "划词"
        assert panel._region_btn.text() == "框选"
        assert panel.height() < 300
    finally:
        panel.close()


def test_pagelens_context_and_concepts_update_without_opening_explanation():
    app = _app()
    panel = PageLensPanel()
    try:
        panel.set_top_concepts(["Attention", "Transformer"])
        panel.set_current_context("3.2.2 Multi-Head Attention", "第 4 页")
        panel.show_panel()
        app.processEvents()
        assert len(panel._top_concept_widgets) == 2
        assert "Multi-Head Attention" in panel._current_context_label.text()
        assert "第 4 页" in panel._current_context_label.text()
        assert panel._scroll.isHidden()
    finally:
        panel.close()


def test_pagelens_sections_expand_only_when_populated():
    app = _app()
    panel = PageLensPanel()
    try:
        panel.set_concept({"term": "Attention", "summary": "An explanation."})
        panel.show_panel()
        app.processEvents()
        assert panel._scroll.isVisible()
        assert panel._content._summary_label.isVisible()
        assert panel._content._context_header.isHidden()
        assert panel._content._related_header.isHidden()
        assert panel._footer.isHidden()

        panel.set_concept({
            "term": "Attention",
            "summary": "An explanation.",
            "context": "Used to combine token information.",
            "related": ["Query"],
            "questions": ["为什么重要？"],
        })
        app.processEvents()
        assert panel._content._context_header.isVisible()
        assert panel._content._related_header.isVisible()
        assert panel._footer.isVisible()
        assert panel._content._questions_header.isHidden()
        panel._on_explore_clicked()
        app.processEvents()
        assert panel._content._questions_header.isVisible()
    finally:
        panel.close()


def test_pagelens_secondary_actions_reuse_signal_entries():
    _app()
    panel = PageLensPanel()
    seen = []
    panel.selection_requested.connect(lambda: seen.append("selection"))
    panel.visual_region_requested.connect(lambda: seen.append("region"))
    panel._select_btn.click()
    panel._region_btn.click()
    assert seen == ["selection", "region"]
    panel.close()


def test_settings_memory_section_opens_existing_management_entry():
    _app()
    popover = SettingsPopover(_Settings(), autostart=_Autostart())
    seen = []
    popover.memory_requested.connect(lambda: seen.append("memory"))
    try:
        assert popover._memory_btn.text() == "View Memory"
        popover._memory_btn.click()
        assert seen == ["memory"]
    finally:
        popover.close()


def test_settings_provider_manager_entry_emits_signal():
    """Provider Manager rc2: AI 模型管理入口按钮发射 provider_manager_requested。"""
    _app()
    popover = SettingsPopover(_Settings(), autostart=_Autostart())
    seen = []
    popover.provider_manager_requested.connect(lambda: seen.append("provider"))
    try:
        assert popover._provider_btn.text() == "AI 模型管理"
        assert popover._provider_btn.objectName() == "openProviderManager"
        popover._provider_btn.click()
        assert seen == ["provider"]
    finally:
        popover.close()


def test_memory_panel_without_legacy_close_signal_still_opens(monkeypatch):
    import app as app_module

    memory_service = object()
    suggestion_service = object()

    class _Panel:
        def __init__(self, **kwargs):
            self.memory_service = kwargs["memory_service"]
            self.visible = False
        def refresh(self): pass
        def show(self): self.visible = True
        def raise_(self): pass
        def activateWindow(self): pass

    monkeypatch.setattr(app_module, "MemoryPanel", _Panel)
    shell = SimpleNamespace(
        _memory_panel=None,
        character_conversation=SimpleNamespace(
            memory_service=memory_service,
            suggestion_service=suggestion_service,
            runtime=SimpleNamespace(
                _runtime=SimpleNamespace(bond_state_engine=object()),
            ),
        ),
    )
    app_module.VisualShell._ensure_memory_panel(shell)
    assert shell._memory_panel.visible
    assert shell._memory_panel.memory_service is memory_service


def test_removed_toolbar_exposure_does_not_remove_backend_handlers():
    from app import VisualShell
    from core.pdf_text_hit_test import hit_test
    from core.pdf_visual_region import crop_region
    from ui.explain_box import ExplainBox
    from ui.memory_panel import MemoryPanel

    assert callable(VisualShell._on_pdf_overlay_requested)
    assert callable(VisualShell._on_pdf_visual_requested)
    assert callable(hit_test)
    assert callable(crop_region)
    assert ExplainBox is not None
    assert MemoryPanel is not None
