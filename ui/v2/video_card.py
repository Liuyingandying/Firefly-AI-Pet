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
        self.setStyleSheet(
            f"VideoCard {{"
            f"  background: rgba{theme.GLASS_BACKGROUND};"
            f"  border: 1px solid rgba{theme.GLASS_BORDER};"
            f"  border-radius: 14px;"
            f"}}"
        )
        title = QLabel(info.title)
        title.setWordWrap(True)
        title.setStyleSheet(
            f"color: {theme.qcolor(theme.TEXT_PRIMARY)}; font-weight: 700; "
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_HEADING}pt;"
        )
        meta = QLabel(
            f"UP主：{info.owner}｜时长：{info.duration}"
            + (f"｜{info.bvid}" if info.bvid else "")
        )
        meta.setStyleSheet(
            f"color: {theme.qcolor(theme.TEXT_SECONDARY)}; "
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )

        status_parts = []
        if info.read_status:
            status_parts.append(f"📖 {info.read_status}")
        if info.study_stage in self._STAGE_LABELS:
            status_parts.append(f"🎓 {self._STAGE_LABELS[info.study_stage]}")
        status_label = QLabel("　".join(status_parts) if status_parts else "")
        status_label.setStyleSheet(
            f"color: {theme.qcolor(theme.MINT_STATUS)}; "
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
                f"  color: {theme.qcolor(theme.TEXT_PRIMARY)};"
                f"  background: rgba{theme.GLASS_BACKGROUND_SELECTED};"
                f"  border: 1px solid rgba{theme.GLASS_BORDER_SELECTED};"
                f"  border-radius: 9px; padding: 4px 10px;"
                f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
                f"}}"
                f"QPushButton:hover {{"
                f"  background: rgba{theme.GLASS_BACKGROUND_HOVER};"
                f"  border: 1px solid rgba{theme.CYAN_ACCENT};"
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
