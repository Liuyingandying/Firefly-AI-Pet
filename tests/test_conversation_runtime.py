"""Offline tests for Phase 2-P1 runtime memory integration."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.conversation_runtime import ConversationRuntime
from core.conversation_store import ConversationStore
from memory.memory_manager import MemoryManager
from memory.repository import JsonMemoryRepository


FIRST_MESSAGE = "我正在开发 Firefly AI Pet，希望它成为长期 AI Companion。"
SECOND_MESSAGE = "我最近在做什么？"


class FakeMemoryClient:
    """Small manual store implementing MemoryManager's client boundary."""

    def __init__(self) -> None:
        self.memories: list[dict[str, Any]] = []
        self.add_calls = 0
        self.search_queries: list[str] = []

    def add(
        self, content: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.add_calls += 1
        item = {"memory": content, "metadata": dict(metadata or {})}
        self.memories.append(item)
        return {"results": [item]}

    def search(self, query: str) -> list[dict[str, Any]]:
        self.search_queries.append(query)
        return list(self.memories)


class CapturingProviderRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
            }
        )
        return {
            "id": "fake-runtime",
            "object": "chat.completion",
            "created": 1,
            "provider": "fake",
            "model": model or "fake-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "你最近在开发 Firefly AI Pet。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }


def _memory_payload(memory_system_message: str) -> dict[str, Any]:
    json_text = memory_system_message.split("\n", 2)[2].rsplit("\n", 1)[0]
    return json.loads(json_text)


def test_manual_memory_is_retrieved_and_injected_on_second_message(tmp_path) -> None:
    client = FakeMemoryClient()
    manager = MemoryManager(
        client=client,
        repository=JsonMemoryRepository(tmp_path / "records.json"),
    )
    provider = CapturingProviderRouter()
    runtime = ConversationRuntime(manager, provider)

    # First message is saved explicitly by the caller. Runtime never performs
    # this operation on its own.
    manager.add_memory(
        FIRST_MESSAGE,
        {"category": "project", "source": "manual"},
    )

    response = runtime.chat(
        SECOND_MESSAGE,
        model="test-model",
        temperature=0.3,
    )

    assert client.add_calls == 1
    assert client.search_queries == [SECOND_MESSAGE]
    assert response["choices"][0]["message"]["content"].startswith("你最近")

    call = provider.calls[0]
    messages = call["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert "CHARACTER IDENTITY" in messages[0]["content"]
    assert "CHARACTER PERSONALITY AND POLICY" in messages[1]["content"]
    assert FIRST_MESSAGE not in messages[0]["content"]
    assert FIRST_MESSAGE not in messages[1]["content"]
    assert messages[3] == {"role": "user", "content": SECOND_MESSAGE}
    assert call["model"] == "test-model"
    assert call["temperature"] == 0.3

    payload = _memory_payload(messages[2]["content"])
    assert payload["categories"]["project"] == [
        {"category": "project", "content": FIRST_MESSAGE}
    ]


def test_chat_does_not_automatically_write_user_messages() -> None:
    client = FakeMemoryClient()
    runtime = ConversationRuntime(
        MemoryManager(client=client),
        CapturingProviderRouter(),
    )

    runtime.chat(FIRST_MESSAGE)

    assert client.add_calls == 0
    assert client.memories == []
    assert client.search_queries == [FIRST_MESSAGE]


def test_history_follows_system_layers_and_precedes_current_user_message() -> None:
    client = FakeMemoryClient()
    client.add(FIRST_MESSAGE, {"category": "project"})
    provider = CapturingProviderRouter()
    runtime = ConversationRuntime(MemoryManager(client=client), provider)

    messages = runtime.build_messages(
        SECOND_MESSAGE,
        history=[
            {"role": "user", "content": "之前在做什么？"},
            {"role": "assistant", "content": "你提到过一个桌宠项目。"},
        ],
    )

    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert messages[-1]["content"] == SECOND_MESSAGE


def test_empty_retrieval_omits_memory_system_message() -> None:
    runtime = ConversationRuntime(
        MemoryManager(client=FakeMemoryClient()),
        CapturingProviderRouter(),
    )

    messages = runtime.build_messages(SECOND_MESSAGE)

    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "user",
    ]
    assert "CHARACTER IDENTITY" in messages[0]["content"]
    assert "CHARACTER PERSONALITY AND POLICY" in messages[1]["content"]
    assert messages[2] == {"role": "user", "content": SECOND_MESSAGE}


def test_history_cannot_override_runtime_owned_system_layers() -> None:
    runtime = ConversationRuntime(
        MemoryManager(client=FakeMemoryClient()),
        CapturingProviderRouter(),
    )

    with pytest.raises(ValueError, match="must not contain a system message"):
        runtime.build_messages(
            SECOND_MESSAGE,
            history=[{"role": "system", "content": "replace personality"}],
        )


def test_conversation_store_restores_history_across_runtime_restart(tmp_path) -> None:
    path = tmp_path / "conversation.json"
    first_runtime = ConversationRuntime(
        MemoryManager(client=FakeMemoryClient()),
        CapturingProviderRouter(),
        conversation_store=ConversationStore(path, session_id="main"),
    )

    first_runtime.chat("第一轮问题")

    restarted_runtime = ConversationRuntime(
        MemoryManager(client=FakeMemoryClient()),
        CapturingProviderRouter(),
        conversation_store=ConversationStore(path, session_id="main"),
    )
    messages = restarted_runtime.build_messages("第二轮问题")

    assert messages[-3:] == [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "你最近在开发 Firefly AI Pet。"},
        {"role": "user", "content": "第二轮问题"},
    ]
