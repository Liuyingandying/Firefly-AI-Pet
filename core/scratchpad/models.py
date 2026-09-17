"""Scratchpad v1 data model — the user-driven temporary inbox.

Scratchpad is NOT long-term memory, NOT experience memory, and NOT
conversation history.  It is a drop-in temporary clipboard the user hands
to Firefly on purpose.  Nothing here promotes into Memory; there is no
experience conversion — that door is deliberately not built in v1.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

STORE_VERSION = 1


class ScratchpadItemType(str, Enum):
    TEXT = "text"
    IMAGE = "image"


class ScratchpadSource(str, Enum):
    DRAG_DROP = "drag_drop"
    MANUAL = "manual"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class ScratchpadItem:
    """One entry in the temporary notebook (metadata only, never image bytes)."""

    id: str
    created_at: int                      # epoch milliseconds (UTC), timezone-free convention
    item_type: ScratchpadItemType
    text: str = ""
    asset_path: str = ""                 # relative to the scratchpad root, e.g. "assets/<uuid>.png"
    source: ScratchpadSource = ScratchpadSource.MANUAL

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def now_ms() -> int:
        return _now_ms()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "id": self.id,
            "created_at": self.created_at,
            "item_type": self.item_type.value,
            "text": self.text,
            "asset_path": self.asset_path,
            "source": self.source.value,
        }

    @staticmethod
    def from_dict(data: MappingLike) -> "ScratchpadItem | None":
        """Tolerant parse: a malformed entry is skipped, never fatal."""
        try:
            item_type = ScratchpadItemType(str(data["item_type"]))
            source = ScratchpadSource(str(data.get("source", ScratchpadSource.MANUAL.value)))
            item_id = str(data["id"]).strip()
            created_at = int(data["created_at"])
            if not item_id or created_at < 0:
                return None
            if item_type is ScratchpadItemType.TEXT:
                text = str(data.get("text", ""))
                if not text.strip():
                    return None
                return ScratchpadItem(item_id, created_at, item_type, text=text, source=source)
            asset_path = str(data.get("asset_path", "")).strip()
            if not asset_path:
                return None
            return ScratchpadItem(item_id, created_at, item_type,
                                  asset_path=asset_path, source=source)
        except Exception:  # noqa: BLE001 - corrupt entries are skipped by design
            return None

    def with_text(self, text: str) -> "ScratchpadItem":
        return replace(self, text=text)


class MappingLike(dict):
    """Typing shim so from_dict accepts plain mappings."""

    pass
