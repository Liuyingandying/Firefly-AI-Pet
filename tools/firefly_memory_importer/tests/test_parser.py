"""Tests for Stage 0 Doubao JSON export parser."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tools.firefly_memory_importer.models import Role
from tools.firefly_memory_importer.parser import parse_file, parse_text


def _msg(
    user_type: str,
    show_content: str,
    *,
    content_type: str = "text",
    create_time: str = "2024-01-01T00:00:00Z",
    message_id: str = "1",
) -> dict:
    return {
        "user_id": "u1",
        "message_id": message_id,
        "conversation_id": "c1",
        "user_type": user_type,
        "content_type": content_type,
        "create_time": create_time,
        "bot_id": "b1",
        "update_time": create_time,
        "show_content": show_content,
    }


def _conv(conversation_id: str, messages: list[dict]) -> dict:
    return {
        "conversation_id": conversation_id,
        "conversation_name": "",
        "bot_id": "b1",
        "bot_name": "",
        "messages": messages,
        "user_id": "u1",
    }


def _text(*conversations: dict) -> str:
    return json.dumps(list(conversations), ensure_ascii=False)


def _block_show_content(text: str, *, with_v2: bool = True) -> str:
    block: dict = {}
    if with_v2:
        block["content_v2"] = json.dumps({"text_block": {"text": text}})
    block["content"] = json.dumps({"text": text})
    return json.dumps([block])


def test_parses_single_user_assistant_pair() -> None:
    history = parse_text(
        _text(
            _conv(
                "c1",
                [
                    _msg("user", "你好"),
                    _msg("bot", "你好呀！"),
                ],
            )
        )
    )

    assert len(history.conversations) == 1
    conv = history.conversations[0]
    assert conv.id == "c1"
    assert [t.role for t in conv.turns] == [Role.USER, Role.ASSISTANT]
    assert [t.content for t in conv.turns] == ["你好", "你好呀！"]


def test_multi_turn_order_preserved() -> None:
    history = parse_text(
        _text(
            _conv(
                "c1",
                [
                    _msg("user", "第一句", message_id="1"),
                    _msg("bot", "回复一", message_id="2"),
                    _msg("user", "第二句", message_id="3"),
                    _msg("bot", "回复二", message_id="4"),
                ],
            )
        )
    )

    conv = history.conversations[0]
    assert [t.content for t in conv.turns] == ["第一句", "回复一", "第二句", "回复二"]
    assert [t.seq for t in conv.turns] == [0, 1, 2, 3]


def test_multiple_conversations() -> None:
    history = parse_text(
        _text(
            _conv("c1", [_msg("user", "a"), _msg("bot", "b")]),
            _conv("c2", [_msg("user", "c"), _msg("bot", "d")]),
        )
    )

    assert len(history.conversations) == 2
    assert [c.id for c in history.conversations] == ["c1", "c2"]
    assert [len(c.turns) for c in history.conversations] == [2, 2]


def test_user_type_maps_to_role() -> None:
    history = parse_text(
        _text(_conv("c1", [_msg("user", "x"), _msg("bot", "y"), _msg("other", "z")]))
    )

    conv = history.conversations[0]
    # unknown user_type is skipped
    assert [t.role for t in conv.turns] == [Role.USER, Role.ASSISTANT]


def test_text_content_read_directly() -> None:
    history = parse_text(_text(_conv("c1", [_msg("user", "  内容  ")])))

    assert history.conversations[0].turns[0].content == "内容"


def test_block_content_prefers_content_v2() -> None:
    history = parse_text(
        _text(
            _conv(
                "c1",
                [_msg("bot", _block_show_content("来自 content_v2"), content_type="block")],
            )
        )
    )

    assert history.conversations[0].turns[0].content == "来自 content_v2"


def test_block_content_falls_back_to_content() -> None:
    history = parse_text(
        _text(
            _conv(
                "c1",
                [
                    _msg(
                        "bot",
                        _block_show_content("来自 content", with_v2=False),
                        content_type="block",
                    )
                ],
            )
        )
    )

    assert history.conversations[0].turns[0].content == "来自 content"


def test_image_content_placeholder() -> None:
    history = parse_text(
        _text(
            _conv(
                "c1",
                [_msg("user", '{"image_list":[]}', content_type="image")],
            )
        )
    )

    turn = history.conversations[0].turns[0]
    assert turn.content == "[image]"
    assert turn.content_type == "image"


def test_messages_sorted_by_create_time() -> None:
    history = parse_text(
        _text(
            _conv(
                "c1",
                [
                    _msg("bot", "第二条", create_time="2024-01-01T00:00:02Z"),
                    _msg("user", "第一条", create_time="2024-01-01T00:00:01Z"),
                ],
            )
        )
    )

    assert [t.content for t in history.conversations[0].turns] == ["第一条", "第二条"]


def test_tiebreak_uses_message_id_then_index() -> None:
    same_time = "2024-01-01T00:00:00Z"
    history = parse_text(
        _text(
            _conv(
                "c1",
                [
                    _msg("user", "A", create_time=same_time, message_id="2"),
                    _msg("user", "B", create_time=same_time, message_id="1"),
                    _msg("user", "C", create_time=same_time, message_id="3"),
                ],
            )
        )
    )

    assert [t.content for t in history.conversations[0].turns] == ["B", "A", "C"]


def test_metadata_preserved() -> None:
    history = parse_text(
        _text(
            _conv(
                "c1",
                [
                    _msg(
                        "user",
                        "你好",
                        create_time="2024-10-29T12:09:29Z",
                        message_id="m-123",
                    )
                ],
            )
        )
    )

    turn = history.conversations[0].turns[0]
    assert history.source == "doubao"
    assert turn.message_id == "m-123"
    assert turn.content_type == "text"
    assert turn.ts == "2024-10-29T12:09:29Z"


def test_empty_and_whitespace_input() -> None:
    assert parse_text("").conversations == []
    assert parse_text("   \n  ").conversations == []


def test_invalid_json_yields_empty() -> None:
    assert parse_text("not-json {").conversations == []
    assert parse_text("[]").conversations == []
    assert parse_text("{}").conversations == []


def test_parse_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "conversations.txt"
    path.write_text(
        _text(_conv("c1", [_msg("user", "hi"), _msg("bot", "hello")])),
        encoding="utf-8",
    )

    history = parse_file(path)

    assert history.source_file == "conversations.txt"
    assert [t.content for t in history.conversations[0].turns] == ["hi", "hello"]


_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_ZIP_PATH = _DATA_DIR / "89e3821900eb4847af207291bdb2678b-2.zip"
_TXT_PATH = _DATA_DIR / "conversations.txt"


def _real_export_text() -> str | None:
    if _ZIP_PATH.exists():
        with zipfile.ZipFile(_ZIP_PATH) as archive:
            return archive.read("conversations.txt").decode("utf-8-sig")
    if _TXT_PATH.exists():
        return _TXT_PATH.read_text(encoding="utf-8-sig")
    return None


def test_real_doubao_export_parses_34_conversations() -> None:
    text = _real_export_text()
    if text is None:
        pytest.skip("real Doubao export not present")

    history = parse_text(text)

    assert history.source == "doubao"
    assert len(history.conversations) == 34
    assert sum(len(c.turns) for c in history.conversations) == 4371
