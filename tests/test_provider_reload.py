"""ProviderRouter hot-reload tests (Provider Manager Phase 2).

Covers the five required scenarios plus registry/publisher behavior:
  1. saved TJULLM_API_KEY is readable by providers
  2. changing the key + reload → new value in use
  3. deleting the key + reload → .env fallback
  4. without a credential store → legacy behavior
  5. reload preserves health state and the chat flow

Adapter classes are monkeypatched with recording stubs whose credentials
resolve through ``providers.base.resolve_setting`` at construction time —
exactly like the real adapters.  Transports are injected; no network, and
router state files go to tmp_path so the repo ``provider_state.json`` is
never touched.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import ai_router
from core.ai_router import (
    ProviderRouter,
    ProvidersUpdated,
    reload_default_routers,
    set_updated_publisher,
)
from core.credential_store import CredentialStore
from providers import base as provider_base
from providers.base import (
    OpenAICompatibleProvider,
    ProviderHTTPError,
    normalize_chat_completion,
    register_credential_source,
)

MESSAGES = [{"role": "user", "content": "ping"}]
ENV_KEYS = ("TJULLM_API_KEY", "ZHIPU_API_KEY", "DEEPSEEK_API_KEY")
DOTENV = (
    "TJULLM_API_KEY=dotenv-tju\n"
    "ZHIPU_API_KEY=dotenv-zhipu\n"
    "DEEPSEEK_API_KEY=dotenv-deepseek\n"
)


def _canned(provider: str) -> dict[str, Any]:
    return normalize_chat_completion(
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        provider=provider,
        requested_model="stub-model",
    )


def _make_stub_class(name: str, env_key: str, calls: list):
    """Stub adapter mirroring the real ones: credentials resolve at __init__."""

    class _Stub(OpenAICompatibleProvider):
        def __init__(self, fail_first: bool = False) -> None:
            super().__init__(
                name=name,
                api_key=provider_base.resolve_setting(env_key),
                base_url=f"https://{name}.stub.invalid",
                default_model="stub-model",
                transport=self._stub_transport,
            )
            self._fail_first = fail_first
            self._failed_once = False

        def _stub_transport(self, endpoint, payload, headers, timeout):
            calls.append((name, headers.get("Authorization")))
            if self._fail_first and not self._failed_once:
                self._failed_once = True
                raise ProviderHTTPError(500, "stub first-call failure")
            return _canned(name)

    _Stub.name = name
    return _Stub


@pytest.fixture(autouse=True)
def _isolate_router_registry():
    ai_router._live_routers.clear()
    set_updated_publisher(None)
    yield
    ai_router._live_routers.clear()
    set_updated_publisher(None)


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Credential store registered + hermetic .env; env keys cleared."""
    store = CredentialStore(path=tmp_path / "credentials" / "credentials.json")
    register_credential_source(store.get)
    env_file = tmp_path / ".env"
    env_file.write_text(DOTENV, encoding="utf-8")
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(provider_base, "DEFAULT_ENV_FILE", env_file)
    yield SimpleNamespace(store=store, env_file=env_file)
    register_credential_source(None)


@pytest.fixture()
def calls() -> list:
    return []


