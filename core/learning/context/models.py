"""LearningContext (Phase 2-LC).

A READ-ONLY projection of "where the learner is" inside the ACTIVE curriculum
of the currently active course. It is the ONLY thing the UI and the chat
prompt may consume for learning structure — no UI computes a chapter, and no
AI recommends a route.

Frozen rules:

- only an ACTIVE curriculum is read; a Draft is invisible here;
- ``current_chapter`` is derived from real learning facts (recent study
  session -> most recent concept -> its chapter link). When it cannot be
  determined it is ``None`` — never guessed;
- ``current_focus`` is the most recently studied concept;
- ``next_in_order`` is the deterministic successor on the curriculum's default
  path (or the route's first item when nothing has been studied yet). It is
  structural order, NOT a personalised recommendation;
- nothing in this module writes: no mastery, no concept, no activation, no
  session.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ContextSource(str, Enum):
    """Why the context has (or lacks) curriculum structure."""

    ACTIVE_CURRICULUM = "active_curriculum"
    NO_CURRICULUM = "no_curriculum"


@dataclass(frozen=True, slots=True)
class LearningContext:
    """Read-only snapshot of the learner's structural position."""

    course_id: str
    course_name: str = ""
    curriculum_id: str | None = None
    curriculum_title: str | None = None
    curriculum_version: int | None = None
    current_chapter: str | None = None
    current_focus: str | None = None
    next_in_order: str | None = None
    source: str = ContextSource.NO_CURRICULUM.value

    def __post_init__(self) -> None:
        object.__setattr__(self, "course_name", str(self.course_name or ""))
        for field in (
            "curriculum_id",
            "curriculum_title",
            "current_chapter",
            "current_focus",
            "next_in_order",
        ):
            value = getattr(self, field)
            object.__setattr__(self, field, str(value).strip() if value else None)

    @property
    def has_curriculum(self) -> bool:
        return self.curriculum_id is not None

    @property
    def has_structure(self) -> bool:
        """True when at least one structural position is known."""
        return any((self.current_chapter, self.current_focus, self.next_in_order))

    def structure_lines(self) -> tuple[str, ...]:
        """UI/prompt-safe lines: only non-empty fields, nothing invented."""
        lines: list[str] = []
        if self.curriculum_title:
            version = f" v{self.curriculum_version}" if self.curriculum_version else ""
            lines.append(f"课程结构：{self.curriculum_title}{version}")
        if self.current_chapter:
            lines.append(f"当前章节：{self.current_chapter}")
        if self.current_focus:
            lines.append(f"当前重点：{self.current_focus}")
        if self.next_in_order:
            lines.append(f"下一结构节点：{self.next_in_order}")
        return tuple(lines)

    def context_block(self) -> str | None:
        """Structure block for the chat prompt; None when there is nothing to
        say (no curriculum and no structural position)."""
        lines = self.structure_lines()
        if not lines:
            return None
        header = f"当前学习项目：{self.course_name}" if self.course_name else "学习模式"
        return "\n".join(
            [header, "课程结构（只读，是课程顺序而非个性化推荐）："]
            + [f"- {line}" for line in lines]
        )

    def resume_line(self) -> str | None:
        """One-line "where we are" summary for the entry flow."""
        parts: list[str] = []
        if self.current_chapter:
            parts.append(f"我们继续{self.current_chapter}")
        if self.current_focus:
            parts.append(f"上次学习：{self.current_focus}")
        if self.next_in_order:
            parts.append(f"按照课程顺序，下一节是：{self.next_in_order}")
        return "。".join(parts) + "。" if parts else None


__all__ = ["ContextSource", "LearningContext"]
