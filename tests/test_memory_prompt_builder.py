"""Offline tests for the Phase 2-P0 memory prompt isolation layer."""

from __future__ import annotations

import json

import pytest

from memory.memory_prompt_builder import (
    MEMORY_CATEGORIES,
    MemoryPromptBuilder,
)


class FakeMemorySearcher:
    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search_memory(self, query: str) -> list[dict]:
        self.queries.append(query)
        return self.results


def test_build_keeps_personality_and_memory_in_separate_system_messages() -> None:
    searcher = FakeMemorySearcher(
        [
            {
                "memory": "用户喜欢简洁回答",
                "metadata": {"category": "preference"},
            }
        ]
    )
    builder = MemoryPromptBuilder(searcher)

    layers = builder.build("你是流萤。", "怎样回答我？")
    messages = layers.to_system_messages()

    assert searcher.queries == ["怎样回答我？"]
    assert layers.personality_prompt == "你是流萤。"
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": "你是流萤。"}
    assert "用户喜欢简洁回答" not in messages[0]["content"]
    assert "用户喜欢简洁回答" in messages[1]["content"]
    assert "你是流萤" not in messages[1]["content"]


def test_memory_context_has_stable_six_category_schema() -> None:
    memories = [
        {"memory": "用户住在上海", "metadata": {"category": "user_fact"}},
        {"memory": "偏好 Markdown", "category": "preference"},
        {"memory": "正在开发 Firefly", "metadata": {"category": "project"}},
        {
            "memory": "一起完成了 Phase 1",
            "metadata": {"category": "shared_experience"},
        },
        {"memory": "彼此逐渐熟悉", "metadata": {"category": "relationship"}},
        {"memory": "今天有些疲惫", "metadata": {"category": "emotion"}},
    ]

    context = MemoryPromptBuilder.format_memories(memories)
    structured = context.to_dict()

    assert tuple(structured["categories"]) == MEMORY_CATEGORIES
    assert structured["type"] == "memory_context"
    assert structured["version"] == 1
    for category, memory in zip(MEMORY_CATEGORIES, memories):
        assert structured["categories"][category] == [
            {"category": category, "content": memory["memory"]}
        ]


def test_prompt_block_contains_parseable_structured_json_and_boundaries() -> None:
    context = MemoryPromptBuilder.format_memories(
        [{"text": "用户偏好本地处理", "metadata": {"category": "preference"}}]
    )

    prompt = context.to_prompt()
    json_text = prompt.split("\n", 2)[2].rsplit("\n", 1)[0]

    assert prompt.startswith("--- BEGIN MEMORY CONTEXT ---")
    assert prompt.endswith("--- END MEMORY CONTEXT ---")
    assert json.loads(json_text) == context.to_dict()


def test_invalid_legacy_and_empty_records_are_not_injected() -> None:
    context = MemoryPromptBuilder.format_memories(
        [
            {"memory": "旧的无分类记忆"},
            {"memory": "未知分类", "metadata": {"category": "other"}},
            {"memory": "   ", "metadata": {"category": "emotion"}},
            {"metadata": {"category": "project"}},
        ]
    )

    assert context.is_empty
    assert context.to_prompt() == ""
    assert all(not values for values in context.to_dict()["categories"].values())


def test_empty_context_does_not_add_a_memory_system_message() -> None:
    layers = MemoryPromptBuilder(FakeMemorySearcher([])).build(
        "你是流萤。", "你好"
    )

    assert layers.to_system_messages() == [
        {"role": "system", "content": "你是流萤。"}
    ]


@pytest.mark.parametrize(
    ("personality", "query", "field"),
    [("", "你好", "personality_prompt"), ("你是流萤。", "  ", "query")],
)
def test_build_rejects_empty_required_text(
    personality: str, query: str, field: str
) -> None:
    builder = MemoryPromptBuilder(FakeMemorySearcher([]))

    with pytest.raises(ValueError, match=field):
        builder.build(personality, query)