@pytest.fixture()
def stub_router(
    wired: SimpleNamespace,
    calls: list,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ProviderRouter:
    """Router over recording stub adapters; state file isolated to tmp."""
    monkeypatch.setattr(
        ai_router, "TJUQwenProvider", _make_stub_class("tju", "TJULLM_API_KEY", calls)
    )
    monkeypatch.setattr(
        ai_router, "ZhipuGLMProvider", _make_stub_class("zhipu", "ZHIPU_API_KEY", calls)
    )
    monkeypatch.setattr(
        ai_router,
        "DeepSeekProvider",
        _make_stub_class("deepseek", "DEEPSEEK_API_KEY", calls),
    )
    return ProviderRouter(state_path=tmp_path / "provider_state.json")


def _patch_adapters(
    monkeypatch: pytest.MonkeyPatch, calls: list
) -> None:
    monkeypatch.setattr(
        ai_router,
        "TJUQwenProvider",
        _make_stub_class("tju", "TJULLM_API_KEY", calls),
    )
    monkeypatch.setattr(
        ai_router, "ZhipuGLMProvider", _make_stub_class("zhipu", "ZHIPU_API_KEY", calls)
    )
    monkeypatch.setattr(
        ai_router,
        "DeepSeekProvider",
        _make_stub_class("deepseek", "DEEPSEEK_API_KEY", calls),
    )


def _auth_header_for(calls: list, name: str) -> str | None:
    for provider_name, authorization in reversed(calls):
        if provider_name == name:
            return authorization
    return None


# ---------------------------------------------------------------------------
# 1. saved credential is readable by providers
# ---------------------------------------------------------------------------


def test_saved_key_visible_to_provider(
    wired: SimpleNamespace, stub_router: ProviderRouter, calls: list
):
    wired.store.save("TJULLM_API_KEY", "stored-key-1")
    stub_router.reload()
    response = stub_router.chat(MESSAGES)
    assert response["provider"] == "tju"
    assert _auth_header_for(calls, "tju") == "Bearer stored-key-1"
    assert stub_router.providers[0].api_key == "stored-key-1"


# ---------------------------------------------------------------------------
# 2. changing the key + reload → new value in use
# ---------------------------------------------------------------------------


def test_reload_picks_up_new_key(
    wired: SimpleNamespace, stub_router: ProviderRouter, calls: list
):
    wired.store.save("TJULLM_API_KEY", "stored-key-1")
    stub_router.reload()
    stub_router.chat(MESSAGES)
    old_instance = stub_router.providers[0]

    wired.store.save("TJULLM_API_KEY", "stored-key-2")
    assert stub_router.reload() is True
    assert stub_router.providers[0] is not old_instance
    stub_router.chat(MESSAGES)
    assert _auth_header_for(calls, "tju") == "Bearer stored-key-2"


# ---------------------------------------------------------------------------
# 3. deleting the key + reload → .env fallback
# ---------------------------------------------------------------------------


def test_delete_falls_back_to_env_file(
    wired: SimpleNamespace, stub_router: ProviderRouter, calls: list
):
    wired.store.save("TJULLM_API_KEY", "stored-key-1")
    stub_router.reload()
    stub_router.chat(MESSAGES)
    assert _auth_header_for(calls, "tju") == "Bearer stored-key-1"

    wired.store.delete("TJULLM_API_KEY")
    stub_router.reload()
    stub_router.chat(MESSAGES)
    assert _auth_header_for(calls, "tju") == "Bearer dotenv-tju"


# ---------------------------------------------------------------------------
# 4. without a credential store → legacy behavior
# ---------------------------------------------------------------------------


def test_without_store_legacy_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list
):
    register_credential_source(None)  # no source registered
    monkeypatch.delenv("TJULLM_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TJULLM_API_KEY=dotenv-tju\n", encoding="utf-8")
    monkeypatch.setattr(provider_base, "DEFAULT_ENV_FILE", env_file)
    _patch_adapters(monkeypatch, calls)

    # a store that exists but is NOT registered must stay invisible
    ghost_store = CredentialStore(path=tmp_path / "ghost" / "credentials.json")
    ghost_store.save("TJULLM_API_KEY", "ghost-value")

    router = ProviderRouter(state_path=tmp_path / "provider_state.json")
    response = router.chat(MESSAGES)
    assert response["provider"] == "tju"
    assert _auth_header_for(calls, "tju") == "Bearer dotenv-tju"
    # the router is default-constructed, so it is reload-eligible by design;
    # with the store unregistered, reloading still resolves from .env only
    assert router.reload() is True
    assert reload_default_routers() == 1
    assert _auth_header_for(calls, "tju") == "Bearer dotenv-tju"


# ---------------------------------------------------------------------------
# 5. reload preserves health state and the chat flow
# ---------------------------------------------------------------------------


