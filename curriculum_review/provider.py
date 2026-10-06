"""Review-only binding to Firefly's existing TJU provider and Authoring keys."""

from __future__ import annotations

import os
import time
from typing import Any

from core.provider_manager import ProviderManager
from providers.base import ProviderError, ProviderTimeoutError, Transport, validate_messages
from providers.tju_qwen import DEFAULT_BASE_URL, TJUQwenProvider
from tools.teach_mcp_provider_launcher import KEY_SLOTS, build_authoring_env


AUTHORING_MODEL = "deepseek-v4-flash"  # book_pipeline._tjullm_chat default


def read_user_environment(names: tuple[str, ...] = KEY_SLOTS) -> dict[str, str]:
    """Read current Windows user variables absent from a stale parent process.

    Values stay in memory and are never included in diagnostics or artifacts.
    """
    try:
        import winreg
    except ImportError:
        return {}
    try:
        handle = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    except OSError:
        return {}
    found: dict[str, str] = {}
    with handle:
        for name in names:
            try:
                value, _ = winreg.QueryValueEx(handle, name)
            except OSError:
                continue
            if isinstance(value, str) and value.strip():
                found[name] = value.strip()
    return found


class AllTJUKeysFailed(ProviderError):
    """All configured key slots failed; exposes status metadata, never values."""

    def __init__(self, attempted_slots: tuple[int, ...], statuses: tuple[int | None, ...]):
        self.attempted_slots = attempted_slots
        self.statuses = statuses
        self.status_code = 429 if 429 in statuses else next(
            (status for status in reversed(statuses) if status is not None), None)
        super().__init__("all configured TJU key slots failed")


class RotatingTJUProvider:
    """Authoring-style 1→2→3 retry over the existing TJU chat client."""

    name = "tju"

    def __init__(self, entries: list[tuple[int, TJUQwenProvider]], *, explicit_model: str | None = None) -> None:
        if not entries:
            raise RuntimeError("TJU LLM has no configured key slots")
        self._entries = entries
        self._explicit_model = explicit_model
        self.default_model = entries[0][1].default_model
        self.endpoint = entries[0][1].endpoint
        self.attempted_slots: list[int] = []
        self.last_successful_slot: int | None = None

    @property
    def slot_count(self) -> int:
        return len(self._entries)

    def chat(self, messages: list[dict[str, Any]], model: str | None = None,
             temperature: float = 0.2, *, timeout: float | None = None,
             profile: str | None = None) -> dict:
        return self.complete({"messages": validate_messages(messages), "temperature": temperature, "stream": False},
                             task="review", model=model, profile=profile, timeout=timeout)

    def complete(self, payload: dict, *, task="review", model=None, profile=None, timeout=None) -> dict:
        """Route once around the established three-key transport, including tools."""
        from core.model_router import get_model_router, validate_completion
        def send(body, budget):
            return self._complete_slots(body, budget, validate_completion)
        explicit = model or (self._explicit_model if profile is None else None)
        return get_model_router().request(payload, send, task=task, model=explicit, profile=profile,
                                           timeout=timeout or self._entries[0][1].timeout,
                                           capabilities=("text", "tools") if payload.get("tools") else ("text",))

    def _complete_slots(self, payload, timeout, validate):
        self.attempted_slots = []
        self.last_successful_slot = None
        statuses: list[int | None] = []
        deadline = time.monotonic() + timeout
        for index, (slot, provider) in enumerate(self._entries):
            self.attempted_slots.append(slot)
            try:
                remaining = (deadline-time.monotonic()) / (len(self._entries)-index)
                if remaining <= 0:
                    raise ProviderTimeoutError("TJU key rotation budget exhausted")
                response = provider._transport(provider.endpoint, payload,
                    {"Content-Type": "application/json", "Authorization": f"Bearer {provider.api_key}"}, remaining)
                validate(response)
            except ProviderError as exc:
                statuses.append(getattr(exc, "status_code", None))
                continue
            self.last_successful_slot = slot
            return response
        raise AllTJUKeysFailed(tuple(self.attempted_slots), tuple(statuses))


def configured_tju_provider(
    *, timeout: float = 120.0,
    source_env: dict[str, str] | None = None,
    user_env: dict[str, str] | None = None,
    store: Any | None = None,
    dotenv: dict[str, str] | None = None,
    manager: ProviderManager | None = None,
    transport: Transport | None = None,
) -> tuple[RotatingTJUProvider, str]:
    """Use Provider Manager discovery and Authoring's established slot resolver.

    The standalone CLI's inherited environment may predate HKCU variable edits.
    Supply those missing *user* variables to the existing Authoring resolver,
    then construct one existing TJUQwenProvider per distinct key. No HTTP code
    or credential storage is implemented here.
    """
    source = dict(os.environ if source_env is None else source_env)
    user = read_user_environment() if user_env is None else user_env
    inherited = False
    for name in KEY_SLOTS:
        if not (source.get(name) or "").strip() and (user.get(name) or "").strip():
            source[name] = user[name]
            inherited = True
    status = (manager or ProviderManager(store=store)).get_provider_status("tju")
    if not status["configured"] and not any((source.get(name) or "").strip() for name in KEY_SLOTS):
        raise RuntimeError("TJU LLM is not configured in Provider Manager or user key slots")
    resolved = build_authoring_env(source_env=source, store=store, dotenv=dotenv)
    model = (source.get("TEACH_LLM_MODEL") or AUTHORING_MODEL).strip()
    base_url = (source.get("TEACH_LLM_URL") or DEFAULT_BASE_URL).strip()
    seen: set[str] = set()
    entries = []
    for slot, name in enumerate(KEY_SLOTS, 1):
        key = resolved[name]
        if key in seen:
            continue
        seen.add(key)
        entries.append((slot, TJUQwenProvider(api_key=key, base_url=base_url,
                                               model=model, timeout=timeout,
                                               transport=transport, routing_enabled=False)))
    source_label = "authoring_env:" + status["source"]
    if inherited:
        source_label += "+HKCU_user_environment"
    return RotatingTJUProvider(entries, explicit_model=(source.get("TEACH_LLM_MODEL") or "").strip() or None), source_label
