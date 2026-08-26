"""Offline tests for Firefly short-term conversation persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.conversation_store import ConversationStore, STORE_VERSION, Turn


def test_append_and_load_messages(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversation.json")

    store.append_turn("user", "你好", timestamp_ms=1_000)
    store.append_turn("assistant", "晚上好。", timestamp_ms=1_001)

    assert store.load_working_window() == [
        Turn("user", "你好", 1_000),
        Turn("assistant", "晚上好。", 1_001),
    ]
    assert store.window_size() == 2
    assert store.has_history() is True


def test_restart_restores_active_session_window(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    first = ConversationStore(path, session_id="main")
    first.append_exchange("记得这句话", "我会记住当前对话窗口。", timestamp_ms=2_000)

    restarted = ConversationStore(path, session_id="main")

    assert [turn.to_chat_message() for turn in restarted.load_recent_window()] == [
        {"role": "user", "content": "记得这句话"},
        {"role": "assistant", "content": "我会记住当前对话窗口。"},
    ]


def test_sessions_are_isolated_and_manageable(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversation.json", session_id="alpha")
    store.append_turn("user", "Alpha", timestamp_ms=3_000)
    store.create_session("empty")
    store.switch_session("beta")
    store.append_turn("user", "Beta", timestamp_ms=3_001)

    assert [turn.content for turn in store.load_working_window()] == ["Beta"]
    assert [
        turn.content for turn in store.load_working_window(session_id="alpha")
    ] == ["Alpha"]
    assert store.list_sessions() == ["alpha", "beta", "empty"]
    assert store.delete_session("alpha") is True
    assert store.delete_session("alpha") is False
    assert store.load_working_window(session_id="alpha") == []


def test_maximum_window_discards_oldest_messages(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversation.json", max_messages=3)
    for index in range(5):
        store.append_turn("user", f"message-{index}", timestamp_ms=4_000 + index)

    assert [turn.content for turn in store.load_working_window()] == [
        "message-2",
        "message-3",
        "message-4",
    ]


def test_summary_is_explicit_storage_only(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversation.json")
    store.append_turn("user", "消息", timestamp_ms=5_000)

    assert store.get_summary() is None
    store.set_summary("  外部生成的摘要  ")
    assert store.get_summary() == "外部生成的摘要"
    assert store.load_working_window()[0].content == "消息"
    store.set_summary(None)
    assert store.get_summary() is None


@pytest.mark.parametrize("content", ["broken", "[]", '{"version": 1}'])
def test_corrupt_top_level_data_degrades_to_empty(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "conversation.json"
    path.write_text(content, encoding="utf-8")

    store = ConversationStore(path)

    assert store.list_sessions() == []
    assert store.load_working_window() == []


def test_bad_turn_is_skipped_without_losing_session(tmp_path: Path) -> None:
    path = tmp_path / "conversation.json"
    path.write_text(
        json.dumps(
            {
                "version": STORE_VERSION,
                "sessions": {
                    "main": {
                        "turns": [
                            {"role": "user", "content": "有效", "ts": 6_000},
                            {"role": "system", "content": "非法", "ts": 6_001},
                            {"role": "assistant", "content": " ", "ts": 6_002},
                        ],
                        "summary": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = ConversationStore(path, session_id="main")

    assert store.load_working_window() == [Turn("user", "有效", 6_000)]
