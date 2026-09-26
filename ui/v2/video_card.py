"""VideoCard — inline session-video summary card for the companion console.

Pure rendering shell over a plain snapshot dataclass. The console builds a
``VideoCardInfo`` from the runner's session/study contexts (read-only) and
inserts the card widget into the chat flow after a video analysis. All three
buttons emit signals; the console decides what they do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme


@dataclass(frozen=True)
class VideoCardInfo:
    """Immutable snapshot for the card; built from SessionVideoContext etc."""

    bvid: str
    title: str
    owner: str = ""
    duration: str = ""
    read_status: str = ""          # e.g. "已阅读 · 237 段转录"
    study_stage: str = ""          # "" / "watching" / "quizzing"
    url: str = ""
    tags: list[str] = field(default_factory=list)


class VideoCard(QFrame):
    """Glassy card showing the session video + study entry points."""

    continue_study = Signal()
    timestamp_qa = Signal()
    open_video = Signal()

    _STAGE_LABELS = {"watching": "学习模式 · 可考我", "quizzing": "学习中 · 待你回答"}

    def __init__(self, info: VideoCardInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        # ui美化: 深色聊天区内的白色卡片 + 紫调软阴影（与 AI 气泡一致）。
        self.setStyleSheet(
            f"VideoCard {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba{theme.V2.CHAT_BORDER};"
            f"  border-radius: {theme.V2.RADIUS_INPUT}px;"
            f"}}"
        )
        from ui.v2 import motion

        motion.purple_shadow(self, blur=12, y_offset=2, alpha=26)
        title = QLabel(info.title)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN}; font-weight: 700; "
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_HEADING}pt;"
        )
        meta = QLabel(
            f"UP主：{info.owner}｜时长：{info.duration}"
            + (f"｜{info.bvid}" if info.bvid else "")
        )
        meta.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY}; "
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )

        status_parts = []
        if info.read_status:
            status_parts.append(f"📖 {info.read_status}")
        if info.study_stage in self._STAGE_LABELS:
            status_parts.append(f"🎓 {self._STAGE_LABELS[info.study_stage]}")
        status_label = QLabel("　".join(status_parts) if status_parts else "")
        status_label.setStyleSheet(
            f"color: rgba{theme.MINT_STATUS}; "
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )

        row = QHBoxLayout()
        row.setSpacing(8)
        for label, signal in (
            ("📚 继续学习", self.continue_study),
            ("⏱ 时间点问答", self.timestamp_qa),
            ("🔗 打开视频", self.open_video),
        ):
            button = QPushButton(label, self)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{"
                f"  color: rgba{theme.V2.PRIMARY};"
                f"  background: rgba{theme.V2.PRIMARY_SOFT};"
                f"  border: 1px solid rgba{theme.V2.PRIMARY_GLOW};"
                f"  border-radius: {theme.V2.RADIUS_CARD}px; padding: 4px 12px;"
                f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background: rgba{theme.V2.PRIMARY};"
                f"  color: rgba{theme.V2.ON_PRIMARY_TEXT};"
                f"}}"
            )
            button.clicked.connect(signal.emit)
            row.addWidget(button)
        row.addStretch(1)

        body = QVBoxLayout(self)
        body.setContentsMargins(14, 10, 14, 10)
        body.setSpacing(6)
        body.addWidget(title)
        body.addWidget(meta)
        body.addWidget(status_label)
        body.addLayout(row)


__all__ = ["VideoCard", "VideoCardInfo"]
