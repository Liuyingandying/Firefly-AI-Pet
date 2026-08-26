"""CompanionConfig loading + wiring tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.companion_config import (
    CompanionConfig,
    SecuritySettings,
    load_companion_config,
)
from core.companion_runtime import CompanionRuntime
from core.conversation_store import ConversationStore
from memory.records import WritePolicy
from memory.repository import JsonMemoryRepository
from memory.service import MemoryService
from memory.suggestion.suggestion_service import SuggestionService
from memory.write_guards import RedLineViolationError, check_red_line


def _write_json(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "companion.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- loading ---------------------------------------------------------------


def test_defaults_without_file(tmp_path: Path) -> None:
    cfg = load_companion_config(tmp_path / "does-not-exist.json")
    assert cfg.conversation.max_messages == 40
    assert cfg.memory.search_top_k == 5
    assert cfg.memory.write_policy is WritePolicy.EXPLICIT_ONLY
    assert cfg.suggestion.enabled is False
    assert cfg.security.memory_guard_enabled is False


def test_corrupt_config_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "companion.json"
    path.write_text("{ this is not json", encoding="utf-8")
    cfg = load_companion_config(path)
    assert cfg.conversation.max_messages == 40
    assert cfg.memory.write_policy is WritePolicy.EXPLICIT_ONLY


def test_partial_config_merges_defaults(tmp_path: Path) -> None:
    path = _write_json(tmp_path, {"memory": {"search_top_k": 7}})
    cfg = load_companion_config(path)
    assert cfg.memory.search_top_k == 7
    assert cfg.memory.write_policy is WritePolicy.EXPLICIT_ONLY  # default kept
    assert cfg.conversation.max_messages == 40  # default kept
    assert cfg.suggestion.enabled is False


def test_invalid_write_policy_falls_back(tmp_path: Path) -> None:
    path = _write_json(tmp_path, {"memory": {"write_policy": "bogus"}})
    cfg = load_companion_config(path)
    assert cfg.memory.write_policy is WritePolicy.EXPLICIT_ONLY


def test_full_config_applied(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path,
        {
            "conversation": {"max_messages": 12},
            "memory": {"search_top_k": 9, "write_policy": "ask"},
            "suggestion": {"enabled": True},
            "security": {"memory_guard_enabled": True},
        },
    )
    cfg = load_companion_config(path)
    assert cfg.conversation.max_messages == 12
    assert cfg.memory.search_top_k == 9
    assert cfg.memory.write_policy is WritePolicy.ASK
    assert cfg.suggestion.enabled is True
    assert cfg.security.memory_guard_enabled is True


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = _write_json(tmp_path, {"bogus_key": {"x": 1}})
    cfg = load_companion_config(path)
    assert cfg.conversation.max_messages == 40


# --- privacy red line is never configurable --------------------------------


def test_red_line_cannot_be_disabled_by_config(tmp_path: Path) -> None:
    cfg = load_companion_config(
        _write_json(tmp_path, {"security": {"memory_guard_enabled": False}})
    )
    assert cfg.security.memory_guard_enabled is False
    # Firefly red-line lives in write_guards and is independent of config.
    with pytest.raises(RedLineViolationError):
        check_red_line("sk-abcdef1234567890abcdef")


def test_config_schema_has_no_red_line_toggle() -> None:
    assert set(CompanionConfig.model_fields) == {
        "conversation",
        "memory",
        "suggestion",
        "security",
    }
    assert set(SecuritySettings.model_fields) == {"memory_guard_enabled"}


# --- wiring ----------------------------------------------------------------


class RecordingAdapter:
    def __init__(self) -> None:
        self.limits: list[int] = []

    def add(self, text, metadata=None):
        return "vec-1"

    def search(self, query, *, limit=5):
        self.limits.append(limit)
        return []

    def delete(self, vector_id):
        return True


def test_search_top_k_reaches_adapter(tmp_path: Path) -> None:
    repo = JsonMemoryRepository(tmp_path / "records.json")
    adapter = RecordingAdapter()
    service = MemoryService(repo, adapter, search_top_k=7)

    service.search("q")

    assert adapter.limits == [7]


def test_max_messages_reaches_conversation_store() -> None:
    cfg = CompanionConfig(conversation={"max_messages": 7})
    store = ConversationStore(max_messages=cfg.conversation.max_messages)
    assert store.max_messages == 7


def test_suggestion_enabled_gates_detection() -> None:
    off = SuggestionService(None, enabled=False)
    on = SuggestionService(None, enabled=True)

    assert off.detect("我毕业了") == []
    assert len(on.detect("我毕业了")) == 1


def test_runtime_wires_config_into_store_and_service() -> None:
    cfg = CompanionConfig(
        conversation={"max_messages": 7},
        memory={"search_top_k": 9, "write_policy": WritePolicy.ASK},
    )
    runtime = CompanionRuntime(config=cfg)

    assert runtime.conversation_store.max_messages == 7
    assert runtime.memory_service.search_top_k == 9
    assert runtime.memory_service.write_policy is WritePolicy.ASK