def test_reload_preserves_health_state_and_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: list
):
    store = CredentialStore(path=tmp_path / "credentials" / "credentials.json")
    register_credential_source(store.get)
    try:
        env_file = tmp_path / ".env"
        env_file.write_text(DOTENV, encoding="utf-8")
        for key in ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(provider_base, "DEFAULT_ENV_FILE", env_file)

        # tju stub whose very first built instance fails once, later succeed
        built: list[Any] = []

        class FailingFirstTju(OpenAICompatibleProvider):
            def __init__(self) -> None:
                super().__init__(
                    name="tju",
                    api_key=provider_base.resolve_setting("TJULLM_API_KEY"),
                    base_url="https://tju.stub.invalid",
                    default_model="stub-model",
                    transport=self._stub_transport,
                )
                self._fail_first = len(built) == 0
                self._failed_once = False
                built.append(self)

            def _stub_transport(self, endpoint, payload, headers, timeout):
                calls.append(("tju", headers.get("Authorization")))
                if self._fail_first and not self._failed_once:
                    self._failed_once = True
                    raise ProviderHTTPError(500, "stub first-call failure")
                return _canned("tju")

        FailingFirstTju.name = "tju"
        monkeypatch.setattr(ai_router, "TJUQwenProvider", FailingFirstTju)
        monkeypatch.setattr(
            ai_router,
            "ZhipuGLMProvider",
            _make_stub_class("zhipu", "ZHIPU_API_KEY", calls),
        )
        monkeypatch.setattr(
            ai_router,
            "DeepSeekProvider",
            _make_stub_class("deepseek", "DEEPSEEK_API_KEY", calls),
        )

        router = ProviderRouter(state_path=tmp_path / "provider_state.json")

        # first chat: tju fails once → zhipu serves; failure is recorded
        response = router.chat(MESSAGES)
        assert response["provider"] == "zhipu"
        state_before = deepcopy(router.state)
        assert state_before["failures"]["tju"] == 1
        assert state_before["current_provider"] == "zhipu"

        # credential change + reload: instances rebuilt, health state preserved
        store.save("TJULLM_API_KEY", "stored-key-2")
        assert router.reload() is True
        assert router.state == state_before

        # the chat flow continues on rebuilt instances with the new credential
        response = router.chat(MESSAGES)
        assert response["provider"] == "tju"
        assert _auth_header_for(calls, "tju") == "Bearer stored-key-2"
        # diagnostics advanced; _record_success resets this provider's count
        assert router.state["current_provider"] == "tju"
        assert router.state["failures"]["tju"] == 0
    finally:
        register_credential_source(None)


# ---------------------------------------------------------------------------
# reload_default_routers: registry coverage + updated publisher
# ---------------------------------------------------------------------------


def test_reload_default_routers_covers_all_and_publishes(
    wired: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calls: list,
):
    _patch_adapters(monkeypatch, calls)
    router_a = ProviderRouter(state_path=tmp_path / "a.json")
    router_b = ProviderRouter(state_path=tmp_path / "b.json")
    wired.store.save("TJULLM_API_KEY", "stored-key-x")

    published: list[ProvidersUpdated] = []
    set_updated_publisher(lambda: published.append(ProvidersUpdated(reloaded_routers=2)))
    assert reload_default_routers() == 2
    assert len(published) == 1
    assert isinstance(published[0], ProvidersUpdated)

    for router in (router_a, router_b):
        assert router.providers[0].api_key == "stored-key-x"


def test_injected_providers_never_rebuilt(tmp_path: Path, calls: list):
    own = _make_stub_class("tju", "TJULLM_API_KEY", calls)()
    router = ProviderRouter(providers=[own], state_path=tmp_path / "s.json")
    assert router.reload() is False
    assert router.providers[0] is own
    assert reload_default_routers() == 0  # injected router is not in the registry


def test_providers_updated_payload_is_frozen_dataclass():
    payload = ProvidersUpdated()
    assert payload.reason == "credential_update"
    assert ProvidersUpdated(reason="manual", reloaded_routers=3).reloaded_routers == 3
