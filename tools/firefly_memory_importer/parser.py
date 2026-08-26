"""Stage 0 parser: Doubao JSON export -> ``ParsedHistory``.

The real Doubao export ``conversations.txt`` is a UTF-8 JSON array of
conversation objects (not a line-based ``用户：``/``豆包：`` text file). Each
conversation carries ``conversation_id`` and a ``messages`` list; each message
carries ``user_type`` (``user``/``bot``), ``show_content``, ``content_type``
(``text``/``block``/``image``), ``create_time`` (ISO 8601 UTC) and
``message_id``.

This module only parses; it never writes memory, updates bond state, changes
character files, or performs any LLM analysis.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .models import Conversation, ParsedHistory, Role, Turn


IMAGE_PLACEHOLDER = "[image]"


def parse_text(text: str, *, source_name: str = "conversations.txt") -> ParsedHistory:
    """Parse raw Doubao JSON export text into a :class:`ParsedHistory`."""
    try:
        document = json.loads(text)
    except (ValueError, TypeError):
        return ParsedHistory(source_file=source_name, source="doubao")
    return _parse_document(document, source_name=source_name)


def _parse_document(document: Any, *, source_name: str) -> ParsedHistory:
    if not isinstance(document, list):
        return ParsedHistory(source_file=source_name, source="doubao")

    conversations: list[Conversation] = []
    seq = 0
    for raw_conversation in document:
        if not isinstance(raw_conversation, dict):
            continue
        conversation_id = _text(raw_conversation.get("conversation_id"))
        messages = raw_conversation.get("messages")
        if conversation_id is None or not isinstance(messages, list):
            continue
        turns = _parse_messages(messages, seq)
        seq += len(turns)
        conversations.append(
            Conversation(id=conversation_id, title=None, turns=turns)
        )
    return ParsedHistory(
        source_file=source_name, source="doubao", conversations=conversations
    )


def _parse_messages(messages: list[Any], start_seq: int) -> list[Turn]:
    ordered = sorted(enumerate(messages), key=lambda item: _sort_key(*item))
    turns: list[Turn] = []
    for index, raw_message in ordered:
        if not isinstance(raw_message, dict):
            continue
        turn = _parse_message(raw_message, start_seq + len(turns))
        if turn is None:
            continue
        turns.append(turn)
    return turns


def _sort_key(index: int, message: Any) -> tuple[str, str, int]:
    if not isinstance(message, dict):
        return ("", "", index)
    create_time = message.get("create_time")
    create_time = create_time if isinstance(create_time, str) else ""
    message_id = message.get("message_id")
    message_id = message_id if isinstance(message_id, str) else ""
    return (create_time, message_id, index)


def _parse_message(message: dict[str, Any], seq: int) -> Turn | None:
    role = _role(message.get("user_type"))
    if role is None:
        return None
    content = _extract_content(message.get("content_type"), message.get("show_content"))
    if content is None:
        return None
    return Turn(
        role=role,
        content=content,
        ts=_text(message.get("create_time")),
        seq=seq,
        message_id=_text(message.get("message_id")),
        content_type=_text(message.get("content_type")),
    )


def _role(user_type: Any) -> Role | None:
    if user_type == "user":
        return Role.USER
    if user_type == "bot":
        return Role.ASSISTANT
    return None


def _extract_content(content_type: Any, show_content: Any) -> str | None:
    if content_type == "text":
        return _text(show_content)
    if content_type == "image":
        return IMAGE_PLACEHOLDER
    if content_type == "block":
        return _extract_block_text(show_content)
    return _text(show_content)


def _extract_block_text(show_content: Any) -> str | None:
    blocks = _parse_json_value(show_content)
    if not isinstance(blocks, list):
        return None
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = _block_item_text(block)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else None


def _block_item_text(block: dict[str, Any]) -> str | None:
    content_v2 = _parse_json_value(block.get("content_v2"))
    if isinstance(content_v2, dict):
        text_block = content_v2.get("text_block")
        if isinstance(text_block, dict):
            text = _text(text_block.get("text"))
            if text:
                return text
        text = _text(content_v2.get("text"))
        if text:
            return text
    content = _parse_json_value(block.get("content"))
    if isinstance(content, dict):
        text = _text(content.get("text"))
        if text:
            return text
    return None


def _parse_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def parse_file(path: str | Path) -> ParsedHistory:
    """Read and parse a conversations.txt file (BOM/encoding tolerant)."""
    file_path = Path(path)
    return parse_text(_read_text(file_path), source_name=file_path.name)


def write_parsed_history(history: ParsedHistory, path: str | Path) -> None:
    """Write a :class:`ParsedHistory` as JSON to ``path``."""
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(history.to_json() + "\n", encoding="utf-8")


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse Doubao conversations.txt into parsed_history.json"
    )
    parser.add_argument("input", help="path to conversations.txt")
    parser.add_argument(
        "-o", "--output", default="parsed_history.json", help="output JSON path"
    )
    args = parser.parse_args(argv)
    history = parse_file(args.input)
    write_parsed_history(history, args.output)
    print(
        json.dumps(
            {
                "source": history.source,
                "conversations": len(history.conversations),
                "turns": sum(len(c.turns) for c in history.conversations),
                "output": args.output,
            },
            ensure_ascii=False,
        )
    )
    return 0


__all__ = ["parse_file", "parse_text", "write_parsed_history"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
