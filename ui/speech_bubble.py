"""A compact, non-chat greeting bubble anchored to Firefly."""

from __future__ import annotations

import html

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from . import theme


class BubbleTail(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.moveTo(QPointF(1, 1))
        path.lineTo(QPointF(self.width() - 2, 1))
        path.lineTo(QPointF(self.width() - 4, self.height() - 2))
        path.closeSubpath()
        painter.setBrush(theme.qcolor(theme.GLASS_BACKGROUND))
        painter.setPen(QPen(theme.qcolor(theme.GLASS_BORDER), 1))
        painter.drawPath(path)


class SpeechBubble(QWidget):
    def __init__(self, parent=None):
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
        super().__init__(parent, flags)
        self.setWindowTitle("Firefly Greeting")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())
        self.setFixedSize(theme.BUBBLE_SIZE)

        self._card = theme.GlassPanel(theme.RADIUS_CARD, self)
        self._card.setGeometry(theme.SHADOW_MARGIN, 15, self.width() - theme.SHADOW_MARGIN * 2, 94)
        theme.apply_soft_shadow(self._card, blur=36, y_offset=7)

        layout = QHBoxLayout(self._card)
        layout.setContentsMargins(theme.SPACE_LG + 2, theme.SPACE_SM, theme.SPACE_LG + 2, theme.SPACE_SM)
        layout.setSpacing(theme.SPACE_MD)
        self._card_layout = layout
        self._star = theme.VectorIcon("star", theme.CYAN_ACCENT, 24, self._card)
        layout.addWidget(self._star, 0, Qt.AlignVCenter)

        self._text = QLabel(self._card)
        self._text.setTextFormat(Qt.RichText)
        self._text.setText(theme.bubble_html())
        self._text.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._text.setStyleSheet(theme.transparent_window_style())
        layout.addWidget(self._text, 1, Qt.AlignVCenter)

        self._tail = BubbleTail(self)
        self._tail.setGeometry(self.width() - 62, 106, 32, 21)

        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self.hide)
        self._content: tuple = ("greeting", None)

    def show_greeting(self) -> None:
        self._auto_hide.stop()
        self._star.set_color(theme.CYAN_ACCENT)
        self._content = ("greeting", None)
        self._text.setText(theme.bubble_html())

    def show_blank(self) -> None:
        """Show the bubble shell with no greeting copy (greeting-on-startup off)."""
        self._auto_hide.stop()
        self._star.set_color(theme.CYAN_ACCENT)
        self._content = ("blank", None)
        self._text.setText("")

    def show_message(
        self,
        title: str,
        message: str,
        duration_ms: int = 3_000,
        *,
        accent=None,
    ) -> None:
        self._auto_hide.stop()
        self._star.set_color(accent if accent is not None else theme.CYAN_ACCENT)
        self._content = ("message", (title, message, accent))
        self._text.setText(self._message_html(title, message, accent))
        self.show()
        self.raise_()
        self._auto_hide.start(duration_ms)

    def apply_scale(self) -> None:
        w = theme.scaled_px(theme.BUBBLE_SIZE.width())
        h = theme.scaled_px(theme.BUBBLE_SIZE.height())
        self.setFixedSize(w, h)
        self._card.setGeometry(
            theme.scaled(theme.SHADOW_MARGIN),
            theme.scaled(15),
            w - theme.scaled(theme.SHADOW_MARGIN) * 2,
            theme.scaled(94),
        )
        self._tail.setGeometry(
            w - theme.scaled(62), theme.scaled(106), theme.scaled(32), theme.scaled(21)
        )
        self._star.setFixedSize(theme.scaled_px(24), theme.scaled_px(24))
        self._card_layout.setContentsMargins(
            theme.scaled(theme.SPACE_LG + 2),
            theme.scaled_px(theme.SPACE_SM),
            theme.scaled(theme.SPACE_LG + 2),
            theme.scaled_px(theme.SPACE_SM),
        )
        self._card_layout.setSpacing(theme.scaled_px(theme.SPACE_MD))
        if self._content[0] == "message":
            title, message, accent = self._content[1]
            self._text.setText(self._message_html(title, message, accent))
        elif self._content[0] == "blank":
            self._text.setText("")
        else:
            self._text.setText(theme.bubble_html())

    @staticmethod
    def _message_html(title: str, message: str, accent) -> str:
        secondary = theme.css_color(theme.TEXT_SECONDARY)
        if accent is not None:
            accent_css = theme.css_color(accent)
        else:
            accent_css = theme.css_color(theme.CYAN_ACCENT)
        return (
            f"<div style=\"font-family:'{theme.FONT_FAMILY}'; font-size:{theme.scaled_font_px(10.5)}pt; line-height:145%;\">"
            f"<span style=\"color:{accent_css}; font-weight:600;\">{html.escape(title)}</span><br>"
            f"<span style=\"color:{secondary};\">{html.escape(message)}</span>"
            "</div>"
        )
