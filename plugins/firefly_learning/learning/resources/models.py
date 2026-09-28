"""LearningResource (Phase 7B Learning Resource Layer).

A course-scoped REFERENCE to trusted learning material. It records and shows
sources — nothing else:

- it never parses or downloads anything;
- it never recommends anything (selection is always explicit or deterministic
  structure, never a ranking);
- it carries no mastery and cannot change learning state;
- deletion removes only the reference row.

Types cover the sources the app actually has: TEXTBOOK / PDF (教材与文档)、
VIDEO（B 站学习）、PAPER（手动登记——没有检索管线）、NOTE（手动条目）、
LINK（任意 URL）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResourceType(str, Enum):
    TEXTBOOK = "textbook"
    PDF = "pdf"
    VIDEO = "video"
    PAPER = "paper"
    NOTE = "note"
    LINK = "link"


class ResourceOrigin(str, Enum):
    """Who registered the reference (provenance, not a decision)."""

    USER_EXPLICIT = "user_explicit"
    TEXTBOOK_IMPORT = "textbook_import"
    SYSTEM_GENERATED = "system_generated"


def _type_value(value: Any) -> str:
    if isinstance(value, ResourceType):
        return value.value
    text = str(value or "").strip().lower()
    for member in ResourceType:
        if text in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid resource type: {value!r}")


def _origin_value(value: Any) -> str:
    if isinstance(value, ResourceOrigin):
        return value.value
    text = str(value or "").strip().lower()
    for member in ResourceOrigin:
        if text in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid resource origin: {value!r}")


@dataclass(frozen=True, slots=True)
class LearningResource:
    """One trusted learning-material reference (immutable)."""

    id: str
    course_id: str
    resource_type: str
    title: str
    source: str = ""
    locator: str = ""
    concept_id: str | None = None
    chapter_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        for field_name in ("id", "course_id", "resource_type", "title", "created_at"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise ValueError(f"LearningResource.{field_name} is required")
            object.__setattr__(self, field_name, value)
        object.__setattr__(
            self, "resource_type", _type_value(self.resource_type)
        )
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(self, "locator", str(self.locator or "").strip())
        for field_name in ("concept_id", "chapter_id"):
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, str(value).strip() if value else None
            )
        object.__setattr__(
            self, "metadata", dict(self.metadata) if self.metadata else {}
        )

    @property
    def type(self) -> ResourceType:
        return ResourceType(self.resource_type)

    @property
    def is_bound_to_concept(self) -> bool:
        return self.concept_id is not None

    @property
    def is_bound_to_chapter(self) -> bool:
        return self.chapter_id is not None

    def label(self) -> str:
        """One-line human label used by the UI (icon + title + locator)."""
        icons = {
            ResourceType.TEXTBOOK.value: "📚",
            ResourceType.PDF.value: "📄",
            ResourceType.VIDEO.value: "🎬",
            ResourceType.PAPER.value: "📄",
            ResourceType.NOTE.value: "📝",
            ResourceType.LINK.value: "🔗",
        }
        icon = icons.get(self.resource_type, "🔗")
        locator = f"（{self.locator}）" if self.locator else ""
        return f"{icon} {self.title}{locator}"


__all__ = ["LearningResource", "ResourceOrigin", "ResourceType"]
