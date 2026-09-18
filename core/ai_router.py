"""Provider fallback router for ordinary OpenAI-compatible chat requests.

The router is deliberately independent from the existing Claude workflow and
Codex agent launcher. Every call starts with the fixed product priority
``tju -> zhipu -> deepseek``; ``current_provider`` records the most recent
success for diagnostics, rather than making fallback sticky across future
calls.
"""

from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from weakref import WeakSet

from providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    BaseProvider,
    ChatCompletion,
    ProviderTimeoutError,
    validate_messages,
)
from providers.deepseek import DeepSeekProvider
from providers.tju_qwen import TJUQwenProvider
from providers.zhipu_glm import ZhipuGLMProvider


STATE_FILE = Path(__file__).resolve().parent / "provider_state.json"
PROVIDER_ORDER = ("tju", "zhipu", "deepseek")
DEFAULT_TIMEOUT_BUDGET_SECONDS = 60.0
DEFAULT_STATE = {
    "current_provider": "tju",
    "failures": {
        "tju": 0,
        "zhipu": 0,
        "deepseek": 0,
    },
}


@dataclass(frozen=True, slots=True)
class ProvidersUpdated:
    """Payload for the ``providers.updated`` runtime event (Phase 2).

    Emitted after at least one default-constructed router rebuilt its
    provider instances (typically following a credential store change).
    Carries no credential material.
    """

    reason: str = "credential_update"
    reloaded_routers: int = 0


# Live default-constructed routers; weak so nothing here extends lifetimes.
_live_routers: WeakSet["ProviderRouter"] = WeakSet()
_live_routers_lock = threading.Lock()
_updated_publisher: Callable[[], None] | None = None


def set_updated_publisher(publisher: Callable[[], None] | None) -> None:
    """Register the callback invoked after default routers reload.

    The composition root (app.py) wires this to the RuntimeBus
    ``providers.updated`` event; ``core.ai_router`` itself stays Qt-free.
    """
    global _updated_publisher
    _updated_publisher = publisher


def _notify_updated() -> None:
    publisher = _updated_publisher
    if publisher is not None:
        publisher()


class AllProvidersFailedError(Exception):
    """Every configured provider failed for a chat request."""

    def __init__(self, failures: Iterable[tuple[str, Exception]]) -> None:
        self.failures = tuple(failures)
        providers = ", ".join(name for name, _ in self.failures) or "none"
        super().__init__(f"all AI providers failed: {providers}")


