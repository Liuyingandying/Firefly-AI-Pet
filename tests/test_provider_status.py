"""Provider Status service + Settings AI Status view tests (Phase 2.2).

Locks:
- every (non-deprecated) catalog provider yields a status row
- text status: TJU primary/active, Zhipu fallback, DeepSeek fallback
- vision status: TJU-Qwen primary/active
- coding status: Codex / Claude Code CLI entries exist
- opening/closing Settings never affects runtime state
- unavailable providers render as "(unavailable)" without dialogs
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from core.providers.catalog import CATALOG, STATUS_DEPRECATED
from core.providers.status import (
    CATEGORY_MEMORY,
    ROLE_FALLBACK,
    ROLE_INDEPENDENT,
    ROLE_PRIMARY,
    ProviderStatusService,
)


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _text_statuses(service) -> dict[str, object]:
    return {
        s.provider_id: s
        for s in service.snapshot()
        if s.category in ("text",)
    }


# ---------------------------------------------------------------------------
# 1. every catalog provider yields a status row
# ---------------------------------------------------------------------------


def test_catalog_providers_generate_status() -> None:
    service = ProviderStatusService()
    rows = service.snapshot()
    by_id = {row.provider_id: row for row in rows}

    for spec in CATALOG.values():
        if spec.status == STATUS_DEPRECATED:
            continue
        assert spec.id in by_id, f"catalog provider {spec.id} missing from status"
        row = by_id[spec.id]
        assert row.category == spec.category
        assert row.status == spec.status
        assert row.model == spec.model
        assert isinstance(row.available, bool)

    # Deprecated entries are documentation-only: never shown.
    assert "dashscope-qwen" not in by_id


# ---------------------------------------------------------------------------
# 2. text status: TJU active/primary, Zhipu/DeepSeek fallback
# ---------------------------------------------------------------------------


def test_text_status_roles_and_order() -> None:
    service = ProviderStatusService()
    text = _text_statuses(service)

    tju = text["tju"]
    assert tju.role == ROLE_PRIMARY
    assert tju.status == "active"
    assert tju.fallback_rank == 0
    assert tju.display_name == "TJU LLM"

    zhipu = text["zhipu"]
    assert zhipu.role == ROLE_FALLBACK
    assert zhipu.status == "fallback"
    assert zhipu.fallback_rank == 1

    deepseek = text["deepseek"]
    assert deepseek.role == ROLE_FALLBACK
    assert deepseek.status == "fallback"
    assert deepseek.fallback_rank == 2


# ---------------------------------------------------------------------------
# 3. vision status: TJU-Qwen primary/active
# ---------------------------------------------------------------------------


def test_vision_status_roles_and_order() -> None:
    service = ProviderStatusService()
    vision = {
        s.provider_id: s
        for s in service.snapshot()
        if s.category == "vision"
    }

    assert vision["tju-qwen"].role == ROLE_PRIMARY
    assert vision["tju-qwen"].status == "active"
    assert vision["tju-qwen"].fallback_rank == 0
    assert vision["tju-qwen"].display_name == "TJU-Qwen"

    assert vision["deepseek-vision"].role == ROLE_FALLBACK
    assert vision["deepseek-vision"].fallback_rank == 1
    assert vision["glm-vision"].role == ROLE_FALLBACK
    assert vision["glm-vision"].fallback_rank == 2


# ---------------------------------------------------------------------------
# 4. coding status: Codex / Claude Code CLI
# ---------------------------------------------------------------------------


def test_coding_status_entries(monkeypatch) -> None:
    # Deterministic: both CLIs present.
    monkeypatch.setattr(
        "core.providers.status.shutil.which",
        lambda cmd: f"C:/fake/{cmd}.CMD",
    )
    service = ProviderStatusService()
    coding = {
        s.provider_id: s
        for s in service.snapshot()
        if s.category == "coding_agent"
    }

    assert coding["codex-cli"].display_name == "Codex CLI"
    assert coding["codex-cli"].role == ROLE_INDEPENDENT
    assert coding["codex-cli"].available is True
    assert coding["claude-cli"].display_name == "Claude Code CLI"
    assert coding["claude-cli"].role == ROLE_INDEPENDENT
    assert coding["claude-cli"].available is True


def test_coding_status_missing_cli_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(
        "core.providers.status.shutil.which",
        lambda cmd: None,
    )
    service = ProviderStatusService()
    coding = {
        s.provider_id: s
        for s in service.snapshot()
        if s.category == "coding_agent"
    }
    assert coding["codex-cli"].available is False
    assert coding["claude-cli"].available is False


# ---------------------------------------------------------------------------
# 5. Settings open/close does not affect runtime
# ---------------------------------------------------------------------------


class _FakeAutostart:
    def is_enabled(self) -> bool:
        return False


class _FakeManager:
    connect = lambda self, _cb: None  # noqa: E731 - manager subscribe noop

    def __init__(self) -> None:
        self.notifications_enabled = False
        self.keep_awake_enabled = False
        self.greeting_on_startup = False
        self.screen_vision_fast_mode = True


def test_settings_open_close_does_not_affect_runtime(qapp) -> None:
    from ui.settings_popover import SettingsPopover

    service = ProviderStatusService()
    before = service.snapshot()

    popover = SettingsPopover(_FakeManager(), autostart=_FakeAutostart())
    try:
        popover.show()
        popover.refresh()  # renders the AI status block
        assert "Firefly AI Status" in popover._ai_status_label.text()
        popover.close()
    finally:
        popover.deleteLater()
        qapp.processEvents()

    after = service.snapshot()
    assert before == after, "opening/closing Settings changed runtime status"


# ---------------------------------------------------------------------------
# 6. unavailable providers render without dialogs
# ---------------------------------------------------------------------------


def test_unavailable_provider_renders_without_dialog(monkeypatch, qapp) -> None:
    from ui.settings_popover import SettingsPopover

    # Simulate a host where no API credentials resolve.
    monkeypatch.setattr("providers.base.resolve_setting", lambda _name, **_kw: "")
    service = ProviderStatusService()
    text = service.render_text()

    assert "DeepSeek (unavailable)" in text
    assert "TJU LLM (unavailable)" in text
    assert "Firefly AI Status" in text

    # The view must render without any dialog.
    popover = SettingsPopover(_FakeManager(), autostart=_FakeAutostart())
    try:
        popover.refresh()
        rendered = popover._ai_status_label.text()
        assert "DeepSeek (unavailable)" in rendered
    finally:
        popover.deleteLater()
        qapp.processEvents()


def test_memory_status_entry() -> None:
    service = ProviderStatusService()
    memory = [s for s in service.snapshot() if s.category == CATEGORY_MEMORY]
    assert len(memory) == 1
    assert memory[0].provider_id == "local-memory"
    assert memory[0].display_name == "Local Memory"
    assert memory[0].available is True
    assert memory[0].role == ROLE_INDEPENDENT
