"""Learning entry presentation models (Phase 2-UX).

Pure, widget-free view models for the learning entry experience:

    build_entry_card(controller)     -> welcome + [继续 X] / [选择其他课程] / [新建]
    build_project_cards(controller)  -> one card per project ("当前课程结构 / 最近学习 / [进入]")
    build_status_card(context)       -> chapter / focus / next, FROM LearningContext ONLY

These builders never compute learning structure themselves and never write:
the status card is a straight field mapping of a :class:`LearningContext`, and
the entry/project data comes from read-only controller snapshots. A UI widget
renders these objects; it never derives a chapter or a "next step" on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Action ids the console wires to buttons (single source, no string drift).
ACTION_CONTINUE = "continue_last"
ACTION_CHOOSE_OTHER = "choose_other"
ACTION_CREATE = "create_project"
ACTION_ENTER = "enter_project"
ACTION_IMPORT_TEXTBOOK = "import_textbook"   # Phase 7A: PDF → CurriculumDraft


@dataclass(frozen=True, slots=True)
class LearningEntryCard:
    """The welcome state shown right after entering learning mode.

    It only OFFERS the remembered project — nothing is activated by showing
    this card.
    """

    greeting: str
    last_course_id: str | None = None
    last_course_name: str | None = None
    course_count: int = 0
    actions: tuple[tuple[str, str], ...] = ()  # (action_id, label)

    @property
    def has_last_course(self) -> bool:
        return self.last_course_id is not None

    def lines(self) -> tuple[str, ...]:
        lines = ["📘 学习模式", "", self.greeting]
        if self.last_course_name:
            lines += ["", "上次学习：", self.last_course_name, "", "继续之前的学习吗？"]
        elif self.course_count:
            lines += ["", "选择要继续的学习项目。"]
        else:
            lines += ["", "还没有学习项目，先创建一个吧。"]
        return tuple(lines)

    def render(self) -> str:
        return "\n".join(self.lines())


@dataclass(frozen=True, slots=True)
class LearningProjectCard:
    """One project in the picker (LearningProjectPicker)."""

    course_id: str
    name: str
    curriculum_title: str | None = None
    curriculum_version: int | None = None
    recent: str | None = None
    status: str = ""
    action_label: str = "进入"

    @property
    def structure_line(self) -> str:
        if not self.curriculum_title:
            return "还没有课程结构"
        version = f" v{self.curriculum_version}" if self.curriculum_version else ""
        return f"{self.curriculum_title}{version}"

    @property
    def recent_line(self) -> str:
        return self.recent or "还没有学习记录"

    def title_line(self) -> str:
        return f"📘 {self.name}"


@dataclass(frozen=True, slots=True)
class LearningStatusCard:
    """Compact companion status: chapter / focus / next / action.

    Every field is copied verbatim from a LearningContext (and, when supplied,
    the Phase 4 recommendation); ``visible`` is False when there is no context
    or no structural position to show.
    """

    course_name: str = ""
    chapter: str = ""
    focus: str = ""
    next_in_order: str = ""
    action: str = ""
    resources: tuple = ()          # Phase 7B: LearningResource objects (display only)
    source: str = ""
    visible: bool = False

    @classmethod
    def from_context(
        cls,
        context: Any | None,
        recommendation: Any | None = None,
        resources: Any | None = None,
    ) -> "LearningStatusCard":
        """Pure mapping — no computation, no guessing.

        ``recommendation`` is the Phase 4 decision; its action is shown verbatim
        (FREE_CHAT is omitted — there is nothing to execute).
        """
        if context is None:
            # no learning context: the card stays invisible, but the mapped
            # resource labels are kept for callers that want them.
            return cls(resources=tuple(resources or ()))
        chapter = getattr(context, "current_chapter", None) or ""
        focus = getattr(context, "current_focus", None) or ""
        next_in_order = getattr(context, "next_in_order", None) or ""
        action = ""
        if recommendation is not None:
            decided = getattr(recommendation, "action", None) or ""
            if decided and decided != "free_chat":
                action = str(decided).upper()
        return cls(
            course_name=getattr(context, "course_name", "") or "",
            chapter=chapter,
            focus=focus,
            next_in_order=next_in_order,
            action=action,
            resources=tuple(resources or ()),
            source=getattr(context, "source", "") or "",
            visible=bool(chapter or focus or next_in_order or action),
        )

    def rows(self) -> tuple[tuple[str, str], ...]:
        """(caption, value) rows for a compact card; only non-empty rows."""
        rows = []
        if self.chapter:
            rows.append(("章节", self.chapter))
        if self.focus:
            rows.append(("重点", self.focus))
        if self.next_in_order:
            rows.append(("下一步", self.next_in_order))
        if self.action:
            rows.append(("动作", self.action))
        return tuple(rows)

    def resource_labels(self) -> tuple[str, ...]:
        """Verbatim one-line labels for the bound resources (no computing)."""
        labels: list[str] = []
        for resource in self.resources:
            label = getattr(resource, "label", None)
            if callable(label):
                text = label()
                if text:
                    labels.append(text)
        return tuple(labels)


# ---------------------------------------------------------------------------
# builders (fed by read-only controller snapshots)
# ---------------------------------------------------------------------------


def build_entry_card(snapshot: dict) -> LearningEntryCard:
    """Build the welcome card from ``controller.entry_snapshot()``."""
    last = snapshot.get("last_course")
    last_id = last[0] if isinstance(last, (tuple, list)) and len(last) == 2 else None
    last_name = last[1] if isinstance(last, (tuple, list)) and len(last) == 2 else None
    owner = (snapshot.get("owner_name") or "").strip()
    greeting = f"欢迎回来，{owner}。" if owner else "欢迎回来。"

    actions: list[tuple[str, str]] = []
    if last_id is not None:
        actions.append((ACTION_CONTINUE, f"继续 {last_name}"))
    actions.append((ACTION_CHOOSE_OTHER, "选择其他课程"))
    create_label = (
        "创建第一个课程"
        if last_id is None and not int(snapshot.get("course_count") or 0)
        else "新建学习项目"
    )
    actions.append((ACTION_CREATE, create_label))
    actions.append((ACTION_IMPORT_TEXTBOOK, "导入教材"))
    return LearningEntryCard(
        greeting=greeting,
        last_course_id=last_id,
        last_course_name=last_name,
        course_count=int(snapshot.get("course_count") or 0),
        actions=tuple(actions),
    )


def build_project_cards(summaries: list[dict]) -> tuple[LearningProjectCard, ...]:
    """Build picker cards from ``controller.project_summaries()``."""
    cards = []
    for summary in summaries or ():
        cards.append(
            LearningProjectCard(
                course_id=summary.get("course_id", ""),
                name=summary.get("name", ""),
                curriculum_title=summary.get("curriculum_title"),
                curriculum_version=summary.get("curriculum_version"),
                recent=summary.get("recent"),
                status=summary.get("status", ""),
                action_label=summary.get("action", "进入"),
            )
        )
    return tuple(cards)


def build_status_card(
    context: Any | None,
    recommendation: Any | None = None,
    resources: Any | None = None,
) -> LearningStatusCard:
    """Build the companion status card from a LearningContext (or None).

    ``recommendation`` (Phase 4 decision) and ``resources`` (Phase 7B resource
    references) are optional; both are shown verbatim when present.
    """
    return LearningStatusCard.from_context(
        context, recommendation=recommendation, resources=resources
    )


__all__ = [
    "ACTION_CHOOSE_OTHER",
    "ACTION_CONTINUE",
    "ACTION_CREATE",
    "ACTION_ENTER",
    "ACTION_IMPORT_TEXTBOOK",
    "LearningEntryCard",
    "LearningProjectCard",
    "LearningStatusCard",
    "build_entry_card",
    "build_project_cards",
    "build_status_card",
]
