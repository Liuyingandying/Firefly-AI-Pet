"""ProviderManager status-layer tests (Provider Manager Phase 3A).

Covers: configured/source reporting (credential_store / missing /
environment / dotenv), no key values anywhere in the output, catalog reuse
(CLI exclusion, deprecated flag), and state-version bumping driven by the
RuntimeBus ``providers.updated`` event after a reload.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core import ai_router
from core.ai_router import ProviderRouter, reload_default_routers, set_updated_publisher
from core.credential_store import CredentialStore
from core.provider_manager import (
    SOURCE_CREDENTIAL_STORE,
    SOURCE_DOTENV,
    SOURCE_ENVIRONMENT,
    SOURCE_MISSING,
    ProviderManager,
)
from core.runtime_bus import RuntimeBus, RuntimeEvent

ROW_KEYS = {"id", "display_name", "credential_key", "configured", "source", "enabled"}
SECRET = "sk-phase3-secret-value"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(path=tmp_path / "credentials" / "credentials.json")


@pytest.fixture()
def env_file(tmp_path: Path) -> Path:
    return tmp_path / ".env"


@pytest.fixture()
def manager(store: CredentialStore, env_file: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("TJULLM_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return ProviderManager(store=store, env_file=env_file)


def _drain_until(qapp: QApplication, predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
    return predicate()


# ---------------------------------------------------------------------------
# configured / missing / source reporting
# ---------------------------------------------------------------------------


def test_saved_key_shows_credential_store(
    manager: ProviderManager, store: CredentialStore
):
    store.save("TJULLM_API_KEY", SECRET)
    assert manager.get_provider_status("tju") == {
        "configured": True,
        "source": SOURCE_CREDENTIAL_STORE,
    }
    # same credential shared by the vision entry
    assert manager.get_provider_status("tju-qwen")["source"] == SOURCE_CREDENTIAL_STORE

    row = next(r for r in manager.list_providers() if r["id"] == "tju")
    assert row["configured"] is True
    assert row["source"] == SOURCE_CREDENTIAL_STORE
    assert row["credential_key"] == "TJULLM_API_KEY"
    assert row["enabled"] is True


def test_unconfigured_shows_missing(manager: ProviderManager):
    assert manager.get_provider_status("tju") == {
        "configured": False,
        "source": SOURCE_MISSING,
    }
    rows = {r["id"]: r for r in manager.list_providers()}
    assert rows["zhipu"]["configured"] is False
    assert rows["zhipu"]["source"] == SOURCE_MISSING
    # deprecated entry ships as a disabled row for UIs to grey out
    assert rows["dashscope-qwen"]["enabled"] is False
    assert rows["dashscope-qwen"]["configured"] is False


def test_env_priority_shows_environment(
    manager: ProviderManager,
    store: CredentialStore,
    monkeypatch: pytest.MonkeyPatch,
):
    store.save("TJULLM_API_KEY", SECRET)
    monkeypatch.setenv("TJULLM_API_KEY", SECRET)
    assert manager.get_provider_status("tju")["source"] == SOURCE_ENVIRONMENT

    # store wins over dotenv even when the env layer is out of the picture
    monkeypatch.delenv("TJULLM_API_KEY")
    assert manager.get_provider_status("tju")["source"] == SOURCE_CREDENTIAL_STORE


def test_dotenv_layer_detected(
    manager: ProviderManager,
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    env_file.write_text("DEEPSEEK_API_KEY=dotenv-deepseek\n", encoding="utf-8")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert manager.get_provider_status("deepseek") == {
        "configured": True,
        "source": SOURCE_DOTENV,
    }


def test_provider_precedence_order_full_matrix(
    manager: ProviderManager,
    store: CredentialStore,
    env_file: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    env_file.write_text("ZHIPU_API_KEY=dotenv-zhipu\n", encoding="utf-8")
    assert manager.get_provider_status("zhipu")["source"] == SOURCE_DOTENV
    store.save("ZHIPU_API_KEY", SECRET)
    assert manager.get_provider_status("zhipu")["source"] == SOURCE_CREDENTIAL_STORE
    monkeypatch.setenv("ZHIPU_API_KEY", SECRET)
    assert manager.get_provider_status("zhipu")["source"] == SOURCE_ENVIRONMENT


# ---------------------------------------------------------------------------
# key values must never leak
# ---------------------------------------------------------------------------


def test_key_values_never_leak(
    manager: ProviderManager,
    store: CredentialStore,
    monkeypatch: pytest.MonkeyPatch,
):
    store.save("TJULLM_API_KEY", SECRET)
    store.save("ZHIPU_API_KEY", "another-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-secret")

    output = json.dumps(
        {
            "rows": manager.list_providers(),
            "tju": manager.get_provider_status("tju"),
            "zhipu": manager.get_provider_status("zhipu"),
            "deepseek": manager.get_provider_status("deepseek"),
        },
        ensure_ascii=False,
    )
    for secret in (SECRET, "another-secret", "env-secret"):
        assert secret not in output
    for row in manager.list_providers():
        assert set(row) == ROW_KEYS
    assert set(manager.get_provider_status("tju")) == {"configured", "source"}


# ---------------------------------------------------------------------------
# catalog reuse: no copied provider list
# ---------------------------------------------------------------------------


def test_list_matches_catalog_and_excludes_cli(manager: ProviderManager):
    from core.providers.catalog import CATALOG, CATEGORY_CODING_AGENT

    rows = {r["id"]: r for r in manager.list_providers()}
    expected = {
        spec.id
        for spec in CATALOG.values()
        if spec.category != CATEGORY_CODING_AGENT and spec.credential_env
    }
    assert set(rows) == expected
    assert "claude-cli" not in rows and "codex-cli" not in rows


def test_unknown_and_cli_ids_raise(manager: ProviderManager):
    with pytest.raises(KeyError):
        manager.get_provider_status("no-such-provider")
    with pytest.raises(KeyError):
        manager.get_provider_status("claude-cli")
    with pytest.raises(KeyError):
        manager.get_provider_status("codex-cli")


# ---------------------------------------------------------------------------
# RuntimeBus: providers.updated → re-query readiness
# ---------------------------------------------------------------------------


def test_reload_event_bumps_state_version(
    qapp: QApplication,
    manager: ProviderManager,
    store: CredentialStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from providers.deepseek import DeepSeekProvider
    from providers.tju_qwen import TJUQwenProvider
    from providers.zhipu_glm import ZhipuGLMProvider

    monkeypatch.setattr(ai_router, "TJUQwenProvider", TJUQwenProvider)
    monkeypatch.setattr(ai_router, "ZhipuGLMProvider", ZhipuGLMProvider)
    monkeypatch.setattr(ai_router, "DeepSeekProvider", DeepSeekProvider)
    ai_router._live_routers.clear()  # registry isolation for the count assert

    bus = RuntimeBus()
    unsubscribe = manager.connect_runtime_bus(bus)
    version_before = manager.state_version()

    published: list[int] = []

    def _publish() -> None:
        bus.publish_event(
            RuntimeEvent(kind="providers.updated", source="provider_manager")
        )
        published.append(1)

    set_updated_publisher(_publish)
    router = ProviderRouter(state_path=tmp_path / "provider_state.json")
    try:
        store.save("TJULLM_API_KEY", SECRET)
        assert manager.state_version() == version_before  # nothing reloaded yet
        assert reload_default_routers() == 1
        assert _drain_until(
            qapp, lambda: manager.state_version() >= version_before + 1
        )
        # queries are live: the new credential is visible immediately
        assert manager.get_provider_status("tju")["source"] == SOURCE_CREDENTIAL_STORE

        # unrelated events must not bump the version
        version_now = manager.state_version()
        bus.publish_event(RuntimeEvent(kind="camera.observed", source="stub"))
        assert _drain_until(qapp, lambda: True)
        qapp.processEvents()
        assert manager.state_version() == version_now

        # after unsubscribe, events no longer bump the version
        unsubscribe()
        store.save("TJULLM_API_KEY", SECRET + "-2")
        reload_default_routers()
        assert _drain_until(qapp, lambda: True)
        assert manager.state_version() == version_now
    finally:
        set_updated_publisher(None)
        unsubscribe()
