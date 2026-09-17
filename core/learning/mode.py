"""Learning Mode runtime state (Phase 1B).

A minimal, widget-free holder for the current learning context:

- enabled:          the user is in learning mode
- active_course_id: the selected course (user-visible "学习项目")
- active_session_id: the running StudySession (started only after a course
                     is selected)

It holds NO learning facts: mastery / concepts / quizzes live in the
LearningStore; the last course id persists via SettingsManager.
"""

from __future__ import annotations

from dataclasses import dataclass

# Lightweight system context injected into ordinary chat turns while
# learning mode is active. Never claims mastery, never fabricates progress.
LEARNING_CONTEXT_TEMPLATE = (
    "当前模式：学习模式\n"
    "当前学习项目：{course_name}\n\n"
    "教学原则：\n"
    "- 优先围绕当前学习项目回答；\n"
    "- 解释时鼓励理解，而不是只给答案；\n"
    "- 用户明确要求直接答案时仍可以直接回答；\n"
    "- 不虚构掌握程度，不声称用户已经掌握某个知识点；\n"
    "- 保持流萤的陪伴人格，自然对话，不强制出题、不强制追问。"
)

MODE_CONTEXT_NO_COURSE = (
    "当前模式：学习模式（尚未选择学习项目）\n"
    "教学原则：先帮助用户建立学习项目，再围绕项目学习。"
)


@dataclass
class LearningModeState:
    """Current runtime learning context (never persisted directly)."""

    enabled: bool = False
    active_course_id: str | None = None
    active_course_name: str | None = None
    active_session_id: str | None = None

    def context_block(self) -> str | None:
        """Prompt context for the ordinary chat path; None when learning
        mode is off (ordinary chat stays untouched)."""
        if not self.enabled:
            return None
        if self.active_course_name:
            return LEARNING_CONTEXT_TEMPLATE.format(
                course_name=self.active_course_name
            )
        return MODE_CONTEXT_NO_COURSE

    def status_line(self) -> str:
        """Lightweight UI line, e.g. '📘 学习模式 · 自动控制原理'.

        The 📘 prefix lives here (next to LearningRuntimeContext.status_line)
        so both status sources render identically — the console never adds it.
        """
        if not self.enabled:
            return ""
        if self.active_course_name:
            return f"📘 学习模式 · {self.active_course_name}"
        return "📘 学习模式 · 未选择项目"


@dataclass(frozen=True)
class LearningRuntimeContext:
    """Read-only aggregation of the current learning runtime (Phase 1E).

    Built by the controller from the LearningStore + runtime state; injected
    into ordinary chat turns through the existing context channel. Purely
    observational — nothing here can write the store, and it never claims a
    mastery the learner has not earned.
    """

    enabled: bool = False
    course_id: str | None = None
    course_name: str | None = None
    concept_name: str | None = None
    concept_mastery: int | None = None
    session_id: str | None = None
    session_active: bool = False
    due_review_count: int = 0

    def context_block(self) -> str | None:
        if not self.enabled:
            return None
        lines: list[str] = [
            "当前模式：学习模式",
        ]
        if self.course_name:
            lines.append(f"当前学习项目：{self.course_name}")
        else:
            lines.append("当前学习项目：未选择")
        if self.concept_name:
            mastery_text = (
                f"（掌握 {self.concept_mastery}/5）"
                if self.concept_mastery is not None else ""
            )
            lines.append(f"最近学习的知识点：{self.concept_name}{mastery_text}")
        if self.session_active:
            lines.append("当前学习会话进行中。")
        if self.due_review_count > 0:
            lines.append(f"有 {self.due_review_count} 个知识点到了复习时间"
                         "（用户询问时才提示）。")
        lines.append(
            "教学原则：\n"
            "- 优先围绕当前学习项目回答；\n"
            "- 解释时鼓励理解，而不是只给答案；\n"
            "- 用户明确要求直接答案时仍可以直接回答；\n"
            "- 不虚构掌握程度，不声称用户已经掌握某个知识点；\n"
            "- 保持流萤的陪伴人格，自然对话，不强制出题、不强制追问。"
        )
        return "\n".join(lines)

    def status_line(self) -> str:
        if not self.enabled:
            return ""
        if self.course_name:
            return f"📘 学习模式 · {self.course_name}"
        return "📘 学习模式 · 未选择项目"
