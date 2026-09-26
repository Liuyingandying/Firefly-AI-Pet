"""User-configured custom OpenAI-compatible providers (credential-store backed).

Entries live in the credential store under the reserved key
``CUSTOM_PROVIDERS`` as one compact-JSON array — reusing the store's atomic
write, corruption quarantine and LOCALAPPDATA placement (keys never enter
the repository).  Each entry::

    {"id": "siliconflow", "name": "硅基流动", "base_url": "https://.../v1",
     "model": "Qwen/...", "api_key": "sk-...", "enabled": true}

The router consumes only ``enabled`` entries with a non-empty key/base/model
(``core.ai_router`` appends them after the built-in trio).  All functions
take the store explicitly so tests can inject a temp store.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

CUSTOM_PROVIDERS_KEY = "CUSTOM_PROVIDERS"

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_URL_PATTERN = re.compile(r"^https?://\S+$")

_lock = threading.RLock()


class CustomProviderError(ValueError):
    """Raised for invalid custom provider entries."""


def _store_get(store: Any) -> str | None:
    try:
        return store.get(CUSTOM_PROVIDERS_KEY)
    except Exception:
        return None


def _parse(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []  # a damaged payload degrades to an empty list, never raises
    if not isinstance(data, list):
        return []
    return [entry for entry in data if isinstance(entry, dict)]


def _validate_entry(entry: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(entry.get("id") or "").strip()
    if not _ID_PATTERN.match(provider_id):
        raise CustomProviderError("id 必须是小写字母/数字/短横线（1~64 字符）")
    name = str(entry.get("name") or "").strip()
    if not name:
        raise CustomProviderError("显示名称不能为空")
    base_url = str(entry.get("base_url") or "").strip()
    if not _URL_PATTERN.match(base_url):
        raise CustomProviderError("Base URL 必须以 http:// 或 https:// 开头")
    model = str(entry.get("model") or "").strip()
    if not model:
        raise CustomProviderError("模型名不能为空")
    api_key = str(entry.get("api_key") or "").strip()
    if "\n" in api_key or "\r" in api_key:
        raise CustomProviderError("API Key 不能包含换行")
    return {
        "id": provider_id,
        "name": name,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "enabled": bool(entry.get("enabled", True)),
    }


def list_custom_providers(store: Any) -> list[dict[str, Any]]:
    """All stored custom entries (defensive copy, secrets included)."""
    with _lock:
        return [dict(entry) for entry in _parse(_store_get(store))]


def enabled_custom_providers(store: Any) -> list[dict[str, Any]]:
    """Enabled entries only — what the router appends to its fallback chain."""
    return [entry for entry in list_custom_providers(store) if entry["enabled"]]


def get_custom_provider(store: Any, provider_id: str) -> dict[str, Any] | None:
    for entry in list_custom_providers(store):
        if entry["id"] == provider_id:
            return entry
    return None


def save_custom_provider(store: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """Validate + upsert one entry (by id).  Returns the stored entry."""
    clean = _validate_entry(entry)
    with _lock:
        entries = list_custom_providers(store)
        entries = [e for e in entries if e["id"] != clean["id"]]
        entries.append(clean)
        store.save(
            CUSTOM_PROVIDERS_KEY,
            json.dumps(entries, ensure_ascii=False, separators=(",", ":")),
        )
    return clean


def delete_custom_provider(store: Any, provider_id: str) -> bool:
    """Remove one entry by id.  Returns True when something was removed."""
    with _lock:
        entries = list_custom_providers(store)
        kept = [e for e in entries if e["id"] != provider_id]
        if len(kept) == len(entries):
            return False
        store.save(
            CUSTOM_PROVIDERS_KEY,
            json.dumps(kept, ensure_ascii=False, separators=(",", ":")),
        )
        return True
