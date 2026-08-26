"""Narrow semantic-index adapter around local Mem0 OSS.

MemoryRecord JSON remains authoritative. This module owns only vector index
operations and semantic hits; it has no permission, lifecycle, category, or
audit behavior.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STORAGE_DIR = PROJECT_DIR / "runtime" / "memory"
DEFAULT_MODEL_CACHE_DIR = DEFAULT_STORAGE_DIR / "models"
DEFAULT_USER_ID = "firefly-local-user"
DEFAULT_COLLECTION = "firefly_memories"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_EMBEDDING_DIMS = 512


class Mem0AdapterError(RuntimeError):
    """Raised when the local semantic index cannot complete an operation."""


class MemoryDependencyError(Mem0AdapterError):
    """Raised when the optional local Mem0 dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class Hit:
    """Backend-independent semantic search result."""

    vector_id: str
    text: str
    metadata: dict[str, Any]
    score: float


BackendFactory = Callable[[dict[str, Any]], Any]


class Mem0Adapter:
    """Local embedding/index boundary with lazy Mem0 initialization."""

    def __init__(
        self,
        storage_dir: str | Path | None = None,
        *,
        user_id: str = DEFAULT_USER_ID,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self.storage_dir = Path(storage_dir or DEFAULT_STORAGE_DIR).resolve()
        self.user_id = _required_text(user_id, "user_id")
        self._backend_factory = backend_factory or _default_backend_factory
        self._backend: Any | None = None

    def add(self, text: str, metadata: Mapping[str, Any] | None = None) -> str:
        """Index raw text and metadata, always bypassing Mem0 LLM inference."""
        normalized_text = _required_text(text, "text")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        try:
            response = self._client().add(
                normalized_text,
                user_id=self.user_id,
                metadata=dict(metadata or {}),
                infer=False,
            )
        except Mem0AdapterError:
            raise
        except Exception as exc:
            raise Mem0AdapterError("failed to add semantic index entry") from exc
        vector_id = _extract_added_id(response)
        if vector_id is None:
            raise Mem0AdapterError("Mem0 add response did not contain a vector ID")
        return vector_id

    def search(self, query: str, *, limit: int = 5) -> list[Hit]:
        """Return normalized semantic hits ordered by descending score."""
        normalized_query = _required_text(query, "query")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        try:
            response = self._client().search(
                normalized_query,
                filters={"user_id": self.user_id},
                top_k=limit,
            )
        except Mem0AdapterError:
            raise
        except Exception as exc:
            raise Mem0AdapterError("semantic search failed") from exc
        hits = [_to_hit(item) for item in _result_items(response)]
        normalized = [hit for hit in hits if hit is not None]
        return sorted(normalized, key=lambda hit: hit.score, reverse=True)[:limit]

    def delete(self, vector_id: str) -> bool:
        """Delete one vector only; authoritative MemoryRecords are untouched."""
        normalized_id = _required_text(vector_id, "vector_id")
        try:
            response = self._client().delete(memory_id=normalized_id)
        except Mem0AdapterError:
            raise
        except ValueError as exc:
            if "not found" in str(exc).lower():
                return False
            raise Mem0AdapterError("failed to delete semantic index entry") from exc
        except Exception as exc:
            raise Mem0AdapterError("failed to delete semantic index entry") from exc
        if response is False:
            return False
        if isinstance(response, Mapping):
            for key in ("deleted", "success"):
                if key in response:
                    return bool(response[key])
        return True

    def clear(self) -> int:
        """Clear this user's vector index and return the prior entry count."""
        client = self._client()
        try:
            existing = client.get_all(
                filters={"user_id": self.user_id},
                top_k=1_000_000,
            )
            count = len(_result_items(existing))
            client.delete_all(user_id=self.user_id)
            return count
        except Mem0AdapterError:
            raise
        except Exception as exc:
            raise Mem0AdapterError("failed to clear semantic index") from exc

    def health(self) -> bool:
        """Return whether the lazily initialized local backend is available."""
        try:
            self._client()
        except Mem0AdapterError:
            return False
        return True

    def _client(self) -> Any:
        if self._backend is not None:
            return self._backend

        # These are deliberately set immediately before the only possible
        # Mem0 import (inside _default_backend_factory).
        os.environ["MEM0_TELEMETRY"] = "false"
        os.environ["MEM0_DIR"] = str(self.storage_dir / "mem0-home")
        os.environ["FASTEMBED_CACHE_PATH"] = str(self.storage_dir / "models")
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._backend = self._backend_factory(self._config())
        except MemoryDependencyError:
            raise
        except Exception as exc:
            raise Mem0AdapterError("failed to initialize local Mem0 backend") from exc
        if self._backend is None:
            raise Mem0AdapterError("backend factory returned no backend")
        return self._backend

    def _config(self) -> dict[str, Any]:
        return {
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "local-inference-disabled",
                    "api_key": "local-inference-disabled",
                    "openai_base_url": "http://127.0.0.1:9/v1",
                },
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": DEFAULT_COLLECTION,
                    "embedding_model_dims": DEFAULT_EMBEDDING_DIMS,
                    "path": str(self.storage_dir / "qdrant"),
                    "on_disk": True,
                },
            },
            "embedder": {
                "provider": "fastembed",
                "config": {
                    "model": DEFAULT_EMBEDDING_MODEL,
                    "embedding_dims": DEFAULT_EMBEDDING_DIMS,
                },
            },
            "history_db_path": str(self.storage_dir / "history.db"),
        }


def _default_backend_factory(config: dict[str, Any]) -> Any:
    """Import Mem0 only after the adapter has installed privacy settings."""
    try:
        from mem0 import Memory
    except ImportError as exc:
        raise MemoryDependencyError(
            "Mem0 is not installed. Install dependencies from requirements.txt."
        ) from exc
    return Memory.from_config(config)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _result_items(response: Any) -> list[Any]:
    if isinstance(response, Mapping):
        items = response.get("results", [])
    else:
        items = response
    return list(items) if isinstance(items, (list, tuple)) else []


def _extract_added_id(response: Any) -> str | None:
    candidates = _result_items(response)
    if not candidates and isinstance(response, Mapping):
        candidates = [response]
    elif isinstance(response, str):
        candidates = [response]
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
        if isinstance(item, Mapping):
            for key in ("id", "vector_id", "memory_id"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _to_hit(item: Any) -> Hit | None:
    if not isinstance(item, Mapping):
        return None
    vector_id = next(
        (
            item.get(key).strip()
            for key in ("id", "vector_id", "memory_id")
            if isinstance(item.get(key), str) and item.get(key).strip()
        ),
        None,
    )
    text = next(
        (
            item.get(key).strip()
            for key in ("memory", "text")
            if isinstance(item.get(key), str) and item.get(key).strip()
        ),
        None,
    )
    if vector_id is None or text is None:
        return None
    metadata = item.get("metadata")
    normalized_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    score = item.get("score", 0.0)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        normalized_score = 0.0
    else:
        normalized_score = float(score)
        if not math.isfinite(normalized_score):
            normalized_score = 0.0
    return Hit(vector_id, text, normalized_metadata, normalized_score)


__all__ = [
    "DEFAULT_COLLECTION",
    "DEFAULT_EMBEDDING_DIMS",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_MODEL_CACHE_DIR",
    "DEFAULT_STORAGE_DIR",
    "DEFAULT_USER_ID",
    "Hit",
    "Mem0Adapter",
    "Mem0AdapterError",
    "MemoryDependencyError",
]
