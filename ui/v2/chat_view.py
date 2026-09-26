"""ChatView — "角色交流空间" message flow for the companion console (UI V2).

Phase: 消息卡自适应高度（ChatGPT 式气泡）。

- 每条消息是一张角色化卡片：流萤左（淡紫白）、用户右（浅青蓝）。
- 卡片宽度随内容收缩，上限 560px（不填满中央列）。
- 卡片高度完全由 QTextBrowser 的文档高度驱动：内部无任何滚动条
  （ScrollBarAlwaysOff），文档 ``adjustSize()`` 后按
  ``document().size().height()`` 更新 ``minimumHeight``——短消息矮、
  长消息自动增长。
- 头像是简单圆形容器（占位接口：放入 assets/ui/avatar/liuying.png 即
  自动圆形裁剪为真实头像）。

Public API unchanged: ``append_user`` / ``append_assistant`` / ``append_card``
/ ``set_status`` / ``clear``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap, QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from character import CharacterDisplayNames
from ui import theme
from ui.chat_markup import markdown_to_html

_MAX_CARD_WIDTH = 600          # 卡片最大宽度（Phase UI-5；不填满中央列）
_CARD_H_PADDING = 18 * 2       # 卡片左右内边距
_MIN_CARD_WIDTH = 120          # 卡片最小宽度：短句不折成竖排（单字宽≈16px）
_AVATAR_SIZE = 34

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSISTANT_AVATAR_PATH = REPO_ROOT / "assets" / "ui" / "avatar" / "liuying.png"


def _rounded_pixmap(image: QImage, size: int) -> QPixmap:
    """Center-crop ``image`` into a circular pixmap (antialiased)."""
    pixmap = QPixmap.fromImage(image).scaled(
        size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    result = QPixmap(size, size)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, size, size)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pixmap)
    painter.end()
    return result


from voice_client.play_button import PlayVoiceButton


class _Avatar(QFrame):
    """Simple round avatar container.

    Placeholder interface: pass ``image_path`` to render a real circular
    image (future: assets/ui/avatar/liuying.png); without one, a flat
    low-saturation disc with a single character is shown.
    """

    def __init__(
        self,
        text: str,
        base_color: tuple[int, int, int, int],
        text_color: tuple[int, int, int, int],
        image_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedSize(_AVATAR_SIZE, _AVATAR_SIZE)
        radius = _AVATAR_SIZE // 2

        image_label = QLabel(self)
        image_label.setGeometry(0, 0, _AVATAR_SIZE, _AVATAR_SIZE)
        loaded = False
        if image_path is not None and Path(image_path).is_file():
            try:
                image = QImage(str(image_path))
                if not image.isNull():
                    image_label.setPixmap(_rounded_pixmap(image, _AVATAR_SIZE))
                    loaded = True
            except Exception:  # asset problems never break the chat flow
                loaded = False
        if loaded:
            self.setStyleSheet(
                f"background: transparent; border: 1px solid"
                f" rgba{theme.V2.BORDER_SOFT}; border-radius: {radius}px;"
            )
            return

        image_label.setVisible(False)
        self.setStyleSheet(
            f"background: rgba{base_color}; border-radius: {radius}px;"
        )
        self._text = QLabel(text, self)
        self._text.setAlignment(Qt.AlignCenter)
        self._text.setGeometry(0, 0, _AVATAR_SIZE, _AVATAR_SIZE)
        self._text.setStyleSheet(
            f"color: rgba{text_color}; font-weight: 700;"
            f"font-size: {theme.V2.FONT_CAPTION}pt; background: transparent;"
        )


class _ContentBrowser(QTextBrowser):
    """QTextBrowser with all scrollbars off; size follows its document.

    Phase UI-5 决策记录：曾尝试 Preferred/Minimum + maximumWidth 的纯布局
    方案，但在 widgetResizable 滚动容器内出现宽度反馈环（短消息过宽、长
    消息被压缩出滚动条）。回到确定性显式同步：宽度 = clamp(自然宽,
    _MIN_CARD_WIDTH, 上限)，高度 = 当前宽度下的文档高度。
    """

    def __init__(self, max_content_width: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._max_content_width = max_content_width
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setStyleSheet(
            "QTextBrowser { background: transparent; border: none;"
            f" color: rgba{theme.V2.TEXT_MAIN};"
            f" font-family: {theme.V2_FONT_STACK};"
            f" font-size: {theme.V2.FONT_BODY}pt; }}"
        )
        self._syncing = False
        self.document().documentLayout().documentSizeChanged.connect(
            self._sync_to_document)

    def set_content(self, html: str) -> None:
        self.setHtml(html)
        self.document().adjustSize()
        self._sync_to_document(self.document().size())

    def _sync_to_document(self, size: QSize) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            document = self.document()
            # 1) 自然宽（setTextWidth(-1) = 不换行布局）。注意 Qt6 的
            #    idealWidth() 返回的是"可收缩最小行宽"（CJK 逐字可断），
            #    不能当自然宽使用，否则短句会被压成单字竖排。
            document.setTextWidth(-1)
            natural = int(document.size().width()) + 6
            width = max(_MIN_CARD_WIDTH, min(self._max_content_width, natural))
            if self.width() != width:
                self.setFixedWidth(width)
            # 2) 按最终宽度重排文档，再取真实高度。
            document.setTextWidth(max(20, width - 8))
            height = int(document.size().height()) + 10
            if self.minimumHeight() != height:
                self.setMinimumHeight(height)
                self.setMaximumHeight(height)
        finally:
            self._syncing = False


def _caption(color: tuple[int, int, int, int], text: str, *, bold: bool = False) -> QLabel:
    label = QLabel(text)
    weight = "; font-weight: 700" if bold else ""
    label.setStyleSheet(
        f"color: rgba{color}; font-size: {theme.V2.FONT_CAPTION}pt{weight};"
        f"background: transparent;"
    )
    return label


class _MessageCard(QFrame):
    """One role-styled warm paper card: avatar + name + time + rich content.

    高度自适应：卡片不含任何固定高度，跟随内部 ``_ContentBrowser`` 的文档
    高度（minimumHeight/maximumHeight 驱动）。
    """

    def __init__(
        self,
        html: str,
        *,
        role: str,  # "user" | "assistant"
        time_text: str,
        voice_text: str | None = None,
        assistant_name: str = CharacterDisplayNames().assistant_name,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        is_user = role == "user"
        card_bg = theme.V2.CARD_BG_USER if is_user else theme.V2.CARD_BG_ASSISTANT
        self.setObjectName("messageCard")
        # Phase UI-5: 水平 Preferred（宽度随内容、上限 600、最小 120）；
        # 垂直 Fixed —— 高度完全由内容高度驱动（非固定常量）。
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setMinimumWidth(_MIN_CARD_WIDTH)
        self.setMaximumWidth(_MAX_CARD_WIDTH)
        self.setStyleSheet(
            f"#messageCard {{ background: rgba{card_bg};"
            f" border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f" border-radius: 15px; }}"
        )

        header = QHBoxLayout()
        header.setSpacing(8)
        if is_user:
            header.addStretch(1)
            header.addWidget(_caption(theme.V2.TEXT_SECONDARY, f"你 · {time_text}"))
            header.addWidget(_Avatar(
                "你", theme.V2.PRIMARY_BLUE, theme.V2.TEXT_MAIN))
        else:
            header.addWidget(_Avatar(
                assistant_name[:1], theme.V2.ACCENT_PURPLE, theme.V2.TEXT_MAIN,
                image_path=ASSISTANT_AVATAR_PATH))
            header.addWidget(_caption(
                theme.V2.ACCENT_PURPLE, assistant_name, bold=True
            ))
            header.addWidget(_caption(theme.V2.TEXT_SECONDARY, f"· {time_text}"))
            header.addStretch(1)

        browser = _ContentBrowser(
            max_content_width=_MAX_CARD_WIDTH - _CARD_H_PADDING - 6)
        browser.set_content(html)

        body = QVBoxLayout(self)
        body.setContentsMargins(18, 12, 18, 14)
        body.setSpacing(6)
        body.addLayout(header)
        body.addWidget(browser)
        if role == "assistant" and voice_text:
            play_row = QHBoxLayout()
            play_row.addStretch(1)
            play_row.addWidget(PlayVoiceButton(lambda t=voice_text: t))
            body.addLayout(play_row)

    def set_plain_height(self) -> None:
        self.adjustSize()


class ChatView(QWidget):
    """Scrollable "角色交流空间" with warm paper cards and inline widgets."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        display_names: CharacterDisplayNames | None = None,
    ) -> None:
        super().__init__(parent)
        self._display_names = display_names or CharacterDisplayNames()

        self._status_chip = QLabel("")
        self._status_chip.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY};"
            f"font-size: {theme.V2.FONT_CAPTION}pt;"
            f"padding: 2px 24px;"
        )
        self._status_chip.setVisible(False)

        self._column = QVBoxLayout()
        self._column.setContentsMargins(24, 18, 24, 18)  # 大留白：左右/上下
        self._column.setSpacing(18)                       # 消息之间不堆叠
        self._column.addStretch(1)

        container = QWidget()
        container.setLayout(self._column)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(container)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 8px; }"
            "QScrollBar::handle:vertical { background: rgba(160,150,130,80);"
            " border-radius: 4px; min-height: 24px; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._status_chip)
        root.addWidget(self._scroll, 1)

    # ------------------------------------------------------------ messages

    def append_user(self, text: str) -> None:
        self._append_card(text, role="user")

    def append_assistant(self, text: str, voice_text: str | None = None) -> None:
        """voice_text: 传入时在卡片底部渲染 🔊 播放按钮 (v1.3 语音播放)。"""
        self._append_card(text, role="assistant", voice_text=voice_text)

    def _append_card(self, text: str, *, role: str, voice_text: str | None = None) -> None:
        card = _MessageCard(
            markdown_to_html(text or ""), role=role,
            time_text=datetime.now().strftime("%H:%M"),
            voice_text=voice_text,
            assistant_name=self._display_names.assistant_name,
        )
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            row.addStretch(1)
            row.addWidget(card, 0, Qt.AlignTop)
        else:
            row.addWidget(card, 0, Qt.AlignTop)
            row.addStretch(1)
        wrapper = QWidget()
        wrapper.setLayout(row)
        self._column.insertWidget(self._column.count() - 1, wrapper)
        self._scroll_to_bottom()

    def append_card(self, widget: QWidget) -> None:
        """Insert an inline widget (e.g. VideoCard) into the message flow."""
        self._column.insertWidget(self._column.count() - 1, widget)
        self._scroll_to_bottom()

    def clear(self) -> None:
        while self._column.count() > 1:
            item = self._column.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    # --------------------------------------------------------------- status

    def set_status(self, text: str) -> None:
        self._status_chip.setText(text)
        self._status_chip.setVisible(bool(text))

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


__all__ = ["ChatView"]
