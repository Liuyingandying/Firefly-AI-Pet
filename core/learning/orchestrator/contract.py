"""LearningResponseContract (Phase 9B-1).

The bridge that makes Phase 4 Decision + Phase 5 Action actually shape the
final answer:

    build_response_contract(learning_result) → contract
    contract.prompt_block()                  → constraint lines in the prompt
    contract.is_satisfied(response)          → deterministic validation

The contract ONLY constrains — it never generates an answer, never judges
mastery, and never calls a provider. Language style remains the LLM's job;
the anchors (course name / next structural node) are checked verbatim so a
non-answering response ("好的") can never silently pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.learning.orchestrator.models import LearningLoopResult


#: Structural requirements per decided action (frozen by the phase spec's
#: example: EXPLAIN → 课程定位 / 第一学习任务 / 下一步交互).
# Phase 9C: the bootstrap contract elements (a fresh course's first turn).
BOOTSTRAP_REQUIRED_ELEMENTS: tuple[str, ...] = (
    "课程定位", "开始章节", "第一学习任务", "下一步交互",
)

REQUIRED_ELEMENTS_BY_ACTION: dict[str, tuple[str, ...]] = {
    "explain": ("课程定位", "第一学习任务", "下一步交互"),
    "explain_recall": ("课程定位", "回忆提问", "下一步交互"),
    "practice": ("练习任务", "作答引导", "下一步交互"),
    "review": ("课程定位", "复习回顾", "记忆检查"),
    "transfer": ("课程定位", "应用场景", "迁移说明"),
    "move_next": ("课程定位", "下一结构节点", "承接说明"),
    "free_chat": (),
}


@dataclass(frozen=True, slots=True)
class LearningResponseContract:
    """What the final answer MUST reflect (structure, not style)."""

    course: str = ""
    chapter: str = ""
    focus: str = ""
    action: str = ""
    next_step: str = ""
    required_elements: tuple[str, ...] = ()
    anchors: tuple[str, ...] = ()
    # Phase 9C: the bootstrap's own 课程定位/开始章节/第一学习任务 lines
    bootstrap_lines: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("course", "chapter", "focus", "action", "next_step"):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, str(value or "").strip())
        object.__setattr__(
            self, "required_elements",
            tuple(_clean(e) for e in self.required_elements if _clean(e)),
        )
        object.__setattr__(
            self, "anchors",
            tuple(_clean(a) for a in self.anchors if _clean(a)),
        )

    # -- prompt projection -------------------------------------------------

    def prompt_block(self) -> str | None:
        """Constraint lines injected into the turn's system context.

        Phase 9C.1 — Bootstrap Response Priority: when the bootstrap exists
        (fresh course, ``current_focus`` still None), the block LEADS with a
        priority directive requiring the course-start information to open the
        answer, before any companion tone. The data is verbatim from the
        bootstrap (nothing hardcoded).
        """
        if self.bootstrap_lines:
            priority: list[str] = []
            if self.course:
                priority.append(f"课程：{self.course}")
            priority.append(
                "本回合必须优先输出以下学习启动信息（置于回答开头，"
                "之后再以陪伴语气继续；不要只做闲聊鼓励）："
            )
            priority.extend(f"- {line}" for line in self.bootstrap_lines)
            priority.append(
                "- 用户下一步动作：邀请学习者确认或开始第一学习任务"
            )
            if self.required_elements:
                priority.append(
                    "回答必须包含：" + "、".join(self.required_elements)
                )
            return "\n".join(
                ["学习响应契约（优先级最高，覆盖陪伴默认开场）：", *priority]
            )
        lines: list[str] = []
        if self.course:
            lines.append(f"课程：{self.course}")
        if self.chapter:
            lines.append(f"章节：{self.chapter}")
        if self.focus:
            lines.append(f"当前重点：{self.focus}")
        if self.action and self.action != "free_chat":
            lines.append(f"学习动作：{self.action.upper()}")
        if self.next_step:
            lines.append(f"下一步：{self.next_step}")
        if self.required_elements:
            lines.append(
                "回答必须包含：" + "、".join(self.required_elements)
            )
        if not lines:
            return None
        return "\n".join(
            ["学习响应契约（由课程结构决定，必须体现在回答中）：", *[f"- {l}" for l in lines]]
        )

    # -- deterministic validation -------------------------------------------

    def missing_anchors(self, response: str) -> tuple[str, ...]:
        """Anchors absent from the response — deterministic, no LLM."""
        text = _squash(response)
        return tuple(anchor for anchor in self.anchors if _squash(anchor) not in text)

    def is_satisfied(self, response: str) -> bool:
        return not self.missing_anchors(response)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _squash(text: str) -> str:
    # CJK title brackets (《绪论》) normalize to the bare form so answers may
    # style chapter names without breaking the verbatim anchor check.
    return re.sub(r"[\s:：、,，.。！!？?《》「」『』（）()]+", "", text or "").lower()


def build_response_contract(
    learning_result: LearningLoopResult | None,
) -> LearningResponseContract | None:
    """Build the response contract from a loop result (pure; no recomputation).

    The required elements come from the DECIDED action (Phase 4); the anchors
    are the checkable facts the answer must reference: the course name and the
    next structural node. No anchors (free chat) → None.
    """
    if learning_result is None:
        return None
    learning_context = getattr(learning_result, "learning_context", None)
    decision = getattr(learning_result, "decision", None)
    course = getattr(learning_context, "course_name", None) or ""
    if not course:
        return None
    chapter = getattr(learning_context, "current_chapter", None) or ""
    focus = getattr(learning_context, "current_focus", None) or ""
    next_step = getattr(learning_context, "next_in_order", None) or ""
    action = getattr(learning_result, "action", None) or getattr(decision, "action", None) or ""

    # Phase 9C: a fresh course's bootstrap enriches the contract (the decision
    # itself — FREE_CHAT — is unchanged; the bootstrap is not a decision).
    bootstrap = getattr(learning_result, "bootstrap", None)
    required = tuple(BOOTSTRAP_REQUIRED_ELEMENTS) if bootstrap is not None else (
        REQUIRED_ELEMENTS_BY_ACTION.get(str(action).lower(), ())
    )

    anchors = [course]
    if bootstrap is not None:
        for value in (getattr(bootstrap, "first_chapter", None),
                      getattr(bootstrap, "first_focus", None)):
            if _clean(value):
                anchors.append(_clean(value))
    elif next_step:
        anchors.append(next_step)
    anchors = tuple(_clean(a) for a in anchors if _clean(a))
    if not anchors:
        return None
    bootstrap_lines = (
        tuple(bootstrap.lines()) if bootstrap is not None else ()
    )
    return LearningResponseContract(
        course=course,
        chapter=chapter,
        focus=focus,
        action=str(action).lower(),
        next_step=next_step,
        required_elements=required,
        anchors=anchors,
        bootstrap_lines=bootstrap_lines,
    )


__all__ = [
    "LearningResponseContract",
    "REQUIRED_ELEMENTS_BY_ACTION",
    "BOOTSTRAP_REQUIRED_ELEMENTS",
    "build_response_contract",
]
