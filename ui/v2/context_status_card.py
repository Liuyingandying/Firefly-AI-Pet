"""ContextStatusCard — right-side "当前上下文状态卡" (read-only view).

Pure presentation: takes an already-resolved :class:`ContextStatusView` (the
CONSOLE aggregates real facts from LearningController / conversation store /
provider status; this widget never touches LearningStore, providers or the
runtime). Sections are rendered dynamically — only non-empty facts appear and
placeholders like "—" / "等待指令" / "00:00" are never synthesized. Height
grows/shrinks with the visible sections (no fixed height).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui import theme

# LearningAction -> user-readable "下一步" copy. Presentation ONLY — this
# mapping never feeds back into the Decision.
ACTION_LABELS: dict[str, str] = {
    "explain": "理解当前知识点",
    "explain_recall": "回忆并检查理解",
    "practice": "完成一道练习",
    "review": "复习当前知识点",
    "transfer": "尝试应用与迁移",
    "move_next": "进入下一知识点",
    "free_chat": "",  # nothing to execute → hidden
}

_CAPTION_STYLE = (
    f"color: rgba{theme.V2.TEXT_SECONDARY}; background: transparent;"
    f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
)
_VALUE_STYLE = (
    f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent;"
    f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
)
_MODE_STYLE = (
    f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent;"
    f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt; font-weight: 700;"
)


@dataclass(frozen=True)
class ContextStatusView:
    """Presentation model — resolved facts only; empty means "hide".

    ``decision_action`` is the Phase 4 action id (lowercase) — mapped to user
    copy by :data:`ACTION_LABELS`, never shown raw.
    """

    mode: str = "自由对话"
    online: str = "在线"
    course_name: str = ""
    chapter: str = ""
    focus: str = ""
    decision_action: str = ""
    next_in_order: str = ""
    project_prompt: bool = False   # 学习模式开启但未选项目
    conversation_title: str = ""   # 仅真实会话标题（非「新对话」）
    ai_service: str = ""           # 仅「TJU LLM · 可用」式的 availability 事实


class ContextStatusCard(QFrame):
    """Dynamic-section status card; rebuilds visible sections per update."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("contextStatusCard")
        self.setStyleSheet(
            f"#contextStatusCard {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f"  border-radius: 16px;"
            f"}}"
        )
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._stack = QVBoxLayout(self)
        self._stack.setContentsMargins(16, 12, 16, 12)
        self._stack.setSpacing(10)
        self._view = ContextStatusView()

    # ------------------------------------------------------------ sections

    def update_state(self, view: ContextStatusView) -> None:
        """Rebuild the visible sections from ``view`` (dynamic height)."""
        self._view = view
        while self._stack.count():
            item = self._stack.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._stack.addWidget(self._section("当前状态", f"{view.mode} · {view.online}", bold=True))

        if view.project_prompt:
            self._stack.addWidget(self._section("", "选择一个学习项目后开始"))
            self._add_optional(view)
            return

        if view.course_name:
            self._stack.addWidget(self._section("学习", view.course_name))
            if view.chapter:
                self._stack.addWidget(self._section("当前章节", view.chapter))
            if view.focus:
                self._stack.addWidget(self._section("正在学习", view.focus))
            next_label = ACTION_LABELS.get(view.decision_action, "") if view.decision_action else ""
            if next_label:
                self._stack.addWidget(self._section("下一步", next_label))
            elif view.next_in_order:
                # 只读结构事实——不是 AI 推荐，绝不写成「推荐下一步」。
                self._stack.addWidget(self._section("课程顺序下一项", view.next_in_order))
        else:
            self._stack.addWidget(self._section("正在进行", "与流萤聊天"))
            if view.conversation_title:
                self._stack.addWidget(self._section("当前会话", view.conversation_title))
        self._add_optional(view)

    def _add_optional(self, view: ContextStatusView) -> None:
        if view.ai_service:
            self._stack.addWidget(self._section("AI 服务", view.ai_service))

    # ------------------------------------------------------------- widgets

    def _section(self, caption: str, value: str, *, bold: bool = False) -> QWidget:
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        if caption:
            caption_label = QLabel(caption, box)
            caption_label.setStyleSheet(_CAPTION_STYLE)
            caption_label.setWordWrap(True)
            layout.addWidget(caption_label)
        value_label = QLabel(value, box)
        value_label.setWordWrap(True)
        value_label.setStyleSheet(_MODE_STYLE if bold else _VALUE_STYLE)
        layout.addWidget(value_label)
        return box

    @property
    def view(self) -> ContextStatusView:
        return self._view


__all__ = ["ACTION_LABELS", "ContextStatusCard", "ContextStatusView"]
