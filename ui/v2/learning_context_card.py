"""Always-visible chapter context and one explicit next activity."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout


class LearningContextCard(QFrame):
    advance_requested = Signal()
    registered_practice_requested = Signal()
    exit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("learningContextCard")
        self.setStyleSheet("#learningContextCard { background: #eef5fa; border: 1px solid #b8cedf; border-radius: 10px; }"
                           "#learningContextCard QLabel { color: #293e50; background: transparent; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.heading = QLabel(self)
        self.heading.setTextFormat(Qt.PlainText)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.detail = QLabel(self)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.setFixedHeight(18)
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.next_button = QPushButton(self)
        self.next_button.clicked.connect(self.advance_requested)
        row.addWidget(self.next_button)
        row.addStretch()
        official = QPushButton("已注册课程练习", self)
        official.setToolTip("进入原 teach-mcp 课程练习；正式进度由 teach-mcp 管理")
        official.clicked.connect(self.registered_practice_requested)
        row.addWidget(official)
        leave = QPushButton("退出学习", self)
        leave.clicked.connect(self.exit_requested)
        row.addWidget(leave)
        layout.addLayout(row)
        self.hide()

    def set_context(self, context: dict):
        self.setVisible(bool(context))
        if not context:
            return
        self.heading.setText(f"课程：{context['course_title']} · 草稿预览\n章节：{context['chapter_title']}")
        self.detail.setText(f"知识点：{context['concept_title']}\n"
                            f"单元 {context['lesson_position']}/{context['lesson_count']} · 另有 {context['review_count']} 个单元待审 · 正式掌握度：未读取")
        self.progress.setValue(round(context["progress"]*1000))
        self.progress.setFormat(f"草稿活动完成进度：{context['progress']:.1%}")
        self.next_button.setText(context["button_label"])
        self.next_button.setEnabled(context["can_advance"])
