"""CoursePickerCard — transient light-weight learning-project chips.

Appears only when a course must be chosen (entering learning mode with
existing courses, or "换一个学习项目"). One card, chip buttons, disappears
after a choice — no persistent project manager, no new window.

Phase 1E.1: when ``continue_course`` is given, the card leads with a
「继续 X」 chip. That chip is the user-explicit confirmation that re-activates
the remembered project — nothing is activated before it is clicked.
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ui import theme


class CoursePickerCard(QFrame):
    """One-shot course selection chips inside the chat flow."""

    course_selected = Signal(str)      # course id
    create_requested = Signal()

    def __init__(
        self,
        courses: list[tuple[str, str]],  # (course_id, name)
        *,
        continue_course: tuple[str, str] | None = None,  # (course_id, name)
        headline: str = "选择一个学习项目",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("coursePicker")
        self.continue_course = continue_course
        self.setStyleSheet(
            f"#coursePicker {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f"  border-radius: 12px;"
            f"}}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        if continue_course is not None:
            headline = f"上次我们学到「{continue_course[1]}」，要继续吗？"
        title = QLabel(headline, self)
        title.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-family: {theme.V2_FONT_STACK};"
            f"font-size: {theme.V2.FONT_CAPTION}pt; background: transparent;"
        )
        layout.addWidget(title)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        if continue_course is not None:
            continue_id, continue_name = continue_course
            continue_btn = QPushButton(f"继续 {continue_name}", self)
            continue_btn.setObjectName("coursePickerContinue")
            continue_btn.setCursor(Qt.PointingHandCursor)
            continue_btn.setStyleSheet(
                f"#coursePickerContinue {{"
                f"  color: {theme.qcolor(theme.CYAN_ACCENT)};"
                f"  background: rgba{theme.GLASS_BACKGROUND_SELECTED};"
                f"  border: 1px solid rgba{theme.CYAN_ACCENT};"
                f"  border-radius: 10px; padding: 5px 12px;"
                f"  font-family: {theme.V2_FONT_STACK};"
                f"  font-size: {theme.V2.FONT_BODY}pt;"
                f"}}"
                f"#coursePickerContinue:hover {{"
                f"  background: rgba{theme.GLASS_BACKGROUND_HOVER};"
                f"}}"
            )
            continue_btn.clicked.connect(
                lambda _=False, cid=continue_id: self._choose(cid)
            )
            chips.addWidget(continue_btn)
            # 换一个项目: the other projects. The continue target is not
            # repeated, and the label is omitted when nothing else exists.
            courses = [c for c in courses if c[0] != continue_id]
            if courses:
                other_label = QLabel("换一个项目：", self)
                other_label.setStyleSheet(
                    f"color: rgba{theme.V2.TEXT_SECONDARY};"
                    f"font-family: {theme.V2_FONT_STACK};"
                    f"font-size: {theme.V2.FONT_CAPTION}pt; background: transparent;"
                    f"padding-left: 6px;"
                )
                chips.addWidget(other_label)
        for course_id, name in courses:
            button = QPushButton(name, self)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{"
                f"  color: {theme.qcolor(theme.TEXT_PRIMARY)};"
                f"  background: rgba{theme.GLASS_BACKGROUND};"
                f"  border: 1px solid rgba{theme.GLASS_BORDER};"
                f"  border-radius: 10px; padding: 5px 12px;"
                f"  font-family: {theme.V2_FONT_STACK};"
                f"  font-size: {theme.V2.FONT_BODY}pt;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background: rgba{theme.GLASS_BACKGROUND_HOVER};"
                f"  border: 1px solid rgba{theme.CYAN_ACCENT};"
                f"}}"
            )
            button.clicked.connect(
                lambda _=False, cid=course_id: self._choose(cid)
            )
            chips.addWidget(button)
        create_btn = QPushButton("＋ 新建学习项目", self)
        create_btn.setCursor(Qt.PointingHandCursor)
        create_btn.setStyleSheet(
            f"QPushButton {{"
            f"  color: {theme.qcolor(theme.CYAN_ACCENT)};"
            f"  background: transparent;"
            f"  border: 1px dashed rgba{theme.GLASS_BORDER};"
            f"  border-radius: 10px; padding: 5px 12px;"
            f"  font-family: {theme.V2_FONT_STACK};"
            f"  font-size: {theme.V2.FONT_BODY}pt;"
            f"}}"
            f"QPushButton:hover {{"
            f"  border: 1px solid rgba{theme.CYAN_ACCENT};"
            f"}}"
        )
        create_btn.clicked.connect(self._create)
        chips.addWidget(create_btn)
        chips.addStretch(1)
        layout.addLayout(chips)

    def _choose(self, course_id: str) -> None:
        self.hide()  # transient chips: disappear after a choice
        self.course_selected.emit(course_id)

    def _create(self) -> None:
        self.hide()
        self.create_requested.emit()


class TextbookDraftReviewState(str, Enum):
    """UI-only state for the textbook draft recovery flow (Phase 9A)."""

    READY_WITH_CONCEPTS = "ready_with_concepts"
    NEED_CONCEPT_GENERATION = "need_concept_generation"
    GENERATING_CONCEPTS = "generating_concepts"
    GENERATED_READY = "generated_ready"
    GENERATION_FAILED = "generation_failed"


class TextbookDraftReviewCard(QFrame):
    """Review surface for an imported textbook draft (Phase 7A).

    Shows the draft the adapter produced (📘 title / 来源 / 章节 ✓ / 概念) and
    the two frozen buttons. Nothing is activated until the user clicks 确认 —
    and even then the CF2 confirm gate (validator + user actor) applies.
    """

    confirm_requested = Signal()
    edit_requested = Signal()
    generate_requested = Signal()

    def __init__(
        self,
        summary,
        *,
        state: TextbookDraftReviewState | str | None = None,
        status_message: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("textbookDraftReview")
        self.summary = summary
        if state is None:
            state = (
                TextbookDraftReviewState.READY_WITH_CONCEPTS
                if summary.concept_proposals
                else TextbookDraftReviewState.NEED_CONCEPT_GENERATION
            )
        self.state = TextbookDraftReviewState(state)
        self.status_message = str(status_message or "").strip()
        self.setStyleSheet(_card_style("textbookDraftReview"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title = QLabel(f"📘 {summary.title}", self)
        title.setStyleSheet(_value_style())
        layout.addWidget(title)

        source = QLabel(f"来源：{summary.source_line}", self)
        source.setStyleSheet(_caption_style())
        layout.addWidget(source)

        layout.addWidget(QLabel("章节：", self))
        if summary.chapters:
            for position, chapter_title in summary.chapters:
                row = QLabel(f"✓ {chapter_title}", self)
                row.setStyleSheet(_value_style())
                layout.addWidget(row)
        else:
            empty = QLabel("（未识别出章节）", self)
            empty.setStyleSheet(_caption_style())
            layout.addWidget(empty)

        if summary.concept_proposals:
            layout.addWidget(QLabel("概念：", self))
            for name, _source_section, _confidence in summary.concept_proposals:
                row = QLabel(f"· {name}", self)
                row.setStyleSheet(_value_style())
                layout.addWidget(row)

        for warning in summary.warnings:
            if (
                not summary.concept_proposals
                and warning.startswith("没有识别出任何概念建议")
            ):
                # Phase 9A supplies the actionable generation/save choice below;
                # the old "confirm then add later" warning contradicts Validator.
                continue
            line = QLabel(f"提示：{warning}", self)
            line.setStyleSheet(_caption_style())
            line.setWordWrap(True)
            layout.addWidget(line)

        self._state_label = QLabel("", self)
        self._state_label.setStyleSheet(_caption_style())
        self._state_label.setWordWrap(True)
        layout.addWidget(self._state_label)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.confirm_btn = _primary_button("确认创建学习项目", self)
        self.confirm_btn.clicked.connect(self._confirm)
        row.addWidget(self.confirm_btn)
        self.generate_btn = _primary_button("生成知识点", self)
        self.generate_btn.clicked.connect(self._generate)
        row.addWidget(self.generate_btn)
        self.edit_btn = _ghost_button("继续编辑", self)
        self.edit_btn.clicked.connect(self._edit)
        row.addWidget(self.edit_btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.set_review_state(self.state, self.status_message)

    @property
    def can_confirm(self) -> bool:
        return bool(self.summary.concept_proposals) and self.state in {
            TextbookDraftReviewState.READY_WITH_CONCEPTS,
            TextbookDraftReviewState.GENERATED_READY,
        }

    def set_review_state(
        self,
        state: TextbookDraftReviewState | str,
        message: str = "",
    ) -> None:
        """Update only the transient review UI; the Draft remains persisted."""
        self.state = TextbookDraftReviewState(state)
        self.status_message = str(message or "").strip()
        ready = self.can_confirm
        generating = self.state is TextbookDraftReviewState.GENERATING_CONCEPTS
        failed = self.state is TextbookDraftReviewState.GENERATION_FAILED

        if ready:
            text = self.status_message
        elif generating:
            text = self.status_message or "正在生成知识点，请稍候……"
        elif failed:
            text = self.status_message or "知识点生成失败，可以稍后重试。"
        else:
            text = self.status_message or "当前教材已有章节结构，但还没有知识点。"
        self._state_label.setText(text)
        self._state_label.setVisible(bool(text))

        self.confirm_btn.setVisible(ready)
        self.generate_btn.setVisible(not ready and not generating)
        self.generate_btn.setText("重新生成知识点" if failed else "生成知识点")
        self.edit_btn.setText("继续编辑" if ready else "仅保存草稿")
        self.edit_btn.setEnabled(not generating)

    def _confirm(self) -> None:
        if not self.can_confirm:
            return
        self.hide()
        self.confirm_requested.emit()

    def _generate(self) -> None:
        if self.can_confirm or self.state is TextbookDraftReviewState.GENERATING_CONCEPTS:
            return
        self.generate_requested.emit()

    def _edit(self) -> None:
        # A concept-free Draft has no separate editor/re-entry screen yet.
        # Keep its recovery card in chat so "仅保存草稿" remains retryable.
        if self.can_confirm:
            self.hide()
        self.edit_requested.emit()


# ---------------------------------------------------------------------------
# Phase 2-UX: the learning entry surface
# ---------------------------------------------------------------------------


def _card_style(object_name: str) -> str:
    return (
        f"#{object_name} {{"
        f"  background: rgba{theme.V2.CARD_BG};"
        f"  border: 1px solid rgba{theme.V2.BORDER_SOFT};"
        f"  border-radius: 12px;"
        f"}}"
    )


def _caption_style() -> str:
    return (
        f"color: rgba{theme.V2.TEXT_SECONDARY};"
        f"font-family: {theme.V2_FONT_STACK};"
        f"font-size: {theme.V2.FONT_CAPTION}pt; background: transparent;"
    )


def _value_style() -> str:
    return (
        f"color: rgba{theme.V2.TEXT_MAIN};"
        f"font-family: {theme.V2_FONT_STACK};"
        f"font-size: {theme.V2.FONT_BODY}pt; background: transparent;"
    )


def _primary_button(text: str, parent) -> QPushButton:
    button = QPushButton(text, parent)
    button.setCursor(Qt.PointingHandCursor)
    button.setStyleSheet(
        f"QPushButton {{"
        f"  color: {theme.qcolor(theme.CYAN_ACCENT)};"
        f"  background: rgba{theme.GLASS_BACKGROUND_SELECTED};"
        f"  border: 1px solid rgba{theme.CYAN_ACCENT};"
        f"  border-radius: 10px; padding: 5px 12px;"
        f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
        f"}}"
        f"QPushButton:hover {{ background: rgba{theme.GLASS_BACKGROUND_HOVER}; }}"
    )
    return button


def _ghost_button(text: str, parent) -> QPushButton:
    button = QPushButton(text, parent)
    button.setCursor(Qt.PointingHandCursor)
    button.setStyleSheet(
        f"QPushButton {{"
        f"  color: {theme.qcolor(theme.TEXT_PRIMARY)};"
        f"  background: rgba{theme.GLASS_BACKGROUND};"
        f"  border: 1px dashed rgba{theme.GLASS_BORDER};"
        f"  border-radius: 10px; padding: 5px 12px;"
        f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
        f"}}"
        f"QPushButton:hover {{ border: 1px solid rgba{theme.CYAN_ACCENT}; }}"
    )
    return button


class LearningEntryCardView(QFrame):
    """The learning-mode welcome surface (Phase 2-UX §三).

    Shows the remembered last project and offers 继续 / 选择其他课程 / 新建.
    Nothing is activated by rendering it — activation happens only when the
    user clicks 继续 or 进入.
    """

    continue_requested = Signal()
    choose_other_requested = Signal()
    create_requested = Signal()
    import_textbook_requested = Signal()

    def __init__(self, card, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("learningEntry")
        self.card = card
        self.setStyleSheet(_card_style("learningEntry"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title = QLabel("📘 学习模式", self)
        title.setStyleSheet(_value_style())
        layout.addWidget(title)

        greeting = QLabel(card.greeting, self)
        greeting.setStyleSheet(_caption_style())
        layout.addWidget(greeting)

        if card.last_course_name:
            layout.addWidget(QLabel("上次学习：", self))
            last = QLabel(card.last_course_name, self)
            last.setStyleSheet(_value_style())
            layout.addWidget(last)
            prompt = QLabel("继续之前的学习吗？", self)
            prompt.setStyleSheet(_caption_style())
            layout.addWidget(prompt)
        elif card.course_count:
            hint = QLabel("选择要继续的学习项目。", self)
            hint.setStyleSheet(_caption_style())
            layout.addWidget(hint)
        else:
            hint = QLabel("还没有学习项目，先创建一个吧。", self)
            hint.setStyleSheet(_caption_style())
            layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        for action_id, label in card.actions:
            from core.learning.ui.entry import (
                ACTION_CHOOSE_OTHER,
                ACTION_CONTINUE,
                ACTION_CREATE,
                ACTION_IMPORT_TEXTBOOK,
            )

            if action_id == ACTION_CONTINUE:
                button = _primary_button(label, self)
                button.clicked.connect(self._continue)
            elif action_id == ACTION_CHOOSE_OTHER:
                button = _ghost_button(label, self)
                button.clicked.connect(self._choose_other)
            elif action_id == ACTION_CREATE:
                button = _ghost_button(label, self)
                button.clicked.connect(self._create)
            elif action_id == ACTION_IMPORT_TEXTBOOK:
                button = _ghost_button(label, self)
                button.clicked.connect(self._import)
            else:  # pragma: no cover - unknown action ids are not rendered
                continue
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

    def _continue(self) -> None:
        self.hide()
        self.continue_requested.emit()

    def _choose_other(self) -> None:
        self.hide()
        self.choose_other_requested.emit()

    def _create(self) -> None:
        self.hide()
        self.create_requested.emit()

    def _import(self) -> None:
        self.hide()
        self.import_textbook_requested.emit()


class ConceptChoiceCard(QFrame):
    """Concept selection for a pending check-understanding quiz (Phase 8B-1).

    📘 检查理解
    请选择知识点：
    [传递函数] [状态空间]

    Clicking a chip resumes the EXISTING pending assessment
    (``resume_pending_choice``) — no new flow, no side effects before the click.
    """

    concept_selected = Signal(str)   # concept NAME (resume takes the name)

    def __init__(self, concepts: list[tuple[str, str]], *, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("conceptChoice")
        self.concepts = tuple(concepts)
        self.setStyleSheet(_card_style("conceptChoice"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title = QLabel("📘 检查理解", self)
        title.setStyleSheet(_value_style())
        layout.addWidget(title)
        prompt = QLabel("请选择知识点：", self)
        prompt.setStyleSheet(_caption_style())
        layout.addWidget(prompt)

        chips = QHBoxLayout()
        chips.setSpacing(8)
        for _concept_id, name in self.concepts:
            chip = _primary_button(name, self)
            chip.clicked.connect(
                lambda _=False, name=name: self._choose(name)
            )
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addLayout(chips)

    def _choose(self, concept_name: str) -> None:
        self.hide()  # transient: disappears after the choice
        self.concept_selected.emit(concept_name)


class ReviewReminderCard(QFrame):
    """今日复习 reminder (Phase 8B-2 Part B).

    📅 今日复习
    · 传递函数
    · 状态空间
    [开始复习]

    [开始复习] enters the EXISTING assessment flow with prefer_due — the same
    path as typing 「复习一下」. Nothing starts before the click.
    """

    review_requested = Signal()

    def __init__(self, concept_names: list[str], *, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("reviewReminder")
        self.concept_names = tuple(concept_names)
        self.setStyleSheet(_card_style("reviewReminder"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title = QLabel("📅 今日复习", self)
        title.setStyleSheet(_value_style())
        layout.addWidget(title)
        subtitle = QLabel("以下知识点已到复习时间：", self)
        subtitle.setStyleSheet(_caption_style())
        layout.addWidget(subtitle)
        for name in self.concept_names:
            row = QLabel(f"· {name}", self)
            row.setStyleSheet(_value_style())
            layout.addWidget(row)

        row = QHBoxLayout()
        row.setSpacing(8)
        start_btn = _primary_button("开始复习", self)
        start_btn.clicked.connect(self._start)
        row.addWidget(start_btn)
        row.addStretch(1)
        layout.addLayout(row)

    def _start(self) -> None:
        self.hide()  # transient: disappears once review starts
        self.review_requested.emit()


class LearningProjectPicker(QFrame):
    """Project picker upgrade (Phase 2-UX §四).

    One compact card per project:

        📘 自动控制原理
        当前课程结构：自动控制原理 v1
        最近学习：传递函数
        状态：继续学习        [进入]
    """

    project_chosen = Signal(str)   # course_id
    create_requested = Signal()

    def __init__(self, cards, *, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("learningProjectPicker")
        self.cards = tuple(cards)
        self.setStyleSheet(_card_style("learningProjectPicker"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        if not self.cards:
            empty = QLabel("还没有学习项目", self)
            empty.setStyleSheet(_value_style())
            layout.addWidget(empty)
            create_btn = _primary_button("创建第一个课程", self)
            create_btn.clicked.connect(self._create)
            row = QHBoxLayout()
            row.addWidget(create_btn)
            row.addStretch(1)
            layout.addLayout(row)
            return

        for card in self.cards:
            block = QVBoxLayout()
            block.setSpacing(2)
            title = QLabel(card.title_line(), self)
            title.setStyleSheet(_value_style())
            block.addWidget(title)
            structure = QLabel(f"当前课程结构：{card.structure_line}", self)
            block.addWidget(structure)
            recent = QLabel(f"最近学习：{card.recent_line}", self)
            block.addWidget(recent)
            status = QLabel(f"状态：{card.status}", self)
            block.addWidget(status)
            for label in (structure, recent, status):
                label.setStyleSheet(_caption_style())

            row = QHBoxLayout()
            row.addLayout(block, 1)
            enter = _primary_button(card.action_label, self)
            enter.clicked.connect(
                lambda _=False, cid=card.course_id: self._choose(cid)
            )
            row.addWidget(enter, 0, Qt.AlignTop)
            layout.addLayout(row)

        create_btn = _ghost_button("＋ 新建学习项目", self)
        create_btn.clicked.connect(self._create)
        row = QHBoxLayout()
        row.addWidget(create_btn)
        row.addStretch(1)
        layout.addLayout(row)

    def _choose(self, course_id: str) -> None:
        self.hide()
        self.project_chosen.emit(course_id)

    def _create(self) -> None:
        self.hide()
        self.create_requested.emit()