class ProviderRouter:
    """Try chat providers in fixed priority order and persist health state."""

    def __init__(
        self,
        providers: Iterable[BaseProvider] | None = None,
        *,
        state_path: Path | str = STATE_FILE,
        timeout_budget: float = DEFAULT_TIMEOUT_BUDGET_SECONDS,
        provider_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._providers_injected = providers is not None
        if providers is not None:
            configured = list(providers)
        else:
            configured = list(self._build_default_providers())
        if not self._providers_injected:
            with _live_routers_lock:
                _live_routers.add(self)
        names = [str(provider.name) for provider in configured]
        if len(names) != len(set(names)):
            raise ValueError("provider names must be unique")
        self.providers = tuple(configured)
        requested_budget = float(timeout_budget)
        requested_provider_timeout = float(provider_timeout)
        if not requested_budget > 0:
            raise ValueError("timeout_budget must be positive")
        if not requested_provider_timeout > 0:
            raise ValueError("provider_timeout must be positive")
        # These are hard safety ceilings, even if a caller supplies larger values.
        self.timeout_budget = min(
            requested_budget,
            DEFAULT_TIMEOUT_BUDGET_SECONDS,
        )
        self.provider_timeout = min(
            requested_provider_timeout,
            DEFAULT_TIMEOUT_SECONDS,
        )
        self._clock = clock
        self.state_path = Path(state_path)
        self._state_lock = threading.RLock()
        self._state = self._load_state()

    @property
    def state(self) -> dict[str, Any]:
        """Return a detached snapshot of current router state."""
        with self._state_lock:
            return deepcopy(self._state)

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
    ) -> ChatCompletion:
        """Return the first successful provider response.

        Message validation happens before routing so invalid caller input does
        not incorrectly count as a provider outage. Provider errors, including
        unexpected adapter failures, are isolated and advance to the fallback.
        Each attempt receives the smaller of the per-provider timeout and the
        request's remaining global budget.
        """
        normalized_messages = validate_messages(messages)
        failures: list[tuple[str, Exception]] = []
        deadline = self._clock() + self.timeout_budget

        for provider in self.providers:
            provider_name = str(provider.name)
            remaining = deadline - self._clock()
            if remaining <= 0:
                failures.append((
                    provider_name,
                    ProviderTimeoutError("router timeout budget is exhausted"),
                ))
                self._record_failure(provider_name)
                break
            request_timeout = min(self.provider_timeout, remaining)
            try:
                response = provider.chat(
                    normalized_messages,
                    model=model,
                    temperature=temperature,
                    timeout=request_timeout,
                )
            except Exception as exc:  # provider isolation is the router's boundary
                failures.append((provider_name, exc))
                self._record_failure(provider_name)
                continue

            self._record_success(provider_name)
            return response

        raise AllProvidersFailedError(failures)

    def reload(self) -> bool:
        """Rebuild the default provider instances under the state lock.

        Provider Manager Phase 2: re-runs adapter construction so credentials
        resolved through ``providers.base.resolve_setting`` (process env >
        credential store > .env) are picked up without a restart.  Health
        state (``current_provider`` / failure counts) is preserved, and
        in-flight calls keep finishing on their existing adapter instances.

        Returns ``True`` when this router owns the default provider set and
        was rebuilt; ``False`` for routers constructed with an explicit
        provider list (caller-owned, never rebuilt here).
        """
        with self._state_lock:
            if self._providers_injected:
                return False
            self.providers = tuple(self._build_default_providers())
        return True

    @staticmethod
    def _build_default_providers() -> tuple[BaseProvider, ...]:
        """Fresh default adapter set; credentials resolve at construction."""
        return (
            TJUQwenProvider(),
            ZhipuGLMProvider(),
            DeepSeekProvider(),
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return deepcopy(DEFAULT_STATE)
        if not isinstance(raw, dict):
            return deepcopy(DEFAULT_STATE)

        current = raw.get("current_provider")
        if current not in PROVIDER_ORDER:
            current = DEFAULT_STATE["current_provider"]

        raw_failures = raw.get("failures")
        if not isinstance(raw_failures, dict):
            raw_failures = {}
        failures: dict[str, int] = {}
        for name in PROVIDER_ORDER:
            value = raw_failures.get(name, 0)
            failures[name] = value if isinstance(value, int) and value >= 0 else 0
        return {"current_provider": current, "failures": failures}

    def _record_failure(self, provider_name: str) -> None:
        with self._state_lock:
            if provider_name in self._state["failures"]:
                self._state["failures"][provider_name] += 1
            self._save_state_locked()

    def _record_success(self, provider_name: str) -> None:
        with self._state_lock:
            if provider_name in PROVIDER_ORDER:
                self._state["current_provider"] = provider_name
            if provider_name in self._state["failures"]:
                self._state["failures"][provider_name] = 0
            self._save_state_locked()

    def _save_state_locked(self) -> None:
        """Best-effort atomic state write; routing remains available on I/O errors."""
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


_default_router: ProviderRouter | None = None
_default_router_lock = threading.Lock()


def _get_default_router() -> ProviderRouter:
    global _default_router
    if _default_router is None:
        with _default_router_lock:
            if _default_router is None:
                _default_router = ProviderRouter()
    return _default_router


def reload_default_routers() -> int:
    """Reload every live default-constructed router (Phase 2 hot update).

    Called after a credential store change so all default routers — the
    module-level chat router and any ``ProviderRouter()`` instances created
    by consumers such as companion runtime — pick up the new credentials
    without a restart.  Health state is preserved per router; the registered
    updated-publisher (wired to the RuntimeBus ``providers.updated`` event by
    the composition root) fires once when at least one router was rebuilt.

    Returns the number of routers actually rebuilt.
    """
    with _live_routers_lock:
        routers = list(_live_routers)
    reloaded = sum(1 for router in routers if router.reload())
    if reloaded:
        _notify_updated()
    return reloaded


def chat(
    messages: list[dict[str, Any]],
    model: str | None = None,
    temperature: float = 0.2,
) -> ChatCompletion:
    """Chat through TJU Qwen, then Zhipu GLM, then DeepSeek Chat."""
    return _get_default_router().chat(
        messages,
        model=model,
        temperature=temperature,
    )
