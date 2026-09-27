"""ChatView — "角色交流空间" message flow for the companion console (UI V2).

ui美化版（「流萤」发光紫视觉语言）:

- 聊天区: 淡紫轻盈渐变 (#F7F4FC → #ECE5F8) + 16px 圆角 + 1px 淡紫描边,
  叠加两枚超低透明度萤火微光斑（paintEvent 自绘, 漂浮感不抢内容）
- 用户消息: 右侧紫色渐变气泡 (#9B6DFF → #8A5CFF) + 白色文字 + 12px 圆角
- AI 消息: 左侧白色卡片 + 深灰文字 + 极淡紫调阴影
- 顶部细状态栏: 当前对话模式 + 「AI 正在思考…」三个跳动紫光点
- 空白态: 居中萤火虫图标 + 「和流萤说点什么吧 ✦」
- 消息入场: 纯透明度淡入 (ui.v2.motion.fade_rise_in; v4 移除 geometry
  上浮 —— 布局容器内改位置会与布局重排打架, 连续插消息时卡片重叠)

卡片宽度/高度自适应逻辑（Phase UI-5）保持不变: 宽度 clamp(自然宽,
120, 600), 高度由 QTextBrowser 文档高度驱动, 内部无滚动条。

Public API unchanged: ``append_user`` / ``append_assistant`` / ``append_card``
/ ``set_status`` / ``clear`` + 新增 ``set_mode``（顶部状态栏模式显示）.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap, QRadialGradient
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

from ui import theme
from ui.chat_markup import markdown_to_html
from ui.v2 import motion

_MAX_CARD_WIDTH = 600          # 卡片最大宽度（Phase UI-5；不填满中央列）
_CARD_H_PADDING = 18 * 2       # 卡片左右内边距
_MIN_CARD_WIDTH = 120          # 卡片最小宽度：短句不折成竖排（单字宽≈16px）
_AVATAR_SIZE = 34

REPO_ROOT = theme.repo_root()
ASSISTANT_AVATAR_PATH = REPO_ROOT / "assets" / "ui" / "avatar" / "liuying.png"

# v6.5: 淡紫渐变+描边+圆角上移到 console 的外层容器（chatWrap, 让背景
# 延伸覆盖输入框, 输入框悬浮其上）; ChatView 自身透明化, 只保留微光斑
# 与状态栏（top 圆角与容器 16px 圆角吻合）。
_CHAT_AREA_STYLE = (
    f"#chatView {{"
    f"  background: transparent; border: none;"
    f"}}"
    f"#chatStatusBar {{"
    f"  background: transparent;"
    f"  border-bottom: 1px solid rgba(155, 109, 255, 36);"
    f"  border-top-left-radius: {theme.V2.RADIUS_CONTAINER}px;"
    f"  border-top-right-radius: {theme.V2.RADIUS_CONTAINER}px;"
    f"}}"
)


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




class _Avatar(QFrame):
    """Simple round avatar container.

    Placeholder interface: pass ``image_path`` to render a real circular
    image; without one, a role-tinted disc with a single character is shown.
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
                f" rgba{theme.V2.CHAT_BORDER}; border-radius: {radius}px;"
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

    宽度 = clamp(自然宽, _MIN_CARD_WIDTH, 上限)，高度 = 当前宽度下的文档高度
    （Phase UI-5 决策记录见 git 历史; 逻辑与原版一致, 仅按角色切换文字颜色）。
    """

    def __init__(
        self,
        max_content_width: int,
        text_color: tuple[int, int, int, int],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._max_content_width = max_content_width
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setStyleSheet(
            "QTextBrowser { background: transparent; border: none;"
            f" color: rgba{text_color};"
            f" selection-background-color: rgba{theme.V2.PRIMARY_SOFT};"
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
            document.setTextWidth(-1)
            natural = int(document.size().width()) + 6
            width = max(_MIN_CARD_WIDTH, min(self._max_content_width, natural))
            if self.width() != width:
                self.setFixedWidth(width)
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
        f"background: transparent; font-family: {theme.V2_FONT_STACK};"
    )
    return label


class _MessageCard(QFrame):
    """One role-styled bubble: 用户=紫渐变右泡, 流萤=白色卡片.

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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        is_user = role == "user"
        self.setObjectName("userBubble" if is_user else "assistantBubble")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setMinimumWidth(_MIN_CARD_WIDTH)
        self.setMaximumWidth(_MAX_CARD_WIDTH)
        if is_user:
            # 用户消息: 紫色渐变 (#9B6DFF → #8A5CFF) + 白字 + 12px 圆角
            self.setStyleSheet(
                f"#userBubble {{"
                f"  background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
                f"    stop:0 rgba{theme.V2.PRIMARY}, stop:1 rgba{theme.V2.PRIMARY_HOVER});"
                f"  border: none; border-radius: {theme.V2.RADIUS_INPUT}px;"
                f"}}"
            )
            text_color = theme.V2.ON_PRIMARY_TEXT
            meta_color = (255, 255, 255, 190)
        else:
            # AI 消息: 白色卡片 + 深灰文字 + 极淡紫调阴影
            self.setStyleSheet(
                f"#assistantBubble {{"
                f"  background: rgba{theme.V2.CARD_BG_ASSISTANT};"
                f"  border: 1px solid rgba{theme.V2.CHAT_BORDER};"
                f"  border-radius: {theme.V2.RADIUS_INPUT}px;"
                f"}}"
            )
            text_color = theme.V2.TEXT_MAIN
            meta_color = theme.V2.TEXT_SECONDARY
            motion.purple_shadow(self, blur=12, y_offset=2, alpha=26)

        header = QHBoxLayout()
        header.setSpacing(8)
        if is_user:
            header.addStretch(1)
            header.addWidget(_caption(meta_color, f"你 · {time_text}"))
            header.addWidget(_Avatar(
                "你", theme.V2.PRIMARY_HOVER, theme.V2.ON_PRIMARY_TEXT))
        else:
            header.addWidget(_Avatar(
                "萤", theme.V2.PRIMARY_SOFT, theme.V2.PRIMARY,
                image_path=ASSISTANT_AVATAR_PATH))
            header.addWidget(_caption(theme.V2.PRIMARY, "流萤", bold=True))
            header.addWidget(_caption(meta_color, f"· {time_text}"))
            header.addStretch(1)

        browser = _ContentBrowser(
            max_content_width=_MAX_CARD_WIDTH - _CARD_H_PADDING - 6,
            text_color=text_color,
        )
        browser.set_content(html)

        body = QVBoxLayout(self)
        body.setContentsMargins(16, 10, 16, 12)
        body.setSpacing(6)
        body.addLayout(header)
        body.addWidget(browser)
        if role == "assistant" and voice_text:
            play_row = QHBoxLayout()
            play_row.addStretch(1)
            try:
                from voice_client.play_button import PlayVoiceButton
            except ImportError:
                PlayVoiceButton = None  # 语音对话插件未安装
            if PlayVoiceButton is not None:
                play_row.addWidget(PlayVoiceButton(lambda t=voice_text: t))
            body.addLayout(play_row)

    def set_plain_height(self) -> None:
        self.adjustSize()


