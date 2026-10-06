"""Per-request model selection. Owns no conversation, curriculum or learning state.

ProviderRouter owns cross-provider failover. Profiles describe model resources;
each request is bound to its provider/protocol adapter before any transport.
Other established protocols use explicit passthrough until an adapter is bound.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from providers.base import ProviderProtocolError, ProviderTimeoutError

PROFILE_PATH = Path(__file__).resolve().parents[1] / "model_profiles.json"
SELECTIONS = ("tju-stable", "tju-max", "auto")
_event_lock = threading.Lock()


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    model: str
    variant: str | None
    capabilities: tuple[str, ...]
    fallback: str | None
    protocol: str = "openai-chat-completions"
    display_name: str = ""
    verified_capabilities: tuple[str, ...] = ()
    evidence: str = ""


def emit_model_event(event: dict) -> None:
    """Only allowlisted routing metadata; no prompts, keys, headers or errors."""
    record = {k: event[k] for k in ("event", "request_id", "task", "profile", "model",
              "variant", "from_profile", "to_profile", "reason", "status_code", "provider", "response_model") if k in event}
    record["timestamp"] = datetime.now(timezone.utc).isoformat()
    # Vision and other protocol passthroughs must retain their no-disk contract.
    # Injectable sinks can observe them, but the default sink has no side effect.
    if record.get("profile") == "protocol-passthrough":
        return
    logging.getLogger("firefly.model_router").info("%s", json.dumps(record, ensure_ascii=False))
    try:
        from core.user_paths import get_user_data_paths
        path = get_user_data_paths().logs / "model_routing.jsonl"
        with _event_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def current_selection() -> str:
    from core.settings_manager import SettingsManager
    return SettingsManager().ai_model_profile


def validate_completion(result: dict) -> None:
    """Validate envelope before fallback ends; tool-only answers remain valid."""
    try:
        message = result["choices"][0]["message"]
        content, calls = message.get("content"), message.get("tool_calls")
        if isinstance(content, str) and content.strip():
            return
        if isinstance(calls, list) and calls and all(isinstance(c, dict) and
                isinstance(c.get("function"), dict) and c["function"].get("name") for c in calls):
            return
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    raise ProviderProtocolError("model returned no usable completion")


class ModelRouter:
    def __init__(self, *, profiles_path: Path = PROFILE_PATH,
                 selection: Callable[[], str] = current_selection,
                 event_sink: Callable[[dict], None] = emit_model_event,
                 clock: Callable[[], float] = time.monotonic):
        data = json.loads(profiles_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("unsupported model profile schema")
        self.profiles = {p["name"]: ModelProfile(p["name"], p["provider"], p["model"], p["variant"],
                                              tuple(p["capabilities"]), p["fallback"],
                                              p.get("protocol", "openai-chat-completions"),
                                              p.get("display_name", p["name"]),
                                              tuple(p.get("verified_capabilities", ())),
                                              p.get("evidence", "")) for p in data["profiles"]}
        if len(self.profiles) != len(data["profiles"]):
            raise ValueError("duplicate model profile")
        self.default, self.task_routes = data["default"], data["task_routes"]
        self.task_capabilities = data.get("task_capabilities", {})
        if self.default != "tju-stable" or self.default not in self.profiles:
            raise ValueError("stable must remain the default")
        for name, profile in self.profiles.items():
            if not profile.model or not profile.provider or not profile.protocol:
                raise ValueError("invalid model resource")
            if not set(profile.verified_capabilities) <= set(profile.capabilities):
                raise ValueError("verified capabilities must be declared")
            visited = set()
            while name:
                if name in visited or name not in self.profiles:
                    raise ValueError("invalid model fallback graph")
                candidate = self.profiles[name]
                if (candidate.provider, candidate.protocol) != (profile.provider, profile.protocol):
                    raise ValueError("cross-provider fallback belongs to the provider dispatcher")
                visited.add(name); name = candidate.fallback
        if not all(p in self.profiles for p in self.task_routes.values()):
            raise ValueError("invalid task profile")
        if not set(self.task_capabilities) <= set(self.task_routes):
            raise ValueError("capabilities refer to an unknown task")
        self.selection, self.event_sink, self.clock = selection, event_sink, clock

    def resolve(self, *, task="chat", profile=None, model=None) -> ModelProfile:
        if profile is not None and model is not None:
            raise ValueError("choose profile or model, not both")
        if task not in self.task_routes:
            raise ValueError("unknown model routing task")
        if model is not None:
            for candidate in self.profiles.values():
                if model in (candidate.name, candidate.model):
                    return candidate
            if not isinstance(model, str) or not model.strip():
                raise ValueError("model must be non-empty")
            # Existing explicitly configured models remain usable and are not renamed.
            return ModelProfile("explicit-model", "tju", model, None, ("text", "json", "tools", "vision"), None)
        choice = self.selection() if profile is None else profile
        if choice == "auto":
            choice = self.task_routes[task]
        if choice not in self.profiles:
            raise ValueError("unknown model profile")
        return self.profiles[choice]

    def request(self, payload: dict, send: Callable[[dict, float], Any], *, task="chat",
                profile=None, model=None, timeout=20.0, capabilities=("text",),
                provider="tju", protocol="openai-chat-completions",
                validator: Callable[[Any], None] = validate_completion):
        """Take one selection snapshot; reserve time for fallback; never mutate input."""
        selected = self.resolve(task=task, profile=profile, model=model)
        # A future profile must never send another provider's model or payload
        # through an already-bound TJU credential/endpoint callback.
        if (selected.provider, selected.protocol) != (provider, protocol):
            raise ValueError("model profile does not match the bound provider adapter")
        capabilities = set(capabilities) | set(self.task_capabilities.get(task, ()))
        budget = float(timeout)
        if not 0 < budget < float("inf"):
            raise ValueError("timeout must be finite and positive")
        request_id = uuid.uuid4().hex
        def event(name, **fields):
            self.event_sink({"event": name, "request_id": request_id, "task": task, **fields})
        attempts = [selected]
        while attempts[-1].fallback:
            attempts.append(self.profiles[attempts[-1].fallback])
        deadline = self.clock() + budget
        for index, candidate in enumerate(attempts):
            missing = set(capabilities) - set(candidate.capabilities)
            if missing:
                if index+1 == len(attempts):
                    raise ValueError("model profile does not support the request capability")
                event("model_switch_event", from_profile=candidate.name, to_profile=attempts[index+1].name,
                      reason="unsupported_capability")
                continue
            remaining = deadline-self.clock()
            if remaining <= 0:
                raise ProviderTimeoutError("model routing budget exhausted")
            body = deepcopy(payload)
            body["model"] = candidate.model
            body.pop("variant", None)
            if candidate.variant:
                body["variant"] = candidate.variant
            event("model_route_event", profile=candidate.name, provider=candidate.provider,
                  model=candidate.model, variant=candidate.variant)
            try:
                result = send(body, remaining/(len(attempts)-index))
                validator(result)
            except Exception as exc:
                if index+1 == len(attempts):
                    raise
                event("model_switch_event", from_profile=candidate.name,
                      to_profile=attempts[index+1].name, reason=type(exc).__name__,
                      status_code=getattr(exc, "status_code", None))
                continue
            event("model_response_event", profile=candidate.name, model=candidate.model,
                  response_model=result.get("model") if isinstance(result, dict) else None)
            return result

    def passthrough(self, send: Callable[[], Any], *, task: str, provider: str, model: str):
        """Observe a non-compatible protocol; preserve its established routing."""
        self.event_sink({"event": "model_route_event", "request_id": uuid.uuid4().hex,
                         "task": task, "provider": provider, "model": model,
                         "profile": "protocol-passthrough"})
        return send()


_default_router: ModelRouter | None = None
_default_lock = threading.Lock()


def get_model_router() -> ModelRouter:
    global _default_router
    with _default_lock:
        if _default_router is None:
            _default_router = ModelRouter()
        return _default_router


def routed_http_call(send, *args, task, provider, **kwargs):
    """Protocol-preserving boundary for vision and other specialized providers."""
    payload = kwargs.get("json", {})
    return get_model_router().passthrough(lambda: send(*args, **kwargs),
                                         task=task, provider=provider,
                                         model=payload.get("model", "configured-model"))
