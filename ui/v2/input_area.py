"""InputArea — "AI Terminal" composer for the companion console (UI V2).

ui美化版（「流萤」发光紫视觉语言）:

- 一体化 composer（仿 ZCode 输入区）: 输入文字与工具栏收在同一张卡片内,
  上半部分是透明无边框的多行输入区（内容增多自动长高, 最高 5 行）,
  底部一行是工具栏 —— 聚焦时整卡边框变紫 + 卡片泛紫光
- 快捷操作: 文件 / 截图 / 语音 / 快捷指令 做成半透明淡紫胶囊
  (全圆角, hover 底色加深)
- 深度思考: Toggle 开关样式, 开启时变紫 (ui.v2.motion.ToggleSwitch)
- 发送: 32px 紫色圆角小方块 + 白色线性箭头, hover 加深 (#8A5CFF),
  点击 0.95 倍缩放回弹, hover 带萤火微光

Send behavior is unchanged: Enter (without Shift) or the 发送 button emits
``send_requested`` with the current text. ``text() / setText() / clear() /
setPlaceholderText() / setFocus()`` delegates stay QLineEdit-compatible.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.v2 import motion
from ui.v2.attachments import ComposerAttachments, mime_has_attachment


class InputArea(QWidget):
    """Warm paper composer card. Send logic stays in the console."""

    send_requested = Signal(str)
    file_requested = Signal()
    screenshot_requested = Signal()
    voice_requested = Signal()
    quick_command_requested = Signal()
    deep_think_toggled = Signal(bool)
    stop_requested = Signal()

    _QUICK_ACTIONS = (
        ("file", "文件", "folder", "file_requested"),
        ("screenshot", "截图", "camera", "screenshot_requested"),
        ("voice", "语音", "mic", "voice_requested"),
        ("quick", "快捷指令", "bolt", "quick_command_requested"),
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inputArea")
        # plain QWidget 子类必须开启 WA_StyledBackground, 否则 QSS 的
        # 白卡背景/描边/圆角不绘制（同 ability_panel 的坑）——v6.5 输入区
        # 透明化并移上渐变背景后此坑显形: 白卡消失、控件裸浮在渐变上。
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 外层一体化 composer 卡片: 白卡 + 16px 容器圆角 + 紫调软阴影。
        # 聚焦态（边框变紫 + 阴影变亮）由 _apply_card_style 切换。
        self._apply_card_style(focused=False)
        # 同一个阴影效果承担两种角色: 失焦=紫调软阴影, 聚焦=卡片泛紫光
        self._card_shadow = motion.purple_shadow(self, blur=14, y_offset=2)

        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setPlaceholderText("输入你的问题…（可直接拖入 / 粘贴 图片、Word、PDF、PPT）")
        # 透明无边框, 视觉上直接"长"在卡片里; 高度随内容自动长高（见 _sync_height）
        self.text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.text_edit.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  background: transparent; border: none; padding: 4px 6px;"
            f"  color: rgba{theme.V2.TEXT_MAIN};"
            f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
            f"  selection-background-color: rgba{theme.V2.PRIMARY};"
            f"}}"
        )
        # placeholder 文字减淡（palette 角色, QSS 无对应属性）
        palette = self.text_edit.palette()
        palette.setColor(
            QPalette.PlaceholderText, QColor(178, 173, 187, 255)
        )
        self.text_edit.setPalette(palette)
        # QSS 写了 background: transparent 后 QPlainTextEdit 的输入光标会
        # 不可见（styled viewport 的 caret 取色被吞）——显式给 viewport 设
        # Text 色兜底, 并把光标加宽到 2px（150% 缩放屏上 ≈3 物理像素）。
        self.text_edit.setCursorWidth(2)
        vp_palette = self.text_edit.viewport().palette()
        vp_palette.setColor(QPalette.Text, QColor(*theme.V2.TEXT_MAIN))
        self.text_edit.viewport().setPalette(vp_palette)
        # 内容增多 → 输入区自动长高（仿 ZCode composer）
        self._INPUT_MIN_H, self._INPUT_MAX_H = 46, 132
        self.text_edit.document().documentLayout().documentSizeChanged.connect(
            self._sync_height
        )
        self._sync_height()
        # 多模态附件条（图片 / Word / PDF / PPT，一个 composer 最多一个）。
        # 入口：「文件」胶囊 / Ctrl+V 图片 / 拖放；发送路由在 console._send。
        self.attachments = ComposerAttachments(self)
        # 卡片聚焦发光 / IME 重影抑制 / Enter 发送共用同一个事件过滤器（见下）。

        # -- 快捷操作: ZCode 式半透明淡紫胶囊（图标 + 小字, 全圆角） -----------
        def _tool_button(label: str, icon_kind: str) -> QToolButton:
            button = QToolButton(self)
            button.setText(label)
            button.setIcon(theme.vector_icon(icon_kind, theme.V2.TEXT_SECONDARY, 13))
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QToolButton {{"
                f"  color: rgba{theme.V2.TEXT_SECONDARY};"
                f"  background: rgba{theme.V2.PRIMARY_SOFT};"
                f"  border: none; border-radius: 13px; padding: 5px 12px 5px 10px;"
                f"  font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
                f"}}"
                f"QToolButton:hover {{"
                f"  color: rgba{theme.V2.PRIMARY};"
                f"  background: rgba(155, 109, 255, 46);"
                f"}}"
            )
            return button

        tools_row = QHBoxLayout()
        tools_row.setSpacing(6)
        for action_id, label, icon_kind, signal_name in self._QUICK_ACTIONS:
            button = _tool_button(label, icon_kind)
            if action_id == "file":
                # 「文件」胶囊 = 附件选择器（多模态输入入口），不再空发信号。
                button.clicked.connect(self.attachments.pick_file)
            else:
                button.clicked.connect(getattr(self, signal_name).emit)
            setattr(self, f"_{action_id}_button", button)
            tools_row.addWidget(button)
        tools_row.addStretch(1)

        # -- 深度思考: Toggle 开关 + 文字 -----------------------------------
        self._deep_think = motion.ToggleSwitch(checked=False, parent=self)
        deep_label = QLabel("深度思考", self)
        deep_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        self._deep_think.toggled.connect(self.deep_think_toggled.emit)
        tools_row.addWidget(self._deep_think)
        tools_row.addWidget(deep_label)

        # -- 发送: ZCode 式 32px 紫色圆角小方块（白色箭头图标） ------------------
        self.send_button = QPushButton(self)
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.setFixedSize(32, 32)
        self.send_button.setIcon(theme.vector_icon("send", theme.V2.ON_PRIMARY_TEXT, 15))
        self.send_button.setStyleSheet(
            f"QPushButton {{"
            f"  background: rgba{theme.V2.PRIMARY};"
            f"  color: rgba{theme.V2.ON_PRIMARY_TEXT};"
            f"  border: none; border-radius: {theme.V2.RADIUS_INPUT}px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background: rgba{theme.V2.PRIMARY_HOVER};"
            f"}}"
            f"QPushButton:pressed {{"
            f"  background: rgba{theme.V2.PRIMARY_HOVER};"
            f"}}"
        )
        self.send_button.clicked.connect(
            lambda: motion.press_bounce(self.send_button)
        )
        self.send_button.clicked.connect(self._emit_send)
        motion.attach_hover_glow(self.send_button)
        tools_row.addWidget(self.send_button)

        # -- 停止生成: 发送后由宿主显示，点击发 stop_requested -------------
        from ui.v2.stop_button import StopButton

        self.stop_button = StopButton(self)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        tools_row.addWidget(self.stop_button)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)  # 大留白
        root.setSpacing(8)
        root.addWidget(self.text_edit)
        root.addWidget(self.attachments)
        root.addLayout(tools_row)

        # Enter 发送发生在 text_edit 上（真实键盘焦点所在），用事件过滤器
        # 拦截；Shift+Enter 不拦截，走 QPlainTextEdit 默认换行。
        self.text_edit.installEventFilter(self)
        # 拖放事件发给 QAbstractScrollArea 的 viewport 而非本体——过滤器
        # 必须两个都装，否则拖文件会走默认行为把 file:/// 路径插进输入框。
        self.text_edit.viewport().installEventFilter(self)
        # IME composition state: while Chinese composition is active the
        # QPlainTextEdit document is still empty, so Qt keeps drawing the
        # placeholder underneath the preedit at the top-left — two text
        # layers ("重影"). We suppress the placeholder for the duration of the
        # composition and restore the console's value when it ends.
        self._placeholder_saved: str | None = None

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        editor = self.text_edit
        is_editor = watched is editor
        if not is_editor and watched is not editor.viewport():
            return super().eventFilter(watched, event)
        event_type = event.type()
        # 拖放落在 viewport，其余编辑事件落在 editor 本体；附件拖放在
        # 两个监听对象上统一拦截（先预检再消费，非附件拖放走默认文本行为）。
        if event_type in (QEvent.DragEnter, QEvent.DragMove):
            if mime_has_attachment(event.mimeData()):
                event.acceptProposedAction()
                return True
        elif event_type == QEvent.Drop:
            if self.attachments.handle_mime(event.mimeData()):
                event.acceptProposedAction()
                return True
        elif is_editor:
            if event_type == QEvent.FocusIn:
                # 聚焦: 整卡边框变紫 + 阴影变亮泛紫光
                self._apply_card_style(focused=True)
                self._card_shadow.setColor(QColor(155, 109, 255, 70))
                self._card_shadow.setBlurRadius(18)
            elif event_type == QEvent.FocusOut:
                self._apply_card_style(focused=False)
                self._card_shadow.setColor(
                    QColor(*theme.V2.SHADOW_PURPLE)
                )
                self._card_shadow.setBlurRadius(14)
            elif event_type == QEvent.InputMethod:
                # Never touch the composition itself: only flip the placeholder.
                preedit = event.preeditString()
                if preedit and self._placeholder_saved is None:
                    current = self.text_edit.placeholderText()
                    if current:
                        self._placeholder_saved = current
                        self.text_edit.setPlaceholderText("")
                elif not preedit and self._placeholder_saved is not None:
                    self.text_edit.setPlaceholderText(self._placeholder_saved)
                    self._placeholder_saved = None
            elif event_type == QEvent.KeyPress:
                if event.matches(QKeySequence.Paste):
                    # Ctrl+V 剪贴板带图 → 转附件（v1 同语义；纯文本粘贴不受影响）。
                    clipboard = QGuiApplication.clipboard()
                    image = clipboard.image()
                    if not image.isNull():
                        self.attachments.add_image(image, "剪贴板图片")
                        return True
                if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
                    event.modifiers() & Qt.ShiftModifier
                ):
                    # 先 emit 后清空：console._send 是同步连接，会从输入框读取文本。
                    # 只带附件（无文字）也允许发送，问题文本由宿主补默认值。
                    text = self.text().strip()
                    if text or self.attachments.pending is not None:
                        self.send_requested.emit(text)
                    self.clear()
                    return True  # 已消费，不再插入换行
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------ behavior

    def _apply_card_style(self, *, focused: bool) -> None:
        """composer 卡片样式: 失焦紫灰边框, 聚焦品牌紫边框（外发光走 _card_shadow）."""
        border = (
            f"rgba{theme.V2.PRIMARY}" if focused else "rgba(216, 211, 226, 235)"
        )
        self.setStyleSheet(
            f"#inputArea {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid {border};"
            f"  border-radius: {theme.V2.RADIUS_CONTAINER}px;"
            f"}}"
        )

    def _sync_height(self, *_args) -> None:
        """输入区高度跟随内容: 单行 ~46px 起, 最多 5 行, 再多内部滚动."""
        doc_height = (
            self.text_edit.document().documentLayout().documentSize().height()
        )
        target = max(
            self._INPUT_MIN_H,
            min(self._INPUT_MAX_H, int(doc_height) + 10),
        )
        if self.text_edit.height() != target:
            self.text_edit.setFixedHeight(target)

    def _emit_send(self) -> None:
        text = self.text().strip()
        if not text and self.attachments.pending is None:
            return
        self.send_requested.emit(text)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        """Enter（无 Shift）发送并清空输入框；Shift+Enter 插入换行。"""
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (
            event.modifiers() & Qt.ShiftModifier
        ):
            # 先 emit 后清空（console._send 同步读输入框文本）。
            # 只带附件（无文字）也允许发送，问题文本由宿主补默认值。
            text = self.text().strip()
            if text or self.attachments.pending is not None:
                self.send_requested.emit(text)
            self.clear()
            event.accept()
            return
        super().keyPressEvent(event)

    # --------------------------------------- QLineEdit-compatible surface
    # The console previously used a QLineEdit; these delegates keep every
    # existing call site (and tests) working unchanged.

    def text(self) -> str:
        return self.text_edit.toPlainText()

    def setText(self, text: str) -> None:
        self.text_edit.setPlainText(text or "")

    def clear(self) -> None:
        self.text_edit.clear()

    def setPlaceholderText(self, text: str) -> None:
        # The console is installing a new prompt — drop any stale
        # composition-suppression state so the new text is honored.
        self._placeholder_saved = None
        self.text_edit.setPlaceholderText(text)

    def setFocus(self) -> None:  # type: ignore[override]
        self.text_edit.setFocus()

    @property
    def deep_think_enabled(self) -> bool:
        """深度思考 Toggle 的当前状态（仅读取, 语义接线留在宿主）。"""
        return self._deep_think.isChecked()


__all__ = ["InputArea"]
