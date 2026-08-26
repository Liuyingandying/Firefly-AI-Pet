"""Standardized intermediate data models for Firefly history migration Stage 0.

This module is standalone by design: it imports neither ``core``, ``memory``,
nor ``character``, and performs no memory writes, bond updates, character
changes, or LLM analysis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    """Speaker role for one parsed message."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Turn:
    """One parsed message."""

    role: Role
    content: str
    ts: str | None = None
    seq: int = 0
    message_id: str | None = None
    content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "content": self.content,
            "ts": self.ts,
            "seq": self.seq,
            "message_id": self.message_id,
            "content_type": self.content_type,
        }


@dataclass(slots=True)
class Conversation:
    """One parsed conversation with an identifier and ordered turns."""

    id: str
    title: str | None = None
    turns: list[Turn] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "turns": [turn.to_dict() for turn in self.turns],
        }


@dataclass(slots=True)
class ParsedHistory:
    """Top-level intermediate output for one source file."""

    source_file: str
    source: str = "doubao"
    conversations: list[Conversation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "source": self.source,
            "source_file": self.source_file,
            "conversations": [
                conversation.to_dict() for conversation in self.conversations
            ],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


__all__ = ["Conversation", "ParsedHistory", "Role", "Turn"]
