"""Typed companion configuration (pydantic-settings).

Configuration is loaded from an optional ``config/companion.json``. There is no
hand-rolled parsing here — ``pydantic-settings`` provides the strong typing and
validation, and env vars may override via the ``FIREFLY_`` prefix.

The Firefly privacy red-line filter is deliberately absent from this schema: it
is a hard, always-on guard in :mod:`memory.write_guards` and cannot be disabled
by any configuration value.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from memory.records import MemoryCategory, WritePolicy


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "companion.json"


class ConversationSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_messages: int = Field(default=40, ge=1, le=1000)


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    search_top_k: int = Field(default=5, ge=1, le=100)
    search_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    write_policy: WritePolicy = WritePolicy.EXPLICIT_ONLY
    dedup_enabled: bool = True
    dedup_similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)


class SuggestionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    auto_extract_enabled: bool = False
    max_candidates_per_turn: int = Field(default=3, ge=0, le=10)
    semantic_dedup_enabled: bool = True
    semantic_dedup_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # P5A-3: Limited auto-approve for explicit remember requests
    explicit_auto_approve_enabled: bool = False
    explicit_auto_approve_min_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    explicit_auto_approve_allowed_categories: list[str] = Field(
        default_factory=lambda: [MemoryCategory.PREFERENCE.value, MemoryCategory.PROJECT.value]
    )


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    memory_guard_enabled: bool = False


class CompanionConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FIREFLY_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    conversation: ConversationSettings = ConversationSettings()
    memory: MemorySettings = MemorySettings()
    suggestion: SuggestionSettings = SuggestionSettings()
    security: SecuritySettings = SecuritySettings()


def load_companion_config(path: Path | str | None = None) -> CompanionConfig:
    """Load config, degrading to safe defaults on any error.

    A missing file, corrupt JSON, or invalid value never raises: it falls back
    to the documented defaults so the companion always boots.
    """
    source = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw: dict = {}
    try:
        parsed = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            raw = parsed
    except (FileNotFoundError, OSError, ValueError, TypeError):
        raw = {}
    try:
        return CompanionConfig(**raw)
    except Exception:
        return CompanionConfig()


__all__ = [
    "CompanionConfig",
    "ConversationSettings",
    "DEFAULT_CONFIG_PATH",
    "MemorySettings",
    "SecuritySettings",
    "SuggestionSettings",
    "load_companion_config",
]
