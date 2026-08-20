"""Offline tests for the TJU -> Zhipu -> DeepSeek fallback router."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.ai_router import AllProvidersFailedError, ProviderRouter
from providers.base import (
    BaseProvider,
    ProviderHTTPError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from providers.deepseek import DeepSeekProvider
from providers.tju_qwen import TJUQwenProvider
from providers.zhipu_glm import ZhipuGLMProvider


MESSAGES = [{"role": "user", "content": "解释相位裕度"}]


def _completion(provider: str, model: str, content: str) -> dict[str, Any]:
    return {
        "id": f"fake-{provider}",
        "object": "chat.completion",
        "created": 1,
        "provider": provider,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


class FakeProvider(BaseProvider):
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
        elapsed: float = 0.0,
        clock: "FakeClock | None" = None,
    ) -> None:
        self.name = name
        self.calls = calls
        self.response = response
        self.error = error
        self.elapsed = elapsed
        self.clock = clock
        self.timeouts: list[float] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self.calls.append(self.name)
        assert timeout is not None
        self.timeouts.append(timeout)
        assert messages == MESSAGES
        assert temperature == 0.2
        if self.clock is not None:
            self.clock.advance(min(self.elapsed, timeout))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _saved_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_default_router_order_and_state_schema(tmp_path: Path) -> None:
    router = ProviderRouter(state_path=tmp_path / "provider_state.json")

    assert [provider.name for provider in router.providers] == [
        "tju",
        "zhipu",
        "deepseek",
    ]
    assert [provider.timeout for provider in router.providers] == [20.0] * 3
    assert router.provider_timeout == 20.0
    assert router.timeout_budget == 60.0
    assert router.state == {
        "current_provider": "tju",
        "failures": {"tju": 0, "zhipu": 0, "deepseek": 0},
    }


def test_tju_failure_falls_back_to_glm(tmp_path: Path) -> None:
    calls: list[str] = []
    state_path = tmp_path / "provider_state.json"
    router = ProviderRouter(
        [
            FakeProvider(
                "tju",
                calls,
                error=ProviderHTTPError(503, "TJU unavailable"),
            ),
            FakeProvider(
                "zhipu",
                calls,
                response=_completion("zhipu", "glm-4.7-flash", "GLM answer"),
            ),
            FakeProvider(
                "deepseek",
                calls,
                response=_completion("deepseek", "deepseek-chat", "unused"),
            ),
        ],
        state_path=state_path,
    )

    result = router.chat(MESSAGES)

    assert calls == ["tju", "zhipu"]
    assert result["provider"] == "zhipu"
    assert result["choices"][0]["message"]["content"] == "GLM answer"
    assert _saved_state(state_path) == {
        "current_provider": "zhipu",
        "failures": {"tju": 1, "zhipu": 0, "deepseek": 0},
    }


def test_tju_and_glm_failure_fall_back_to_deepseek(tmp_path: Path) -> None:
    calls: list[str] = []
    state_path = tmp_path / "provider_state.json"
    router = ProviderRouter(
        [
            FakeProvider(
                "tju",
                calls,
                error=ProviderUnavailableError("TJU unavailable"),
            ),
            FakeProvider(
                "zhipu",
                calls,
                error=ProviderHTTPError(429, "GLM rate limited"),
            ),
            FakeProvider(
                "deepseek",
                calls,
                response=_completion(
                    "deepseek", "deepseek-chat", "DeepSeek answer"
                ),
            ),
        ],
        state_path=state_path,
    )

    result = router.chat(MESSAGES)

    assert calls == ["tju", "zhipu", "deepseek"]
    assert result["provider"] == "deepseek"
    assert result["choices"][0]["message"]["content"] == "DeepSeek answer"
    assert _saved_state(state_path) == {
        "current_provider": "deepseek",
        "failures": {"tju": 1, "zhipu": 1, "deepseek": 0},
    }


def test_all_three_provider_failures_are_recorded(tmp_path: Path) -> None:
    calls: list[str] = []
    state_path = tmp_path / "provider_state.json"
    router = ProviderRouter(
        [
            FakeProvider(
                "tju",
                calls,
                error=ProviderUnavailableError("TJU unavailable"),
            ),
            FakeProvider(
                "zhipu",
                calls,
                error=ProviderHTTPError(429, "GLM rate limited"),
            ),
            FakeProvider(
                "deepseek",
                calls,
                error=ProviderHTTPError(503, "DeepSeek unavailable"),
            ),
        ],
        state_path=state_path,
    )

    with pytest.raises(AllProvidersFailedError) as captured:
        router.chat(MESSAGES)

    assert calls == ["tju", "zhipu", "deepseek"]
    assert [name for name, _ in captured.value.failures] == [
        "tju",
        "zhipu",
        "deepseek",
    ]
    assert _saved_state(state_path) == {
        "current_provider": "tju",
        "failures": {"tju": 1, "zhipu": 1, "deepseek": 1},
    }


def test_deepseek_openai_compatible_defaults() -> None:
    captured: dict[str, Any] = {}

    def transport(endpoint, payload, headers, timeout):
        captured.update({
            "endpoint": endpoint,
            "payload": payload,
            "headers": headers,
            "timeout": timeout,
        })
        return _completion("deepseek", "deepseek-chat", "adapter answer")

    provider = DeepSeekProvider(api_key="deepseek-test-key", transport=transport)
    result = provider.chat(MESSAGES, temperature=0.2)

    assert captured["endpoint"] == "https://api.deepseek.com/chat/completions"
    assert captured["payload"]["model"] == "deepseek-chat"
    assert captured["payload"]["messages"] == MESSAGES
    assert captured["payload"]["temperature"] == 0.2
    assert captured["payload"]["stream"] is False
    assert captured["headers"]["Authorization"] == "Bearer deepseek-test-key"
    assert captured["timeout"] == 20.0
    assert result["provider"] == "deepseek"


def test_all_three_providers_timeout_within_total_budget(tmp_path: Path) -> None:
    calls: list[str] = []
    clock = FakeClock()
    providers = [
        FakeProvider(
            name,
            calls,
            error=ProviderTimeoutError(f"{name} timed out"),
            elapsed=20.0,
            clock=clock,
        )
        for name in ("tju", "zhipu", "deepseek")
    ]
    router = ProviderRouter(
        providers,
        state_path=tmp_path / "provider_state.json",
        clock=clock,
    )

    with pytest.raises(AllProvidersFailedError):
        router.chat(MESSAGES)

    assert calls == ["tju", "zhipu", "deepseek"]
    assert clock.now <= 60.0
    assert [provider.timeouts for provider in providers] == [[20.0], [20.0], [20.0]]


def test_first_two_timeouts_then_third_provider_succeeds(tmp_path: Path) -> None:
    calls: list[str] = []
    clock = FakeClock()
    providers = [
        FakeProvider(
            "tju",
            calls,
            error=ProviderTimeoutError("tju timed out"),
            elapsed=20.0,
            clock=clock,
        ),
        FakeProvider(
            "zhipu",
            calls,
            error=ProviderTimeoutError("zhipu timed out"),
            elapsed=20.0,
            clock=clock,
        ),
        FakeProvider(
            "deepseek",
            calls,
            response=_completion("deepseek", "deepseek-chat", "fallback succeeded"),
            elapsed=5.0,
            clock=clock,
        ),
    ]
    router = ProviderRouter(
        providers,
        state_path=tmp_path / "provider_state.json",
        clock=clock,
    )

    result = router.chat(MESSAGES)

    assert result["provider"] == "deepseek"
    assert calls == ["tju", "zhipu", "deepseek"]
    assert clock.now == 45.0


def test_remaining_total_budget_caps_later_provider_timeout(tmp_path: Path) -> None:
    calls: list[str] = []
    clock = FakeClock()
    providers = [
        FakeProvider(
            "tju",
            calls,
            error=ProviderTimeoutError("tju timed out"),
            elapsed=20.0,
            clock=clock,
        ),
        FakeProvider(
            "zhipu",
            calls,
            error=ProviderTimeoutError("zhipu timed out"),
            elapsed=20.0,
            clock=clock,
        ),
        FakeProvider(
            "deepseek",
            calls,
            error=ProviderTimeoutError("deepseek timed out"),
            elapsed=10.0,
            clock=clock,
        ),
    ]
    router = ProviderRouter(
        providers,
        state_path=tmp_path / "provider_state.json",
        timeout_budget=50.0,
        clock=clock,
    )

    with pytest.raises(AllProvidersFailedError):
        router.chat(MESSAGES)

    assert clock.now == 50.0
    assert providers[2].timeouts == [10.0]


def test_api_keys_load_from_env_file_with_environment_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TJULLM_API_KEY=tju-from-file\n"
        "ZHIPU_API_KEY=zhipu-from-file\n"
        "DEEPSEEK_API_KEY=deepseek-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TJULLM_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert TJUQwenProvider(env_file=env_file).api_key == "tju-from-file"
    assert ZhipuGLMProvider(env_file=env_file).api_key == "zhipu-from-file"
    assert DeepSeekProvider(env_file=env_file).api_key == "deepseek-from-file"

    monkeypatch.setenv("TJULLM_API_KEY", "tju-from-process")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-from-process")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-from-process")
    assert TJUQwenProvider(env_file=env_file).api_key == "tju-from-process"
    assert ZhipuGLMProvider(env_file=env_file).api_key == "zhipu-from-process"
    assert DeepSeekProvider(env_file=env_file).api_key == "deepseek-from-process"
