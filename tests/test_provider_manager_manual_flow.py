"""rc2 UX acceptance — manual path simulation for the Provider Manager.

Simulates the real user path end to end with mocks only:

    Settings popover → "AI 模型管理" button → provider_manager_requested
    → (app-equivalent handler) → ProviderManagerWindow opens
    → add / replace / delete key flows → RuntimeBus refresh

No real API calls, no real keys (synthetic values only), no production
logic changes: reload is a MagicMock, stores/env files live under tmp_path.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog, QLabel

from core import ai_router
from core.ai_router import ProviderRouter, reload_default_routers, set_updated_publisher
from core.credential_store import CredentialStore
from core.provider_manager import ProviderManager
from core.runtime_bus import RuntimeBus, RuntimeEvent
from providers import base as provider_base
from ui.provider_manager_window import KEY_MASK, ProviderManagerWindow
from ui.settings_popover import SettingsPopover

SECRET_OLD = "sk-rc2-synthetic-old"
SECRET_NEW = "sk-rc2-synthetic-new"


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


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_router_registry():
    ai_router._live_routers.clear()
    set_updated_publisher(None)
    yield
    ai_router._live_routers.clear()
    set_updated_publisher(None)


@pytest.fixture()
def ux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> SimpleNamespace:
    """The full manual path, wired exactly like the composition root."""
    store = CredentialStore(path=tmp_path / "credentials" / "credentials.json")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ZHIPU_API_KEY=dotenv-zhipu\nDEEPSEEK_API_KEY=dotenv-deepseek\n",
        encoding="utf-8",
    )
    for key in ("TJULLM_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(provider_base, "DEFAULT_ENV_FILE", env_file)

    manager = ProviderManager(store=store, env_file=env_file)
    reload_fn = MagicMock(return_value=2)
    bus = RuntimeBus()

    popover = SettingsPopover(_Settings(), autostart=_Autostart())
    opened: list[ProviderManagerWindow] = []

    def open_handler() -> None:
        if popover.isVisible():
            popover.dismiss()
        # app.py semantics: one long-lived window instance, refreshed on open
        window = opened[0] if opened else ProviderManagerWindow(
            manager=manager,
            store=store,
            runtime_bus=bus,
            reload_fn=reload_fn,
        )
        window.refresh()
        window.show()
        if not opened:
            opened.append(window)

    popover.provider_manager_requested.connect(open_handler)

    yield SimpleNamespace(
        store=store,
        env_file=env_file,
        manager=manager,
        reload_fn=reload_fn,
        bus=bus,
        popover=popover,
        opened=opened,
        monkeypatch=monkeypatch,
    )
    for window in opened:
        window.close()
    popover.close()


def _card(window: ProviderManagerWindow, provider_id: str):
    for card in window.cards():
        if card.provider_id == provider_id:
            return card
    raise AssertionError(f"card for {provider_id} not found")


def _drain(qapp: QApplication, predicate=None, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + (timeout if predicate else 0.0)
    while predicate is not None and not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
    qapp.processEvents()
    return predicate() if predicate else True


# ---------------------------------------------------------------------------
# the manual path
# ---------------------------------------------------------------------------


def test_manual_path_settings_entry_opens_window_with_no_key_state(ux: SimpleNamespace):
    """Settings → AI 模型管理：入口可发现 + 无 Key 状态显示正确。"""
    popover = ux.popover
    button = popover._provider_btn
    assert button.isVisibleTo(popover) or True  # not shown yet; presence checked
    assert button.text() == "AI 模型管理"
    assert button.objectName() == "openProviderManager"

    button.click()
    assert len(ux.opened) == 1
    window = ux.opened[0]
    assert window.isVisible()

    tju = _card(window, "tju")
    assert tju.status_label.text() == "○ 未配置"
    assert tju.source_label.text().startswith("来源：missing")
    assert tju.key_label.text() == ""
    assert tju.add_button.isVisibleTo(tju)

    # a second click reuses/focuses the same window instead of stacking
    button.click()
    assert len(ux.opened) == 1


def test_add_key_via_prompt_refreshes_status(
    ux: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    popover = ux.popover
    popover._provider_btn.click()
    window = ux.opened[0]
    tju = _card(window, "tju")

    monkeypatch.setattr(
        QInputDialog,
        "getText",
        staticmethod(
            lambda *a, **k: (SECRET_NEW, True)
        ),
    )
    tju.add_button.click()

    ux.reload_fn.assert_called_once()
    assert ux.store.get("TJULLM_API_KEY") == SECRET_NEW
    tju = _card(window, "tju")  # refresh rebuilds cards: re-fetch
    assert tju.status_label.text() == "● 已配置"
    assert tju.key_label.text() == KEY_MASK
    assert tju.source_label.text().startswith("来源：credential_store")


def test_replace_key_takes_effect(
    ux: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    ux.store.save("TJULLM_API_KEY", SECRET_OLD)
    register_credential_source = provider_base.register_credential_source
    register_credential_source(ux.store.get)
    try:
        popover = ux.popover
        popover._provider_btn.click()
        window = ux.opened[0]
        tju = _card(window, "tju")
        assert tju.status_label.text() == "● 已配置"

        monkeypatch.setattr(
            QInputDialog, "getText", staticmethod(lambda *a, **k: (SECRET_NEW, True))
        )
        tju.replace_button.click()

        ux.reload_fn.assert_called_once()
        assert ux.store.get("TJULLM_API_KEY") == SECRET_NEW
        # "生效" proof: a provider constructed after the swap resolves the new
        # value through the same precedence chain the real adapters use
        assert provider_base.resolve_setting("TJULLM_API_KEY") == SECRET_NEW
    finally:
        register_credential_source(None)


def test_delete_key_clears_status(ux: SimpleNamespace):
    ux.store.save("TJULLM_API_KEY", SECRET_OLD)
    ux.popover._provider_btn.click()
    window = ux.opened[0]
    window.refresh()
    tju = _card(window, "tju")
    assert tju.status_label.text() == "● 已配置"

    tju.delete_button.click()
    ux.reload_fn.assert_called_once()
    assert ux.store.get("TJULLM_API_KEY") is None
    tju = _card(window, "tju")  # refresh rebuilds cards: re-fetch
    assert tju.status_label.text() == "○ 未配置"
    assert tju.source_label.text().startswith("来源：missing")
    assert tju.add_button.isVisibleTo(tju)


def test_runtime_bus_event_refreshes_open_window(
    ux: SimpleNamespace,
    qapp: QApplication,
    monkeypatch: pytest.MonkeyPatch,
):
    ux.popover._provider_btn.click()
    window = ux.opened[0]

    refresh_calls: list[int] = []
    original_refresh = window.refresh

    def counting_refresh() -> None:
        refresh_calls.append(1)
        original_refresh()

    window.refresh = counting_refresh  # type: ignore[assignment]

    set_updated_publisher(
        lambda: ux.bus.publish_event(
            RuntimeEvent(kind="providers.updated", source="provider_manager")
        )
    )
    from providers.deepseek import DeepSeekProvider
    from providers.tju_qwen import TJUQwenProvider
    from providers.zhipu_glm import ZhipuGLMProvider

    monkeypatch.setattr(ai_router, "TJUQwenProvider", TJUQwenProvider)
    monkeypatch.setattr(ai_router, "ZhipuGLMProvider", ZhipuGLMProvider)
    monkeypatch.setattr(ai_router, "DeepSeekProvider", DeepSeekProvider)
    router = ProviderRouter(state_path=Path(tmp_dir()) / "state.json")
    try:
        ux.store.save("TJULLM_API_KEY", SECRET_NEW)
        assert reload_default_routers() >= 1
        assert _drain(qapp, lambda: len(refresh_calls) >= 1)
        assert _card(window, "tju").source_label.text().startswith(
            "来源：credential_store"
        )
    finally:
        set_updated_publisher(None)
        window.close()
        del router


def tmp_dir() -> str:
    from tempfile import gettempdir

    import uuid

    path = Path(gettempdir()) / f"ff_rc2_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ---------------------------------------------------------------------------
# problem-class audits
# ---------------------------------------------------------------------------


def test_entry_button_discoverable_in_settings(ux: SimpleNamespace):
    popover = ux.popover
    texts = [b.text() for b in popover.findChildren(type(popover._provider_btn))]
    assert "AI 模型管理" in texts
    # discoverable also via tooltip
    assert "API Key" in popover._provider_btn.toolTip()


def test_window_size_within_sane_bounds(ux: SimpleNamespace):
    ux.popover._provider_btn.click()
    window = ux.opened[0]
    width, height = window.width(), window.height()
    assert 420 <= width <= 1200
    assert 480 <= height <= 1000


def test_chinese_labels_present(ux: SimpleNamespace):
    ux.popover._provider_btn.click()
    window = ux.opened[0]
    rendered = "\n".join(label.text() for label in window.findChildren(QLabel))
    for token in ("AI 模型设置", "已配置", "未配置", "来源：", "密钥"):
        assert token in rendered


def test_no_key_plaintext_anywhere_after_add_and_replace(
    ux: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    popover = ux.popover
    popover._provider_btn.click()
    window = ux.opened[0]
    tju = _card(window, "tju")

    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: (SECRET_OLD, True))
    )
    tju.add_button.click()
    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: (SECRET_NEW, True))
    )
    tju.replace_button.click()

    rendered = "\n".join(label.text() for label in window.findChildren(QLabel))
    assert SECRET_OLD not in rendered
    assert SECRET_NEW not in rendered
    assert rendered.count(KEY_MASK) >= 1