class _EmptyHint(QWidget):
    """空白态引导: 萤火虫小图标 + 「和流萤说点什么吧 ✦」（替代空黑区域）."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        column.addStretch(2)

        icon_holder = QLabel(self)
        icon_holder.setPixmap(
            theme.vector_pixmap("sparkle", theme.V2.PRIMARY, size=40, pen_width=1.4)
        )
        icon_holder.setAlignment(Qt.AlignCenter)
        icon_holder.setStyleSheet("background: transparent;")
        column.addWidget(icon_holder, 0, Qt.AlignHCenter)

        title = QLabel("和流萤说点什么吧 ✦")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color: rgba{theme.V2.CHAT_TEXT_MAIN}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_HEADING}pt;"
        )
        column.addWidget(title, 0, Qt.AlignHCenter)

        subtitle = QLabel("B站链接 = 视频阅读 · 「考考我」= 学习模式")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet(
            f"color: rgba{theme.V2.CHAT_TEXT_SOFT}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        column.addWidget(subtitle, 0, Qt.AlignHCenter)
        column.addStretch(3)


class ChatView(QFrame):
    """深紫聊天容器: 顶部模式状态栏 + 可滚动消息流 + 空白态引导."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatView")
        self.setAttribute(Qt.WA_StyledBackground, True)  # QSS 渐变 + 自绘光斑共存
        self.setStyleSheet(_CHAT_AREA_STYLE)

        # -- 顶部细状态栏: 对话模式 + AI 思考实时状态 -----------------------
        self._mode_chip = QLabel("自由对话")
        self._mode_chip.setStyleSheet(
            f"color: rgba{theme.V2.PRIMARY}; background: rgba(255, 255, 255, 190);"
            f"border: 1px solid rgba{theme.V2.PRIMARY_GLOW};"
            f"border-radius: {theme.V2.RADIUS_CARD - 4}px; padding: 2px 12px;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        self._thinking_dots = motion.ThinkingDots()
        self._status_chip = QLabel("")
        self._status_chip.setStyleSheet(
            f"color: rgba{theme.V2.CHAT_TEXT_SOFT};"
            f"font-size: {theme.V2.FONT_CAPTION}pt; padding: 2px 8px;"
            f"font-family: {theme.V2_FONT_STACK};"
        )
        self._thinking_dots.setVisible(False)
        self._status_chip.setVisible(False)
        # 调试期版本标记: 看到它即当前预览窗口的视觉版本（避免旧窗口混淆）
        self._version_tag = QLabel("v8.8 · logo完整")
        self._version_tag.setStyleSheet(
            f"color: rgba{theme.V2.CHAT_TEXT_SOFT};"
            f"font-size: 8pt; padding: 0 2px;"
            f"font-family: {theme.V2_FONT_STACK};"
        )

        status_bar = QFrame(self)
        status_bar.setObjectName("chatStatusBar")
        status_bar.setFixedHeight(36)
        bar_layout = QHBoxLayout(status_bar)
        bar_layout.setContentsMargins(16, 0, 16, 0)
        bar_layout.setSpacing(8)
        bar_layout.addWidget(self._mode_chip)
        bar_layout.addStretch(1)
        bar_layout.addWidget(self._thinking_dots, 0, Qt.AlignVCenter)
        bar_layout.addWidget(self._status_chip)
        bar_layout.addWidget(self._version_tag)

        # -- 消息流 ----------------------------------------------------------
        self._empty_hint = _EmptyHint()
        self._column = QVBoxLayout()
        self._column.setContentsMargins(24, 14, 24, 18)  # 大留白：左右/上下
        self._column.setSpacing(18)                      # 消息之间不堆叠
        self._column.addStretch(1)      # 顶部弹簧（空态时与底部弹簧一起居中提示）
        self._column.addWidget(self._empty_hint)
        self._column.addStretch(1)      # 底部弹簧（消息从其上方插入）
        self._message_count = 0

        container = QWidget()
        container.setLayout(self._column)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(container)
        self._scroll.setFrameShape(QFrame.NoFrame)
        # 系统深色模式下, 滚动区容器/视口会被 Qt 默认深色调色板填成
        # #1E1E1E, 盖住 #chatView 的浅紫渐变 —— 三层全部显式透明,
        # 让 QSS 渐变透出来 (与 recent_sessions 的滚动区同款写法)。
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 8px; }"
            "QScrollBar::handle:vertical { background: rgba(155, 109, 255, 70);"
            " border-radius: 4px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            " { height: 0; background: transparent; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            " { background: transparent; }"
        )
        # 自动跟随: 内容范围变化时贴底（点击能力插入消息/卡片、文档高度
        # 晚成型、窗口 resize 都会触发 rangeChanged —— 无论哪一环晚到,
        # 范围最终变大时都会滚到底）。用户上翻查看历史时自动暂停跟随,
        # 拖回底部即恢复。
        self._auto_follow = True
        bar = self._scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range_changed)
        bar.valueChanged.connect(self._on_bar_value)

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)   # 留出 1px 内描边的绘制空间
        root.setSpacing(0)
        root.addWidget(status_bar)
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
        )
        wrapper = self._wrap_message(card, is_user=(role == "user"))
        self._insert_message(wrapper)

    def _wrap_message(self, card: _MessageCard, *, is_user: bool) -> QWidget:
        """右/左对齐的外层行容器（入场动画作用于此, 不与卡片阴影冲突）."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if is_user:
            row.addStretch(1)
            row.addWidget(card, 0, Qt.AlignTop)
        else:
            row.addWidget(card, 0, Qt.AlignTop)
            row.addStretch(1)
        wrapper = QWidget()
        wrapper.setLayout(row)
        return wrapper

    def _insert_message(self, wrapper: QWidget) -> None:
        """插入底部弹簧之前 + 淡入入场 + 隐藏空态提示.

        不在这里强制滚底: 贴底跟随由 ``rangeChanged`` 驱动（覆盖布局、
        文档高度等一切晚成型的时机）, 且尊重用户上翻查看历史的状态。
        """
        self._empty_hint.setVisible(False)
        self._message_count += 1
        self._column.insertWidget(self._column.count() - 1, wrapper)
        motion.fade_rise_in(wrapper)

    def append_card(self, widget: QWidget) -> None:
        """Insert an inline widget (e.g. VideoCard) into the message flow.

        v4: 外部卡片直插 column（与原版一致）—— 不再包 wrapper。多层
        布局（row→wrapper→column）会让「插入后才长高」的复杂卡片
        （如学习模式的入口卡）尺寸传播失效, 卡片塌陷裁内容; 直插一层
        由 column 直接管理, sizeHint 变化能正常触发重排。滚动同样由
        跟随机制处理。
        """
        self._empty_hint.setVisible(False)
        self._message_count += 1
        self._column.insertWidget(self._column.count() - 1, widget)
        motion.fade_rise_in(widget)

    def clear(self) -> None:
        """清空消息流并恢复空白态引导.

        布局结构恒为 [顶部弹簧, 空态提示, 底部弹簧], 消息插在提示与底部
        弹簧之间（index 2 起删除, 直到只剩这三个恒驻项）。
        """
        while self._column.count() > 3:
            item = self._column.takeAt(2)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._message_count = 0
        self._empty_hint.setVisible(True)

    def paintEvent(self, event) -> None:  # noqa: N802
        """QSS 渐变背景之上叠加两枚超低透明度萤火微光斑（漂浮感点缀）."""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(Qt.NoPen)
        width, height = self.width(), self.height()
        for cx, cy, radius, alpha in (
            (width * 0.16, height * 0.10, width * 0.44, 24),   # 左上 · 主微光
            (width * 0.86, height * 0.88, width * 0.32, 18),   # 右下 · 副微光
            (width * 0.60, height * 0.46, width * 0.18, 12),   # 中部 · 点缀微光
        ):
            gradient = QRadialGradient(cx, cy, radius)
            gradient.setColorAt(0.0, QColor(155, 109, 255, alpha))
            gradient.setColorAt(0.55, QColor(155, 109, 255, alpha // 3))
            gradient.setColorAt(1.0, QColor(155, 109, 255, 0))
            painter.setBrush(gradient)
            painter.drawEllipse(int(cx - radius), int(cy - radius), int(radius * 2), int(radius * 2))
        painter.end()

    # --------------------------------------------------------------- status

    def set_mode(self, text: str) -> None:
        """顶部状态栏: 当前对话模式（自由对话 / 学习模式 · 课程 …）."""
        text = (text or "").strip()
        self._mode_chip.setText(text or "自由对话")
        self._mode_chip.setVisible(bool(text))

    def set_status(self, text: str) -> None:
        """AI 实时状态: 非空时显示三个跳动紫光点 + 文字提示."""
        text = (text or "").strip()
        self._status_chip.setText(text)
        self._status_chip.setVisible(bool(text))
        self._thinking_dots.setVisible(bool(text))

    def _scroll_to_bottom(self) -> None:
        """主动滚到底（新消息请求时调用）: 恢复跟随并延迟一轮执行,
        等新消息的布局/文档高度完成, 避免停在半途。"""
        self._auto_follow = True
        QTimer.singleShot(0, self._scroll_now)

    def _scroll_now(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        """内容范围变化: 处于跟随态时贴到新的底部。"""
        if self._auto_follow:
            self._scroll.verticalScrollBar().setValue(maximum)

    def _on_bar_value(self, value: int) -> None:
        """滚动位置变化: 离开底部超过一屏阈值则暂停自动跟随（用户在
        查看历史）, 拖回底部附近即恢复。"""
        bar = self._scroll.verticalScrollBar()
        self._auto_follow = value >= bar.maximum() - 24


__all__ = ["ChatView"]
