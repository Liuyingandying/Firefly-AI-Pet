"""Provider Manager window tests (Provider Manager Phase 3B).

Covers: window creation, provider card rendering (status dot / source /
masked key), save flow (store + reload + refresh), delete flow, no plaintext
key rendering, and RuntimeBus ``providers.updated`` refresh.

All credentials are synthetic; no network happens anywhere (reload only
re-builds adapter objects), and stores / router state live under tmp_path.
"""

from __future__ import annotations

import faulthandler

faulthandler.enable(all_threads=True)

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QInputDialog

from core import ai_router
from core.ai_router import (
    ProviderRouter,
    reload_default_routers,
    set_updated_publisher,
)
from core.credential_store import CredentialStore
from core.provider_manager import ProviderManager
from core.runtime_bus import RuntimeBus, RuntimeEvent
from providers import base as provider_base
from ui.provider_manager_window import KEY_MASK, ProviderManagerWindow

SECRET = "sk-phase3b-synthetic-secret"
EXPECTED_IDS = {
    "tju", "zhipu", "deepseek",
    "tju-qwen", "glm-vision", "deepseek-vision",
    "tju-reasoning", "glm-reasoning", "deepseek-reasoning",
    "dashscope-qwen",
}


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
def wired(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    # qapp MUST come first: constructing QWidgets without a QApplication is a
    # hard qFatal abort (no traceback, instant process death).
    store = CredentialStore(path=tmp_path / "credentials" / "credentials.json")
    # note: TJULLM deliberately absent so the tju card starts unconfigured
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ZHIPU_API_KEY=dotenv-zhipu\nDEEPSEEK_API_KEY=dotenv-deepseek\n",
        encoding="utf-8",
    )
    for key in ("TJULLM_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(provider_base, "DEFAULT_ENV_FILE", env_file)

    reload_fn = MagicMock(return_value=1)
    manager = ProviderManager(store=store, env_file=env_file)
    window = ProviderManagerWindow(manager=manager, store=store, reload_fn=reload_fn)
    yield SimpleNamespace(
        store=store,
        env_file=env_file,
        reload_fn=reload_fn,
        manager=manager,
        window=window,
    )
    window.close()


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
# 1. window creation
# ---------------------------------------------------------------------------


def test_window_creation(wired: SimpleNamespace):
    window = wired.window
    assert isinstance(window, QDialog)
    assert window.windowTitle() == "AI 模型设置"
    assert {card.provider_id for card in window.cards()} == EXPECTED_IDS


# ---------------------------------------------------------------------------
# 2. provider list display
# ---------------------------------------------------------------------------


def test_provider_list_display(wired: SimpleNamespace):
    window = wired.window
    tju = _card(window, "tju")
    assert tju.display_name == "TJU LLM"
    assert tju.status_label.text() == "○ 未配置"
    assert tju.source_label.text().startswith("来源：missing")
    assert tju.key_label.text() == ""
    assert tju.add_button.isVisibleTo(tju)
    assert not tju.replace_button.isVisibleTo(tju)
    assert not tju.delete_button.isVisibleTo(tju)
    assert tju.add_button.objectName() == "addKey-tju"
    assert tju.replace_button.objectName() == "replaceKey-tju"
    assert tju.delete_button.objectName() == "deleteKey-tju"

    # deprecated entry ships as a review-only row (no actions, greyed source)
    dash = _card(window, "dashscope-qwen")
    assert dash.enabled is False
    assert dash.key_label.text() == ""


def test_unconfigured_vs_configured_rendering(wired: SimpleNamespace):
    window = wired.window
    unconfigured = _card(window, "tju")
    assert unconfigured.add_button.isVisibleTo(unconfigured)
    assert not unconfigured.replace_button.isVisibleTo(unconfigured)
    assert not unconfigured.delete_button.isVisibleTo(unconfigured)

    wired.store.save("TJULLM_API_KEY", SECRET)
    window.refresh()
    configured = _card(window, "tju")
    assert configured.status_label.text() == "● 已配置"
    assert configured.key_label.text() == KEY_MASK
    assert configured.replace_button.isVisibleTo(configured)
    assert configured.delete_button.isVisibleTo(configured)
    assert not configured.add_button.isVisibleTo(configured)
    assert configured.source_label.text().startswith("来源：credential_store")


# ---------------------------------------------------------------------------
# 3. save flow: store → reload → refresh
# ---------------------------------------------------------------------------


def test_save_flow_invokes_store_and_reload(wired: SimpleNamespace):
    window = wired.window
    window._save_key("tju", "TJULLM_API_KEY", SECRET)
    wired.reload_fn.assert_called_once()
    assert wired.store.get("TJULLM_API_KEY") == SECRET
    assert wired.manager.get_provider_status("tju") == {
        "configured": True,
        "source": "credential_store",
    }
    assert _card(window, "tju").key_label.text() == KEY_MASK


def test_save_with_empty_value_is_noop(wired: SimpleNamespace):
    window = wired.window
    window._save_key("tuj", "TJULLM_API_KEY", "")
    wired.reload_fn.assert_not_called()
    assert wired.store.get("TJULLM_API_KEY") is None


# ---------------------------------------------------------------------------
# 4. delete flow: store → reload → refresh
# ---------------------------------------------------------------------------


def test_delete_flow(wired: SimpleNamespace):
    wired.store.save("TJULLM_API_KEY", SECRET)
    window = wired.window
    window.refresh()

    window._delete_key("tju", "TJULLM_API_KEY")
    wired.reload_fn.assert_called_once()
    assert wired.store.get("TJULLM_API_KEY") is None
    tju = _card(window, "tju")
    assert tju.status_label.text() == "○ 未配置"
    assert tju.add_button.isVisibleTo(tju)


# ---------------------------------------------------------------------------
# 5. the plaintext key is never rendered
# ---------------------------------------------------------------------------


def test_key_plaintext_never_rendered(
    wired: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
):
    window = wired.window
    window._save_key("tju", "TJULLM_API_KEY", SECRET)

    rendered = "\n".join(label.text() for label in window.findChildren(QLabel))
    assert SECRET not in rendered
    assert _card(window, "tju").key_label.text() == KEY_MASK

    # the prompt never prefills the stored value and empty input saves nothing
    captured: dict[str, str] = {}

    def fake_get_text(parent, title, label, *args, **kwargs):
        captured["label"] = label
        return ("", True)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    tju = _card(window, "tju")
    tju._on_add_or_replace()
    assert SECRET not in captured.get("label", "")
    assert tju.key_label.text() == KEY_MASK  # unchanged


# ---------------------------------------------------------------------------
# 6. providers.updated event triggers refresh
# ---------------------------------------------------------------------------


def test_bus_event_triggers_refresh(
    qapp: QApplication,
    wired: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from providers.deepseek import DeepSeekProvider
    from providers.tju_qwen import TJUQwenProvider
    from providers.zhipu_glm import ZhipuGLMProvider

    monkeypatch.setattr(ai_router, "TJUQwenProvider", TJUQwenProvider)
    monkeypatch.setattr(ai_router, "ZhipuGLMProvider", ZhipuGLMProvider)
    monkeypatch.setattr(ai_router, "DeepSeekProvider", DeepSeekProvider)

    bus = RuntimeBus()
    window = ProviderManagerWindow(
        manager=wired.manager,
        store=wired.store,
        runtime_bus=bus,
        reload_fn=wired.reload_fn,
    )
    refresh_calls: list[int] = []
    original_refresh = window.refresh

    def counting_refresh() -> None:
        refresh_calls.append(1)
        original_refresh()

    window.refresh = counting_refresh  # type: ignore[assignment]

    set_updated_publisher(
        lambda: bus.publish_event(
            RuntimeEvent(kind="providers.updated", source="provider_manager")
        )
    )
    # one live default router so reload_default_routers actually rebuilds
    router = ProviderRouter(state_path=tmp_path / "provider_state.json")
    try:
        wired.store.save("TJULLM_API_KEY", SECRET)
        assert reload_default_routers() == 1
        assert _drain(qapp, lambda: len(refresh_calls) >= 1)

        # unrelated events do not refresh the window
        count_before = len(refresh_calls)
        bus.publish_event(RuntimeEvent(kind="camera.observed", source="stub"))
        assert _drain(qapp, lambda: len(refresh_calls) > count_before) is False
        assert len(refresh_calls) == count_before

        # after unsubscribe, providers.updated no longer refreshes
        window._bus_unsubscribe and window._bus_unsubscribe()
        window._bus_unsubscribe = None
        reload_default_routers()
        assert _drain(qapp, lambda: len(refresh_calls) > count_before) is False
        assert len(refresh_calls) == count_before
    finally:
        set_updated_publisher(None)
        window._bus_unsubscribe = None
        window.close()
