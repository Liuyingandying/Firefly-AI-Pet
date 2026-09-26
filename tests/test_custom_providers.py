"""用户自配 Provider（custom OpenAI 兼容端点）测试。

覆盖：凭据库 CRUD 与校验、ai_router 回退链尾部追加（含禁用过滤/热更新）、
Provider Manager 窗口的自定义卡片渲染与删除流程。全程离屏、零真实网络。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from core import custom_providers as cp
from core.ai_router import (
    ProviderRouter,
    set_custom_provider_loader,
)
from core.credential_store import CredentialStore
from ui.provider_manager_window import ProviderManagerWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])

ENTRY = {
    "id": "siliconflow",
    "name": "硅基流动",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "deepseek-ai/DeepSeek-V3",
    "api_key": "sk-test-123",
    "enabled": True,
}


@pytest.fixture
def store(tmp_path):
    return CredentialStore(Path(tmp_path) / "credentials.json")


# -- 凭据库 CRUD -------------------------------------------------------------


def test_save_and_list_roundtrip(store):
    saved = cp.save_custom_provider(store, ENTRY)
    assert saved["id"] == "siliconflow"
    entries = cp.list_custom_providers(store)
    assert [e["id"] for e in entries] == ["siliconflow"]
    assert entries[0]["api_key"] == "sk-test-123"
    assert entries[0]["enabled"] is True


def test_save_upserts_by_id(store):
    cp.save_custom_provider(store, ENTRY)
    cp.save_custom_provider(store, {**ENTRY, "model": "updated-model"})
    entries = cp.list_custom_providers(store)
    assert len(entries) == 1
    assert entries[0]["model"] == "updated-model"


def test_delete_returns_false_when_absent(store):
    assert cp.delete_custom_provider(store, "nope") is False
    cp.save_custom_provider(store, ENTRY)
    assert cp.delete_custom_provider(store, "siliconflow") is True
    assert cp.list_custom_providers(store) == []


def test_disabled_entries_filtered_for_router(store):
    cp.save_custom_provider(store, {**ENTRY, "enabled": False})
    assert cp.enabled_custom_providers(store) == []


@pytest.mark.parametrize(
    "bad",
    [
        {**ENTRY, "id": "Bad Id"},
        {**ENTRY, "name": ""},
        {**ENTRY, "base_url": "ftp://x"},
        {**ENTRY, "model": ""},
        {**ENTRY, "api_key": "line1\nline2"},
    ],
)
def test_invalid_entries_rejected(store, bad):
    with pytest.raises(cp.CustomProviderError):
        cp.save_custom_provider(store, bad)


def test_damaged_payload_degrades_to_empty(store):
    store.save(cp.CUSTOM_PROVIDERS_KEY, "not-json{")
    assert cp.list_custom_providers(store) == []


# -- 路由集成（回退链尾部追加） -----------------------------------------------


def test_router_appends_enabled_custom_providers(store, monkeypatch):
    cp.save_custom_provider(store, ENTRY)
    cp.save_custom_provider(
        store, {**ENTRY, "id": "second", "name": "第二", "enabled": False}
    )
    monkeypatch.setattr(
        "core.ai_router._custom_provider_loader",
        lambda: cp.enabled_custom_providers(store),
    )
    try:
        router = ProviderRouter()
        names = [str(p.name) for p in router.providers]
        assert names[:3] == ["tju", "zhipu", "deepseek"]
        assert "custom:siliconflow" in names
        assert "custom:second" not in names  # 禁用条目不进回退链
    finally:
        set_custom_provider_loader(None)


def test_router_reload_picks_up_new_entries(store, monkeypatch):
    monkeypatch.setattr(
        "core.ai_router._custom_provider_loader",
        lambda: cp.enabled_custom_providers(store),
    )
    try:
        router = ProviderRouter()
        assert all(
            str(p.name) != "custom:siliconflow" for p in router.providers
        )
        cp.save_custom_provider(store, ENTRY)
        assert router.reload() is True
        assert any(
            str(p.name) == "custom:siliconflow" for p in router.providers
        )  # 热更新：无需重启
    finally:
        set_custom_provider_loader(None)


def test_router_clean_without_loader(store):
    router = ProviderRouter()
    names = [str(p.name) for p in router.providers]
    assert names == ["tju", "zhipu", "deepseek"]


# -- Provider Manager 窗口（卡片渲染 + 删除流程） -----------------------------


def test_window_renders_custom_cards(qapp, store):
    cp.save_custom_provider(store, ENTRY)
    window = ProviderManagerWindow(store=store, reload_fn=lambda: 0)
    try:
        assert len(window.custom_cards()) == 1
        texts = " ".join(card.label_text() for card in window.custom_cards())
        assert "硅基流动" in texts
        assert "api.siliconflow.cn" in texts
        # 内置 Provider 卡片也应渲染（tju/zhipu/deepseek 至少 3 张）
        assert len(window.cards()) >= 3
    finally:
        window.close()


def test_window_delete_custom_card(qapp, store, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    reloads = []
    cp.save_custom_provider(store, ENTRY)
    window = ProviderManagerWindow(store=store, reload_fn=lambda: reloads.append(1))
    try:
        # 模拟用户点击确认框的"是"，避免模态阻塞
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.Yes
        )
        window._delete_custom_provider("siliconflow")
        assert cp.list_custom_providers(store) == []
        assert reloads == [1]  # 删除后触发热更新
    finally:
        window.close()
