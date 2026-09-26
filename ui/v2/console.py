"""CompanionConsole — the Firefly AI Pet console window (UI V2).

Composes CharacterHeader + ChatView + AbilityPanel + input bar into one
window and wires them to the existing ``CharacterConversationRunner``:

- user send  -> runner.ask() (existing turn pipeline, all video/study/vision
                capabilities keep working untouched)
- AgentEvent -> header state/task, chat bubbles, inline VideoCard
- AbilityPanel -> maps each capability to an existing entry point; nothing
                is reimplemented here (document via runner, settings/research
                are forwarded as signals an app host may connect).

The console owns no core state: it reads ``runner.session_video`` /
``runner.video_study`` read-only to build the VideoCard snapshot.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, Qt, Signal, QTimer
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.agent_events import AgentEventType
from core.learning.orchestrator import LoopStatus
from ui import theme
from ui.companion_attachment import (
    DEFAULT_ATTACHMENT_QUESTION,
    DEFAULT_DOCUMENT_QUESTION,
    DocumentAttachment,
)
from ui.v2.ability_panel import AbilityPanel
from ui.v2.attachments import mime_has_attachment
from ui.v2.character_header import CharacterHeader
from ui.v2.chat_view import ChatView
from ui.v2.companion_panel import CompanionPanel
from ui.v2.input_area import InputArea
from ui.v2.sidebar import Sidebar
from ui.v2.video_card import VideoCard, VideoCardInfo

log = logging.getLogger(__name__)

# ---- v5: 无边框融合标题栏（Claude Desktop 式） -------------------------------
# logo 探测: 调试副本(ui美化/logo.png) → 项目根 logo.png → assets/firefly.ico
# → 线性 ✦ 图标兜底; 全部相对路径, Release 打包不绑机器。

_LOGO_CANDIDATES = (
    Path(__file__).resolve().parents[2] / "logo.png",
    theme.repo_root() / "logo.png",
    theme.repo_root() / "assets" / "firefly.ico",
)

_RESIZE_MARGIN = 6  # 窗口四边可拖拽调宽的边缘宽度(px)


def _load_logo_pixmap(size: int = 26) -> QPixmap | None:
    for path in _LOGO_CANDIDATES:
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                # 高 DPI: 按物理像素采样并标记 DPR, 缩放屏上保持锐利。
                return theme.scaled_asset_pixmap(pixmap, size)
    return None


class _WindowButton(QPushButton):
    """标题栏窗口控制按钮: 透明底, hover 显示淡紫底（关闭钮红底白 ✕）."""

    def __init__(
        self,
        icon_kind: str,
        tooltip: str,
        *,
        hover_fill: tuple[int, int, int, int] | None = None,
        hover_icon: tuple[int, int, int, int] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = icon_kind
        self._hover_icon = hover_icon
        self._hovered = False
        self.setFixedSize(44, 34)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip(tooltip)
        self.setFlat(True)
        fill = hover_fill if hover_fill is not None else theme.V2.PRIMARY_SOFT
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            " border-radius: 8px; }"
            f"QPushButton:hover {{ background: rgba{fill}; }}"
        )
        self._apply_icon()

    def _apply_icon(self) -> None:
        if self._hovered and self._hover_icon is not None:
            color = self._hover_icon
        else:
            color = theme.V2.TEXT_SECONDARY
        self.setIcon(theme.vector_icon(self._kind, color, 13))

    def set_kind(self, icon_kind: str) -> None:
        if icon_kind != self._kind:
            self._kind = icon_kind
            self._apply_icon()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self._apply_icon()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self._apply_icon()
        super().leaveEvent(event)


_LOGO_SIZE = 42  # v8.7: logo 本体尺寸（28 × 1.5）


class _LogoBadge(QLabel):
    """logo 容器（v8.8: 加大留白, 完整承载圆形蒙版后的位图）."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(_LOGO_SIZE + 12, _LOGO_SIZE + 12)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background: transparent; border: none;")


class _TitleBar(QFrame):
    """与主界面融合的自定义标题栏: logo + 应用名 + 最小化/最大化/关闭.

    背景透明（延续主窗口米白渐变）, 按住拖动 = 系统级窗口移动
    (``startSystemMove``, 支持 Windows 贴边分屏), 双击 = 最大化/还原。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("appTitleBar")
        self.setFixedHeight(_LOGO_SIZE + 10)
        self.setStyleSheet(
            "#appTitleBar { background: transparent; border: none; }"
        )
        self.setCursor(Qt.ArrowCursor)

        layout = QHBoxLayout(self)
        # v8.7: 左侧边距调小（14 → 6, 连同窗口外边距视觉更贴近左缘）。
        layout.setContentsMargins(6, 3, 4, 3)
        layout.setSpacing(10)

        # v8.7: 无底托纯 logo —— 删除紫色光晕背景, logo 本体放大 1.5 倍
        # （28 → 42, 圆形蒙版保留, 与标题基线视觉平衡）。
        logo = _LogoBadge(self)
        logo_pixmap = _load_logo_pixmap(_LOGO_SIZE)
        if logo_pixmap is not None:
            # v8.8: 源图非正方形时先居中裁方（KeepAspectRatio 缩放后位图
            # 贴在左上角, 圆形蒙版会把出界内容裁掉 → 显示不完整）。
            width, height = logo_pixmap.width(), logo_pixmap.height()
            if width != height:
                square = min(width, height)
                logo_pixmap = logo_pixmap.copy(
                    (width - square) // 2, (height - square) // 2, square, square
                )
            # 圆形蒙版: 位图裁成正圆。
            ratio = logo_pixmap.devicePixelRatio()
            side = int(_LOGO_SIZE * ratio)
            masked = QPixmap(side, side)
            masked.setDevicePixelRatio(ratio)
            masked.fill(Qt.transparent)
            mask_painter = QPainter(masked)
            mask_painter.setRenderHint(QPainter.Antialiasing)
            circle = QPainterPath()
            circle.addEllipse(0, 0, _LOGO_SIZE, _LOGO_SIZE)
            mask_painter.setClipPath(circle)
            mask_painter.drawPixmap(0, 0, logo_pixmap)
            mask_painter.end()
            logo.setPixmap(masked)
        else:
            logo.setPixmap(
                theme.vector_pixmap("sparkle", theme.V2.PRIMARY, _LOGO_SIZE - 6)
            )
        layout.addWidget(logo)

        title = QLabel("流萤 · Firefly AI Pet", self)
        title.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK};"
            f"font-size: {theme.V2.FONT_BODY}pt; font-weight: 700;"
        )
        layout.addWidget(title)
        layout.addStretch(1)

        self._min_button = _WindowButton("win_min", "最小化", parent=self)
        self._min_button.clicked.connect(
            lambda: self.window().showMinimized()
        )
        self._max_button = _WindowButton("win_max", "最大化", parent=self)
        self._max_button.clicked.connect(self._toggle_maximize)
        self._close_button = _WindowButton(
            "win_close", "关闭",
            hover_fill=(232, 17, 35, 255),
            hover_icon=(255, 255, 255, 255),
            parent=self,
        )
        self._close_button.clicked.connect(self.window().close)
        layout.addWidget(self._min_button)
        layout.addWidget(self._max_button)
        layout.addWidget(self._close_button)

    def refresh_max_icon(self) -> None:
        window = self.window()
        self._max_button.set_kind(
            "win_restore" if window is not None and window.isMaximized() else "win_max"
        )

    def _toggle_maximize(self) -> None:
        window = self.window()
        if window is None:
            return
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._toggle_maximize()
        super().mouseDoubleClickEvent(event)


_HEADER_STATE_BY_STATUS = {
    "thinking": "working",
    "generating": "working",
    "organizing": "working",
    "processing": "working",
    "running_tool": "tool_running",
    "reading": "working",
    "completed": "success",
    "error": "error",
}

_STATUS_TEXT = {
    "thinking": "思考中…",
    "reading": "读取中…",
    "processing": "处理中…",
    "running_tool": "运行工具…",
}

# Study entry point used by the 学习模式 ability button.
_STUDY_TRIGGER = "考考我"

# Phase UI-3A: RuntimeActivityState -> 中文状态（CompanionPanel 状态卡）。
# Keys are the runtime.activity payload values (RuntimeActivityState is a
# str Enum, so plain-string keys match both enums and raw strings).
_RUNTIME_ACTIVITY_LABELS = {
    "idle": "等待指令",
    "working": "正在工作",
    "tool_running": "正在调用工具",
    "waiting_input": "等待回复",
    "success": "任务完成",
    "error": "出现问题",
}


class CompanionConsole(QMainWindow):
    """Main console window. Standalone: ``python -m ui.v2.console``."""

    # A host (app.py) may connect these to open existing popovers.
    settings_requested = Signal()
    research_requested = Signal()

    def __init__(
        self,
        runner: Any,
        parent: QWidget | None = None,
        *,
        on_video_card: Callable[[VideoCardInfo], None] | None = None,
        runtime_bus: Any = None,
    ) -> None:
        super().__init__(parent)
        self.runner = runner
        self._on_video_card = on_video_card
        self._last_card_bvid: str | None = None
        self._runtime_unsubscribe: Callable[[], None] | None = None

        self.setWindowTitle("流萤 · AI Pet 控制台")
        self.resize(1280, 800)
        # v5: 无边框融合标题栏 —— 去掉系统标题栏, logo 与窗口控件融入主界面
        # (拖动=系统级移动支持贴边分屏; 边缘 6px 可拉伸; 双击标题栏最大化)。
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        logo_pixmap = _load_logo_pixmap(64)
        if logo_pixmap is not None:
            self.setWindowIcon(QIcon(logo_pixmap))
        # Phase UI-2-C: deep-space background from the centralized theme
        # (assets/ui/background image overrides the code gradient).
        self.setStyleSheet(theme.v2_background_style())
        self._pending_edges: Qt.Edge | None = None

        self.header = CharacterHeader(parent=self)
        self.chat = ChatView(self)
        self.ability = AbilityPanel(self)

        # Phase 1B: learning-mode controller. The console owns the shell
        # lifecycle (mode / course / session); the controller never talks to
        # providers and never writes mastery.
        from core.learning.controller import LearningModeController

        self.learning = LearningModeController(
            settings=getattr(runner, "_screen_vision_settings", None)
        )
        if hasattr(runner, "learning_controller") and runner.learning_controller is None:
            runner.learning_controller = self.learning
        elif not hasattr(runner, "learning_controller"):
            runner.learning_controller = self.learning

        # Phase UI-2-B: AI Terminal composer (drop-in for the old QLineEdit —
        # text()/setText()/clear()/setPlaceholderText()/setFocus() delegates).
        self.input = InputArea(self)
        self.input.setPlaceholderText("和流萤聊天…（B站链接=视频阅读；考考我=学习模式）")
        self.send_button = self.input.send_button  # keep the attribute surface
        # Phase 8B-2: review reminder shows once per learning-mode session;
        # the textbook import remembers the file path for the resource viewer.
        self._review_card_shown = False
        self._textbook_import_path = None
        self._textbook_import_paths: dict[str, str] = {}
        self._textbook_review_cards: dict[str, Any] = {}

        # Phase UI-1 three-column layout (ui美化比例 1 : 3 : 1.2 —
        # Sidebar 240px | Conversation (stretch) | CompanionPanel 288px,
        # 突出聊天核心区域):
        #   Sidebar (identity + abilities + bottom actions)
        #   | Conversation (chat + terminal-style input)
        #   | CompanionPanel (character stage + status card)
        # v7 还原: 能力菜单容器 = 白卡 + 浅底菜单（初版样式;
        # 紫色悬浮面板已被 ui初版 之外的迭代放弃, 样式细节在 ability_panel）。
        self.ability.setStyleSheet(
            f"AbilityPanel {{ background: rgba{theme.V2.CARD_BG};"
            f" border: 1px solid rgba{theme.V2.BORDER_SOFT};"
            f" border-radius: {theme.V2.RADIUS_CONTAINER}px; }}"
        )
        self._recent_provider = self._build_recent_session_provider()
        self.sidebar = Sidebar(
            header=self.header,
            ability=self.ability,
            recent_provider=self._recent_provider,
            parent=self,
        )
        self.companion = CompanionPanel(parent=self, runner=self.runner)
        # Phase 8B-2: [查看材料] executes the viewer request (Part A).
        self.companion.view_materials_requested.connect(self._on_view_materials)

        # ui美化: 顶部模式状态栏并入 ChatView（chat.set_mode / set_status,
        # 含「AI 正在思考…」跳动光点）, 学习模式条不再单独占位。
        # v6.5: 聊天区淡紫渐变背景从 ChatView 上移到这个外层容器, 使背景
        # 延伸覆盖输入框区域; ChatView 透明化, InputArea 白卡悬浮在渐变上
        # （仿 ZCode: 输入框是浮在聊天背景之上的 composer 卡片）。
        self.chat_wrap = QFrame(self)
        self.chat_wrap.setObjectName("chatWrap")
        self.chat_wrap.setStyleSheet(
            f"#chatWrap {{"
            f"  background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            f"    stop:0 rgba{theme.V2.CHAT_BG_TOP}, stop:1 rgba{theme.V2.CHAT_BG_BOTTOM});"
            f"  border: 1px solid rgba{theme.V2.CHAT_BORDER};"
            f"  border-radius: {theme.V2.RADIUS_CONTAINER}px;"
            f"}}"
        )
        wrap_layout = QVBoxLayout(self.chat_wrap)
        wrap_layout.setContentsMargins(0, 0, 0, 0)
        wrap_layout.setSpacing(8)
        wrap_layout.addWidget(self.chat, 1)
        # 输入框四周留出渐变边缘 → 悬浮感（白卡 + 紫调阴影浮于渐变之上）
        input_row = QHBoxLayout()
        input_row.setContentsMargins(12, 0, 12, 12)
        input_row.addWidget(self.input)
        wrap_layout.addLayout(input_row)

        conversation_column = QVBoxLayout()
        conversation_column.setContentsMargins(0, 8, 0, 0)
        conversation_column.setSpacing(4)
        conversation_column.addWidget(self.chat_wrap, 1)

        # v5: 融合标题栏（跨三栏全宽, 透明背景延续主窗口渐变）。
        self.title_bar = _TitleBar(self)

        root = QHBoxLayout()
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(8)
        root.addWidget(self.sidebar)
        root.addLayout(conversation_column, 1)
        root.addWidget(self.companion)

        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 0, 10, 8)
        outer.setSpacing(0)
        outer.addWidget(self.title_bar)
        outer.addLayout(root)
        self.setCentralWidget(central)
        # v5: 边缘拖拽 resize —— 中央区四边 6px 检测（子布局 margin 10px,
        # 边缘带无子控件, 事件由 central 自身接收）。
        self._central = central
        central.setMouseTracking(True)
        central.setAttribute(Qt.WA_Hover, True)
        central.installEventFilter(self)

        # Wiring
        self.input.send_requested.connect(self._send)
        self.ability.requested.connect(self._on_ability)
        self.sidebar.action_requested.connect(self._on_sidebar_action)
        self.sidebar.session_clicked.connect(self._on_recent_session_clicked)
        self.sidebar.rename_requested.connect(self._on_session_rename_requested)
        self.sidebar.delete_requested.connect(self._on_session_delete_requested)
        runner_signal = getattr(self.runner, "agent_event", None)
        if runner_signal is not None:
            runner_signal.connect(self._on_event)
        # 附件：后台/按需 OCR 进度实时上 chip。
        ocr_progress = getattr(self.runner, "ocr_progress", None)
        if ocr_progress is not None:
            ocr_progress.connect(self.input.attachments.on_ocr_progress)
        ocr_page = getattr(self.runner, "ocr_page", None)
        if ocr_page is not None:
            ocr_page.connect(self.input.attachments.on_ocr_page)
        # 拖放不限输入卡：控制台整窗接收后转给 composer（同一套预检）。
        self.setAcceptDrops(True)
        # 当前回合是否为图片附件回合（FINAL 成功后消费图片；v1 同语义，
        # 避免其他后台回合的 FINAL 误清刚 attaching 的新附件）。
        self._turn_has_image = False
        # Phase UI-3A: consume RuntimeState from the bus when a host provides
        # one (read-only; the console never publishes). No bus = unchanged.
        if runtime_bus is not None and hasattr(runtime_bus, "subscribe_event"):
            self._runtime_unsubscribe = runtime_bus.subscribe_event(
                self._on_runtime_event
            )

        # TJU 健康探测结论不经过 runtime bus（dock 内部 Qt 信号）；状态卡
        # 的 AI 服务行需要跟随探测结论翻转，可见时以轻量周期刷新兜底。
        self._status_refresh_timer = QTimer(self)
        self._status_refresh_timer.setInterval(15_000)
        self._status_refresh_timer.timeout.connect(self._on_status_refresh_tick)
        self._status_refresh_timer.start()

        # Phase 1E: startup restore — if the persisted learning project still
        # exists, re-enable learning mode and re-attach an unfinished session
        # so the strip/status card shows it again. Never auto-starts an exam.
        # `_current_session_id` is initialized early so status refreshes during
        # startup (which run before the multi-session block below) are safe.
        self._current_session_id = None
        self._restore_learning_runtime()

        # Phase UI-2: multi-session — pick/create the active conversation
        # session, sync the runner's context and restore its turns in the view.
        self._current_session_id = self._ensure_active_session()
        self._reload_runner_history()
        self._refresh_recent_sessions()
        if self._current_session_id:
            self._render_history(self._current_session_id)

    # -------------------------------------------------- v5 frameless chrome

    def _edge_at(self, pos) -> tuple[Qt.Edge, Qt.CursorShape]:
        """窗口坐标 → (待拉伸边缘组合, 光标形状); 无边缘 → (0, Arrow)."""
        x, y = pos.x(), pos.y()
        width, height = self._central.width(), self._central.height()
        left = x <= _RESIZE_MARGIN
        right = x >= width - _RESIZE_MARGIN
        top = y <= _RESIZE_MARGIN
        bottom = y >= height - _RESIZE_MARGIN
        edges = Qt.Edges()
        if left:
            edges |= Qt.LeftEdge
        if right:
            edges |= Qt.RightEdge
        if top:
            edges |= Qt.TopEdge
        if bottom:
            edges |= Qt.BottomEdge
        if not edges:
            return edges, Qt.ArrowCursor
        if (left or right) and not (top or bottom):
            cursor = Qt.SizeHorCursor
        elif (top or bottom) and not (left or right):
            cursor = Qt.SizeVerCursor
        elif (left and top) or (right and bottom):
            cursor = Qt.SizeFDiagCursor
        else:
            cursor = Qt.SizeBDiagCursor
        return edges, cursor

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        if watched is self._central:
            event_type = event.type()
            if event_type in (QEvent.HoverMove, QEvent.HoverEnter, QEvent.MouseMove):
                if self.isMaximized():
                    self._central.setCursor(Qt.ArrowCursor)
                    self._pending_edges = None
                else:
                    edges, cursor = self._edge_at(event.position().toPoint())
                    self._central.setCursor(cursor)
                    self._pending_edges = edges or None
            elif event_type == QEvent.Leave:
                self._central.setCursor(Qt.ArrowCursor)
                self._pending_edges = None
            elif event_type == QEvent.MouseButtonPress:
                if (
                    not self.isMaximized()
                    and self._pending_edges
                    and event.button() == Qt.LeftButton
                ):
                    handle = self.windowHandle()
                    if handle is not None:
                        handle.startSystemResize(self._pending_edges)
                        return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:  # type: ignore[override]
        """最大化/还原时刷新标题栏按钮图标（□ ↔ 叠框）。"""
        if event.type() == QEvent.WindowStateChange:
            self.title_bar.refresh_max_icon()
        super().changeEvent(event)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        """整窗拖放预检：受支持的附件 mime 才接受（与输入卡同规则）。"""
        if mime_has_attachment(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        if self.input.attachments.handle_mime(event.mimeData()):
            event.acceptProposedAction()

    # ------------------------------------------------------------- sending

    def _send(self) -> None:
        text = self.input.text().strip()
        attachment = self.input.attachments.pending
        if not text and attachment is None:
            return
        self.input.clear()
        # 多模态附件：直达自由对话链路（学习 loop 只拦纯文本请求）。
        # 文档走 ask_with_document，图片走 ask_with_image；空文字补默认
        # 提问（v1 同语义），发送失败保留附件供重试。
        if attachment is not None:
            is_document = isinstance(attachment, DocumentAttachment)
            question = text or (
                DEFAULT_DOCUMENT_QUESTION if is_document else DEFAULT_ATTACHMENT_QUESTION
            )
            self.chat.append_user(
                f"[文档] {attachment.display_name}" if is_document
                else f"[图片] {attachment.display_name}"
            )
            if text:
                self.chat.append_user(text)
            asker = (
                self.runner.ask_with_document if is_document
                else self.runner.ask_with_image
            )
            try:
                ok = asker(question, attachment)
            except Exception as exc:  # runner must never crash the console
                log.exception("console attachment ask failed")
                self.chat.append_assistant(f"（发送失败了：{exc}）")
                return
            if not ok:
                self.chat.append_assistant("（我这边正忙着，稍等一下再试？）")
                return
            self.header.set_state("working")
            self._turn_has_image = not is_document
            return
        self.chat.append_user(text)
        # Phase 6.5: when learning mode is on, the input chain is owned by the
        # LearningLoopOrchestrator — it detects the learning request, records
        # the allowed interaction facts, prepares the loop context and answers
        # deterministic learning commands. Ordinary messages still flow to the
        # normal runner pipeline (the loop context rides along via
        # turn_context); a failed loop degrades to the same normal flow.
        # Phase 8B-1: when this send CREATES a pending needs_choice quiz,
        # surface the concept selection card (transition detection — the card
        # never re-appears on later turns while the user ignores it).
        pending_before = self.learning.pending_assessment() is not None

        result = self.learning.run_learning_loop(text)
        if result is None:
            # orchestrator unavailable: keep the legacy interception path so
            # free-chat "继续学习" and learning commands still work.
            learning_reply = self.learning.handle_text(text)
            if learning_reply is not None:
                self.chat.append_assistant(learning_reply)
                self._apply_learning_state()
                if getattr(self.learning, "entry_pending", False):
                    self._show_learning_entry()
                return
        elif result.status == LoopStatus.LEARNING_COMMAND.value:
            self.chat.append_assistant(result.response)
            self._apply_learning_state()
            if getattr(self.learning, "entry_pending", False):
                self._show_learning_entry()
            elif (self.learning.pending_assessment() is not None
                    and not pending_before):
                self._show_concept_choice_card()
            return
        # learning_loop / ordinary / degraded: normal chat. A learning turn
        # passes the loop result so the runner enforces the response contract
        # (Phase 9B-1) without recomputing anything.
        self.header.set_state("working")
        try:
            learning_result = result if (
                self.learning.state.enabled
                and result is not None
                and result.status != LoopStatus.ORDINARY.value
            ) else None
            if learning_result is not None:
                # Phase 9B-1: the loop result drives the response contract.
                ok = self.runner.ask(text, learning_result=learning_result)
            else:
                ok = self.runner.ask(text)
        except Exception as exc:  # runner must never crash the console
            log.exception("console ask failed")
            self.chat.append_assistant(f"（发送失败了：{exc}）")
            return
        if not ok:
            self.chat.append_assistant("（我这边正忙着，稍等一下再试？）")

    # ------------------------------------------------------- learning mode

    def _restore_learning_runtime(self) -> None:
        """Phase 1E.1 startup: a cold start is ALWAYS 自由对话.

        Only validate the remembered last project (a stale id is cleared) so a
        later user-explicit 「继续学习」 can offer it again. This never enables
        learning mode, never activates a course and never opens a session —
        even if Firefly was killed while learning mode was on. Best-effort: a
        store/settings problem just leaves everything off, never blocks the
        console.
        """
        try:
            self.learning.restore_runtime()
        except Exception:  # noqa: BLE001 - startup must never crash the UI
            log.exception("learning startup validation failed")
            return
        self._apply_learning_state()

    def _apply_learning_state(self) -> None:
        """Refresh the chat mode strip + right status card from the
        controller's current state (read-only)."""
        controller = self.learning
        enabled = bool(getattr(controller.state, "enabled", False))
        # status_line() already carries the 📘 prefix (single formatting
        # source in LearningModeState/LearningRuntimeContext).
        self.chat.set_mode(controller.status_line() if enabled else "")
        # 学习模式菜单选中态同步（AbilityPanel 互斥单选）。
        self.ability.set_active("study" if enabled else None)
        # 右侧「当前上下文状态卡」——只展示真实事实（无占位）。
        self._update_context_status()
        # Phase 7B: bound learning resources stay a separate display block.
        self.companion.set_learning_status(self._learning_status_card())
        # Phase 8B-2: surface the review reminder once per mode session when
        # reviews are due (never during ordinary chat — mode off resets it).
        if enabled:
            try:
                self._maybe_show_review_reminder()
            except Exception:  # noqa: BLE001 - reminder is optional
                log.exception("review reminder failed")
        else:
            self._review_card_shown = False

    def _update_context_status(self) -> None:
        """Aggregate the REAL current state into the context status card.

        Read-only: LearningContext / Decision are read; nothing is computed,
        guessed or written by the UI. Empty facts simply hide their section.
        """
        from dataclasses import replace

        from ui.v2.context_status_card import ContextStatusView

        enabled = bool(getattr(self.learning.state, "enabled", False))
        view = ContextStatusView(
            mode="学习模式" if enabled else "自由对话",
            online="在线",
        )
        if enabled:
            course = getattr(self.learning.state, "active_course_name", "") or ""
            if not course:
                view = replace(view, project_prompt=True)
            else:
                context = None
                decision = None
                try:
                    context = self.learning.learning_context()
                    decision = self.learning.learning_decision()
                except Exception:  # noqa: BLE001 - status read must not crash
                    log.warning("learning status read failed", exc_info=True)
                view = replace(view, course_name=course)
                if context is not None:
                    view = replace(
                        view,
                        chapter=getattr(context, "current_chapter", "") or "",
                        focus=getattr(context, "current_focus", "") or "",
                        next_in_order=getattr(context, "next_in_order", "") or "",
                    )
                if decision is not None:
                    view = replace(
                        view,
                        decision_action=getattr(decision, "action", "") or "",
                    )
        else:
            title = self._current_conversation_title()
            if title:
                view = replace(view, conversation_title=title)
        view = replace(view, ai_service=self._ai_service_line())
        self.companion.context_status.update_state(view)

    def _current_conversation_title(self) -> str:
        """Real session title (custom title or first user turn); '' when the
        session has no real content (新对话)."""
        if not self._current_session_id or self._recent_provider is None:
            return ""
        try:
            title = self._recent_provider.session_display_title(self._current_session_id)
        except Exception:  # noqa: BLE001 - best-effort display
            return ""
        return "" if title in ("", "新对话") else title

    def _ai_service_line(self) -> str:
        """回退链上当前实际可用的文本 Provider（配置 + TJU 实测，只读）。

        TJU 行以健康探测结论为准：未配置或探测不可达时让位给下一个可用
        Provider（Zhipu / DeepSeek / 用户自配），不再凭「密钥存在」显示
        不可达的端点。绝不宣称模型已被使用——只有「· 可用」。
        """
        try:
            from core.providers.catalog import CATEGORY_TEXT
            from core.providers.status import ProviderStatusService
            from ui.tju_llm_health import TjuApiStatus, last_probe_status

            tju_live = last_probe_status()
            for row in ProviderStatusService().snapshot():
                if getattr(row, "category", "") != CATEGORY_TEXT:
                    continue
                if getattr(row, "role", "") not in ("primary", "fallback"):
                    continue
                if not getattr(row, "available", False):
                    continue
                name = getattr(row, "display_name", "") or ""
                if not name:
                    continue
                if row.provider_id == "tju" and tju_live != TjuApiStatus.AVAILABLE:
                    continue
                return f"{name} · 可用"
            from core.credential_store import default_store
            from core.custom_providers import enabled_custom_providers

            for entry in enabled_custom_providers(default_store()):
                name = str(entry.get("name") or "").strip()
                if name:
                    return f"{name} · 可用"
        except Exception:  # noqa: BLE001 - status is optional display data
            pass
        return ""

    def _on_status_refresh_tick(self) -> None:
        """窗口可见时跟随 TJU 探测结论刷新状态卡（隐藏时不做无谓工作）。"""
        if self.isVisible():
            self._update_context_status()

    def _learning_status_card(self):
        from core.learning.ui import build_status_card

        try:
            context = self.learning.learning_context()
        except Exception:  # noqa: BLE001 - the UI must never crash on a read
            log.exception("learning context read failed")
            context = None
        recommendation = None
        teaching = None
        try:
            recommendation = self.learning.learning_decision()
            teaching = self.learning.teaching_context()
        except Exception:  # noqa: BLE001
            log.exception("learning decision read failed")
            recommendation = None
        resources = getattr(teaching, "resources", ()) if teaching is not None else ()
        return build_status_card(
            context, recommendation=recommendation, resources=resources
        )

    # --------------------------------------------------- learning entry UX

    def _show_learning_entry(self) -> None:
        """Render the learning-mode welcome surface (Phase 2-UX §三)."""
        from core.learning.ui import build_entry_card
        from ui.v2.course_picker import LearningEntryCardView

        try:
            snapshot = self.learning.entry_snapshot()
        except Exception:  # noqa: BLE001
            log.exception("learning entry snapshot failed")
            return
        card = build_entry_card(snapshot)
        view = LearningEntryCardView(card)
        view.continue_requested.connect(self._on_entry_continue)
        view.choose_other_requested.connect(self._show_project_picker)
        view.create_requested.connect(self._on_entry_create)
        # Phase 7A: [导入教材] — PDF → CurriculumDraft → review card
        view.import_textbook_requested.connect(self._on_import_textbook)
        self.chat.append_card(view)

    def _show_project_picker(self) -> None:
        """Render the LearningProjectPicker (Phase 2-UX §四)."""
        from core.learning.ui import build_project_cards
        from ui.v2.course_picker import LearningProjectPicker

        try:
            summaries = self.learning.project_summaries()
        except Exception:  # noqa: BLE001
            log.exception("learning project summaries failed")
            return
        picker = LearningProjectPicker(build_project_cards(summaries))
        picker.project_chosen.connect(self._on_project_chosen)
        picker.create_requested.connect(self._on_entry_create)
        self.chat.append_card(picker)

    def _on_entry_continue(self) -> None:
        """[继续 X] — the user-explicit activation of the remembered project."""
        reply = self.learning.continue_last_course()
        if reply is None:
            self._show_project_picker()
            return
        self._after_learning_start(reply)

    def _on_project_chosen(self, course_id: str) -> None:
        """[进入] — the user-explicit activation of a picked project."""
        self._after_learning_start(self.learning.select_course(course_id))

    def _on_entry_create(self) -> None:
        self.chat.append_assistant(self.learning.begin_create_prompt())
        self.input.setFocus()

    def _on_entry_create(self) -> None:
        self.chat.append_assistant(self.learning.begin_create_prompt())
        self.input.setFocus()

    # --------------------------------------------------- textbook import UX

    def _on_import_textbook(self) -> None:
        """[导入教材] — PDF → CurriculumDraft → review (Phase 7A).

        Deterministic only: chapters come from the PDF bookmarks and the first
        Draft deliberately contains no generated concepts. Provider-backed
        extraction is a separate, explicit-consent recovery action. Nothing is
        activated until the user reviews and confirms a concept-bearing Draft.
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "选择教材 PDF", "", "PDF (*.pdf)"
        )
        if not path:
            return
        self._textbook_import_path = path  # Phase 8B-2: for the resource viewer
        try:
            draft, service = self._build_textbook_draft(path)
        except Exception as exc:  # noqa: BLE001 - import failures are user-facing
            log.exception("textbook import failed")
            self.chat.append_assistant(f"（教材导入失败：{exc}）")
            return
        try:
            service.save(draft)
            summary = service.review(draft)
        except Exception as exc:  # noqa: BLE001
            log.exception("textbook draft save failed")
            self.chat.append_assistant(f"（草稿保存失败：{exc}）")
            return
        self._textbook_import_paths[draft.id] = path
        self._show_textbook_review_card(summary)

    def _build_textbook_draft(self, path: str):
        """Parse the PDF and produce a saved-ready draft + review service."""
        from pathlib import Path as _Path

        from core.learning.curriculum.adapters import PageLensCurriculumAdapter
        from core.pdf_processor import build_pdf_lazy_index

        pdf = _Path(path)
        index = build_pdf_lazy_index(
            pdf.read_bytes(), display_name=pdf.name
        )
        course = self.learning.create_course_shell(pdf.stem)
        adapter = PageLensCurriculumAdapter()
        draft = adapter.build_draft(
            index,
            course_id=course.id,
            course_name=course.name,
            source="pdf",
            concepts=(),
        )
        service = self.learning.textbook_review_service()
        if service is None:
            raise RuntimeError("教材审核服务不可用")
        return draft, service

    def _show_textbook_review_card(
        self,
        summary,
        *,
        state=None,
        status_message: str = "",
    ):
        """Render one review/recovery card for the persisted textbook Draft."""
        from ui.v2.course_picker import TextbookDraftReviewCard

        previous = self._textbook_review_cards.get(summary.draft_id)
        if previous is not None:
            previous.hide()
        card = TextbookDraftReviewCard(
            summary,
            state=state,
            status_message=status_message,
        )
        card.confirm_requested.connect(
            lambda checked=False, draft_id=summary.draft_id: self._on_textbook_confirm(
                draft_id
            )
        )
        card.generate_requested.connect(
            lambda checked=False, draft_id=summary.draft_id: self._on_textbook_generate(
                draft_id
            )
        )
        card.edit_requested.connect(
            lambda checked=False, draft_id=summary.draft_id: self._on_textbook_edit(
                draft_id
            )
        )
        self._textbook_review_cards[summary.draft_id] = card
        self.chat.append_card(card)
        return card

    def _confirm_textbook_concept_generation_consent(self) -> bool:
        """Ask before any PDF-derived text can be sent to the existing AI path."""
        dialog = QMessageBox(self)
        dialog.setWindowTitle("生成教材知识点")
        dialog.setIcon(QMessageBox.Information)
        dialog.setText(
            "为了建立学习课程，需要分析教材中的部分文本生成知识点。\n"
            "是否允许发送教材内容给 AI？"
        )
        allow = dialog.addButton("允许并生成", QMessageBox.AcceptRole)
        dialog.addButton("取消", QMessageBox.RejectRole)
        dialog.exec()
        return dialog.clickedButton() is allow

    def _on_textbook_generate(self, draft_id: str) -> None:
        """Explicit-consent recovery path for a concept-free textbook Draft."""
        service = self.learning.textbook_review_service()
        draft = service.store.get_draft(draft_id) if service is not None else None
        if draft is None:
            self.chat.append_assistant("（教材草稿不存在，无法生成知识点。）")
            return
        if draft.concept_candidates:
            self._show_textbook_review_card(service.review(draft))
            return
        if not self._confirm_textbook_concept_generation_consent():
            self.chat.append_assistant("已取消知识点生成，教材草稿仍已保存。")
            return

        from ui.v2.course_picker import TextbookDraftReviewState

        card = self._textbook_review_cards.get(draft_id)
        if card is not None:
            card.set_review_state(TextbookDraftReviewState.GENERATING_CONCEPTS)
            QApplication.processEvents()
        try:
            concepts = self._extract_textbook_concepts(
                self._textbook_import_paths.get(draft_id)
                or self._textbook_import_path
                or ""
            )
            if not concepts:
                self._show_textbook_review_card(
                    service.review(draft),
                    state=TextbookDraftReviewState.GENERATION_FAILED,
                    status_message="未发现可用知识点。",
                )
                self.chat.append_assistant("未发现可用知识点。教材草稿仍已保存。")
                return
            updated = self._add_textbook_concepts(draft, concepts)
            service.store.update_draft(updated)
        except Exception:  # noqa: BLE001 - the Draft must survive provider failures
            log.warning("textbook concept generation failed", exc_info=True)
            self._show_textbook_review_card(
                service.review(draft),
                state=TextbookDraftReviewState.GENERATION_FAILED,
                status_message="知识点生成失败，可以稍后重试。",
            )
            self.chat.append_assistant("知识点生成失败，可以稍后重试。教材草稿仍已保存。")
            return

        self._show_textbook_review_card(
            service.review(updated),
            state=TextbookDraftReviewState.GENERATED_READY,
            status_message="知识点已生成，请确认课程结构。",
        )

    def _extract_textbook_concepts(self, path: str) -> list[str]:
        """Use PdfQa only after the caller has completed the consent dialog."""
        if not path:
            raise RuntimeError("教材文件路径不可用")
        factory = getattr(self, "_textbook_pdf_qa_factory", None)
        if factory is None:
            from core.pdf_qa import PdfQa

            factory = PdfQa
        return list(factory().extract_concepts(path, consent=True) or [])

    def _add_textbook_concepts(self, draft, concepts: list[str]):
        """Return the same Draft identity with one adapter-built proposal chapter."""
        from dataclasses import replace
        from pathlib import Path as _Path

        from core.learning.curriculum.adapters import PageLensCurriculumAdapter
        from core.pdf_processor import build_pdf_lazy_index

        path = self._textbook_import_paths.get(draft.id) or self._textbook_import_path
        if not path:
            raise RuntimeError("教材文件路径不可用")
        pdf = _Path(path)
        index = build_pdf_lazy_index(pdf.read_bytes(), display_name=pdf.name)
        generated = PageLensCurriculumAdapter().build_draft(
            index,
            course_id=draft.course_id,
            course_name=draft.title,
            concepts=concepts,
            draft_id=draft.id,
            created_by=draft.created_by,
            make_path=bool(draft.paths),
        )
        proposal_chapters = tuple(
            chapter
            for chapter in generated.chapters
            if chapter.concepts
        )
        if not proposal_chapters:
            raise RuntimeError("未生成有效的 Concept Proposal")
        # Preserve every existing chapter and all domain/provenance fields. The
        # adapter contributes only its proposal-bearing review chapter and the
        # corresponding step on the existing adapter-built path.
        extra = replace(proposal_chapters[-1], position=len(draft.chapters) + 1)
        paths = list(draft.paths)
        generated_extra_steps = tuple(
            step
            for path_draft in generated.paths
            for step in path_draft.steps
            if step.target_id == proposal_chapters[-1].id
        )
        if generated_extra_steps:
            extra_step = generated_extra_steps[0]
            for index, path_draft in enumerate(paths):
                if any(step.target_id == extra.id for step in path_draft.steps):
                    break
                generated_path = next(
                    (item for item in generated.paths if item.id == path_draft.id),
                    None,
                )
                if generated_path is not None:
                    paths[index] = replace(
                        path_draft,
                        steps=tuple(path_draft.steps)
                        + (replace(extra_step, position=len(path_draft.steps) + 1),),
                    )
                    break
        return replace(
            draft,
            chapters=tuple(draft.chapters) + (extra,),
            paths=tuple(paths),
            updated_at=generated.updated_at,
        )

    # ------------------------------------------------ concept choice UX

    def _show_concept_choice_card(self) -> None:
        """Render the concept selection card for a pending quiz (Phase 8B-1)."""
        from ui.v2.course_picker import ConceptChoiceCard

        pending = self.learning.pending_assessment()
        if pending is None or not pending["concepts"]:
            return
        card = ConceptChoiceCard(pending["concepts"])
        card.concept_selected.connect(self._on_concept_choice)
        self.chat.append_card(card)

    def _on_concept_choice(self, concept_name: str) -> None:
        """[概念 chip] — resume the pending quiz (same path as typing it)."""
        reply = self.learning.resume_pending_assessment(concept_name)
        if reply is None:
            return
        self.chat.append_assistant(reply)
        self._apply_learning_state()

    # ------------------------------------------------ review reminder UX

    def _maybe_show_review_reminder(self) -> None:
        """Show the 📅 今日复习 card once per mode session when reviews are due.

        Phase 8B-2: display only — the assessment starts only when the user
        clicks [开始复习]. Never shown outside learning mode.
        """
        if self._review_card_shown:
            return
        due = self.learning.get_due_reviews()
        if not due:
            return
        from ui.v2.course_picker import ReviewReminderCard

        card = ReviewReminderCard(
            [item["concept_name"] or "未命名知识点" for item in due]
        )
        card.review_requested.connect(self._on_start_review)
        self.chat.append_card(card)
        self._review_card_shown = True

    def _on_start_review(self) -> None:
        """[开始复习] — enters the EXISTING prefer_due assessment flow."""
        reply = self.learning.handle_text("复习一下")
        if reply is None:
            return
        self.chat.append_assistant(reply)
        self._apply_learning_state()

    # ------------------------------------------------ resource viewer UX

    def _on_view_materials(self) -> None:
        """[查看材料] — resolve the focus resource into an open request.

        The viewer action only BUILDS the request; the console executes it via
        the app's established open-file mechanism. Unsupported resources show
        their message instead.
        """
        from core.learning.resources.viewer import ResourceViewerAction

        teaching = self.learning.teaching_context()
        resources = getattr(teaching, "resources", ()) if teaching is not None else ()
        request = ResourceViewerAction().open_request(
            resources[0] if resources else None
        )
        if request.supported:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            opened = QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(request.path))
            )
            if opened:
                self.chat.append_assistant(request.message)
                return
            self.chat.append_assistant("（没能打开文件：请确认它还在原位置。）")
            return
        if request.message:
            self.chat.append_assistant(request.message)

    def _on_textbook_confirm(self, draft_id: str) -> None:
        """[确认创建学习项目] — the explicit user gate (validator + activation)."""
        service = self.learning.textbook_review_service()
        if service is None:
            self.chat.append_assistant("（教材审核服务不可用。）")
            return
        draft = service.store.get_draft(draft_id)
        if draft is None:
            self.chat.append_assistant("（教材草稿不存在，无法确认。）")
            return
        if not draft.concept_candidates:
            # Phase 9A: never send a known concept-free Draft into Validator.
            # Return to the explicit generation/save recovery choice instead.
            from ui.v2.course_picker import TextbookDraftReviewState

            self._show_textbook_review_card(
                service.review(draft),
                state=TextbookDraftReviewState.NEED_CONCEPT_GENERATION,
            )
            return
        try:
            curriculum = service.confirm(draft_id, confirmed_by="user")
        except Exception as exc:  # noqa: BLE001 - validator/activation failures
            log.warning("textbook confirm failed: %s", exc)
            self.chat.append_assistant(f"（确认失败：{exc}）")
            return
        # Phase 7B: after confirmation, the chapters' PDF provenance becomes
        # course resources (references only — never the document text).
        try:
            from core.learning.resources import record_textbook_resources

            view = self.learning._curricula().get_active_curriculum(
                curriculum.course_id
            )
            created = record_textbook_resources(
                self.learning.resource_store(),
                curriculum.course_id,
                view,
                document_path=self._textbook_import_paths.get(draft_id)
                or getattr(self, "_textbook_import_path", None),
            )
        except Exception:  # noqa: BLE001 - resource recording is optional
            log.exception("textbook resource recording failed")
            created = []
        resource_note = (
            f"已登记 {len(created)} 条教材资源引用。"
            if created
            else "（本教材未提供可引用的页码来源。）"
        )
        self.chat.append_assistant(
            f"📘 学习项目已创建，课程结构 v{curriculum.version} 已激活。\n"
            f"{resource_note}\n"
            "说「继续学习」或从项目列表进入即可开始。"
        )
        self._apply_learning_state()

    def _on_textbook_edit(self, draft_id: str) -> None:
        """[继续编辑] — the draft stays a DRAFT; nothing is activated."""
        service = self.learning.textbook_review_service()
        draft = service.store.get_draft(draft_id) if service is not None else None
        if draft is not None and not draft.concept_candidates:
            self.chat.append_assistant(
                "教材草稿已保存，没有激活课程结构。你可以稍后在上方卡片中生成知识点。"
            )
            return
        self.chat.append_assistant(
            "草稿已保存（还没有激活课程结构）。说「我有哪些学习项目」可随时回来继续。"
        )

    def _after_learning_start(self, reply: str) -> None:
        """Common tail after a course is activated: show the reply, refresh the
        shell, then present the read-only structure (chapter / focus / next).
        """
        self.chat.append_assistant(reply)
        self._apply_learning_state()
        context = self.learning.learning_context()
        resume = context.resume_line() if context is not None else None
        if resume:
            self.chat.append_assistant(f"📘 {context.course_name}\n\n{resume}")

    def _maybe_show_course_picker(self, reply: str) -> None:
        """Show transient course chips when the mode reply asks the user to
        pick/continue a project. The card disappears on selection.

        Phase 1E.1: when a last project is remembered, the card leads with a
        「继续 X」 chip — the ONLY way to re-activate that project. Nothing is
        activated before the user clicks it.
        """
        if not self.learning.state.enabled:
            return
        courses = self.learning.list_course_tuples()
        continue_course = self.learning.last_course_tuple()
        if not courses and continue_course is None:
            return
        from ui.v2.course_picker import CoursePickerCard

        card = CoursePickerCard(courses, continue_course=continue_course)
        card.course_selected.connect(self._on_picker_course)
        card.create_requested.connect(self._on_picker_create)
        self.chat.append_card(card)

    def _on_picker_course(self, course_id: str) -> None:
        reply = self.learning.select_course(course_id)
        self.chat.append_assistant(reply)
        self._apply_learning_state()

    def _on_picker_create(self) -> None:
        self.chat.append_assistant(self.learning.begin_create_prompt())
        self.input.setFocus()

    # -------------------------------------------------------------- events

    def _on_event(self, event: Any) -> None:
        event_type = getattr(event, "type", None)
        if event_type == AgentEventType.STATUS:
            status = getattr(event, "status", "") or ""
            state = _HEADER_STATE_BY_STATUS.get(status)
            if state:
                self.header.set_state(state)
                self.companion.set_runtime_state(state)
            self.chat.set_status(_STATUS_TEXT.get(status, ""))
        elif event_type == AgentEventType.FINAL:
            text = getattr(event, "text", "") or ""
            self.chat.set_status("")
            self.header.set_task(text)
            self.header.set_state("success")
            self.companion.set_runtime_state("success")
            self.chat.append_assistant(text, voice_text=text)  # Voice-1.3: 播放按钮
            if self._turn_has_image:
                # 图片附件回合成功后消费；文档保留在 composer 可继续追问。
                self._turn_has_image = False
                self.input.attachments.consume()
            self._maybe_insert_video_card(text)
            # 消息完成后刷新右侧上下文状态卡（focus 可能已变化）。
            self._update_context_status()
        elif event_type == AgentEventType.ERROR:
            text = getattr(event, "text", "") or "出错了"
            self.chat.set_status("")
            self.header.set_state("error")
            self.companion.set_runtime_state("error")
            self.chat.append_assistant(f"（{text}）")
            # 失败：保留附件让用户直接重发（图片回合标记复位）。
            self._turn_has_image = False
        elif event_type == AgentEventType.CANCELLED:
            self.chat.set_status("已取消")
            self.header.set_state("idle")
            self.companion.set_runtime_state("idle")

    def _maybe_insert_video_card(self, assistant_text: str) -> None:
        """After a video-analysis turn, show one card per video in the flow."""
        session = getattr(self.runner, "session_video", None)
        if session is None:
            return
        bvid = getattr(session, "bvid", "") or ""
        if not bvid or bvid == self._last_card_bvid:
            return
        self._last_card_bvid = bvid
        info = VideoCardInfo(
            bvid=bvid,
            title=getattr(session, "title", "") or "",
            owner=getattr(session, "owner", "") or "",
            duration=getattr(session, "duration_formatted", "") or "",
            read_status=(
                f"已阅读 · {getattr(session, 'segment_count', '')} 段转录"
                if getattr(session, "segment_count", None)
                else "已阅读"
            ),
            study_stage=self._study_stage_label(),
            url=getattr(session, "url", "") or "",
            tags=list(getattr(session, "tags", []) or []),
        )
        card = VideoCard(info)
        card.continue_study.connect(self._on_continue_study)
        card.timestamp_qa.connect(self._on_timestamp_qa)
        card.open_video.connect(self._on_open_video)
        self.chat.append_card(card)
        if self._on_video_card is not None:
            self._on_video_card(info)

    def _study_stage_label(self) -> str:
        study = getattr(self.runner, "video_study", None)
        if study is None:
            return ""
        return str(getattr(study, "stage", "") or "")

    # -------------------------------------------------------- card actions

    def _on_continue_study(self) -> None:
        if getattr(self.runner, "video_study", None) is None:
            self._hint("还没有开始学习模式，说「陪我学习这个视频」就能开始～")
            return
        self.chat.append_user(_STUDY_TRIGGER)
        self.runner.ask(_STUDY_TRIGGER)

    def _on_timestamp_qa(self) -> None:
        self.input.setFocus()
        self.input.setPlaceholderText(
            "输入时间点，例如：2分30秒那里是什么？或 开头那个界面是什么意思？"
        )

    def _on_open_video(self) -> None:
        url = getattr(self.runner.session_video, "url", "") if self.runner.session_video else ""
        if not url:
            return
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl(url))

    def _hint(self, text: str) -> None:
        self.chat.append_assistant(text)

    def _on_runtime_event(self, event: Any) -> None:
        """Consume ``runtime.activity`` and Vision-1B ``camera.observed``.

        Read-only display bindings: activity state (RuntimeActivityState) is
        mapped to a label on the companion status card; a completed camera
        observation updates the task card.
        """
        kind = getattr(event, "kind", "")
        if kind == "camera.observed":
            self.companion.set_task("最近观察：刚刚")
            return
        if kind == "providers.updated":
            # 密钥保存/删除（Provider Manager）→ AI 服务行立即刷新。
            self._update_context_status()
            return
        if kind != "runtime.activity":
            return
        payload = getattr(event, "payload", None)
        state = str(getattr(payload, "value", payload) or "").strip().lower()
        label = _RUNTIME_ACTIVITY_LABELS.get(state)
        if label is not None:
            self.companion.set_companion_state(label)
            # ui美化: 同一状态 id 驱动右侧立绘动画 / 指示灯 / 状态短句。
            self.companion.set_runtime_state(state)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """Release the singleton holder and bus subscription."""
        global _console_instance
        if _console_instance is self:
            _console_instance = None
        pending_attachment = self.input.attachments.pending
        if pending_attachment is not None:
            cancel_lazy = getattr(pending_attachment, "cancel_lazy", None)
            if cancel_lazy is not None:
                cancel_lazy()  # 停掉在途 OCR 工作线程
        if self._runtime_unsubscribe is not None:
            unsubscribe = self._runtime_unsubscribe
            self._runtime_unsubscribe = None
            unsubscribe()  # idempotent per RuntimeBus contract
        super().closeEvent(event)

    # ------------------------------------------------------------ abilities

    def _on_sidebar_action(self, action_id: str) -> None:
        """Sidebar bottom actions: settings reuses the existing mapping;
        about / new_chat are light view-only helpers."""
        if action_id == "settings":
            self._on_ability("settings")
        elif action_id == "about":
            self._hint("流萤 · Firefly AI Pet 控制台\n陪伴、阅读、学习、科研，与你同行。")
        elif action_id == "new_chat":
            self._new_chat_session()

    # --------------------------------------------------- conversation sessions

    def _conversation_store(self):
        """The ConversationStore backing the runner (defensive)."""
        return getattr(
            getattr(self.runner, "runtime", None), "conversation_store", None
        )

    def _ensure_active_session(self) -> str | None:
        """Pick the active conversation session at startup.

        Most recently updated session when one exists, otherwise a brand-new
        chat session. The legacy ``firefly-main`` session is preserved (never
        deleted, never split). Returns the chosen session id.
        """
        store = self._conversation_store()
        if store is None:
            return None
        from core.conversation_store import new_session_id

        try:
            recent = getattr(store, "most_recent_session_id", None)
            chosen = recent() if callable(recent) else None
            if chosen is None:
                chosen = new_session_id()
                store.create_session(chosen)
            store.switch_session(chosen)
            return chosen
        except Exception:  # noqa: BLE001 - startup must never break the console
            log.warning("active session selection failed", exc_info=True)
            return getattr(store, "active_session_id", None)

    def _summarize_session_before_switch(self, store, old_session_id):
        """会话收尾 → set_summary（仅进入 Conversation history/context）。

        Boundary repair v1: session summaries stay in the ConversationStore
        and never auto-write a long-term memory entry — the previous direct
        write violated the explicit-consent boundary.  Summary text remains
        available in-context via the store.
        """
        if not old_session_id:
            return
        try:
            turns = store.load_working_window(session_id=old_session_id)
        except Exception:
            return
        if not turns or len(turns) < 4:
            return
        try:
            from core.conversation_store import build_session_summary

            summary = build_session_summary(turns)
            if not summary:
                return
            store.set_summary(summary, session_id=old_session_id)
        except Exception as exc:
            log.warning("session summary failed: %s", exc)

    def _new_chat_session(self) -> str | None:
        """Create a fresh session, switch to it and reset the chat view.

        The old session stays in the store; only the view clears. Returns the
        new session id (None when no store is reachable).
        """
        store = self._conversation_store()
        if store is None:
            self.chat.clear()
            return None
        from core.conversation_store import new_session_id

        self._summarize_session_before_switch(store, self._current_session_id)
        session_id = new_session_id()
        try:
            store.create_session(session_id)
            store.switch_session(session_id)
        except Exception as exc:  # noqa: BLE001 - never crash on a UI action
            log.warning("new chat session failed: %s", exc)
            self.chat.clear()
            return None
        self._current_session_id = session_id
        self._reload_runner_history()
        self.chat.clear()
        self._refresh_recent_sessions()
        self._update_context_status()
        self._hint("已开启新的对话。")
        return session_id

    def _switch_to_session(self, session_id: str) -> bool:
        """Switch to an existing session and restore its turns in the view.

        No LLM call, no re-save. Unknown/mock session ids are only logged.
        """
        store = self._conversation_store()
        if store is None:
            log.info("requested session restore (no store): %s", session_id)
            return False
        try:
            if session_id not in set(store.list_sessions()):
                log.info("requested session restore (unknown): %s", session_id)
                return False
            self._summarize_session_before_switch(store, self._current_session_id)
            store.switch_session(session_id)
        except Exception as exc:  # noqa: BLE001 - restore must never crash
            log.warning("session switch failed: %s", exc)
            return False
        self._current_session_id = session_id
        self._reload_runner_history()
        self._render_history(session_id)
        self._refresh_recent_sessions()
        self._update_context_status()
        log.info("restored conversation session: %s", session_id)
        return True

    def _reload_runner_history(self) -> None:
        """Re-seed the runner's in-memory history from the active session."""
        reloader = getattr(self.runner, "reload_history", None)
        if callable(reloader):
            try:
                reloader()
            except Exception:  # noqa: BLE001 - history sync is optional
                log.warning("runner history reload failed", exc_info=True)

    def _render_history(self, session_id: str) -> None:
        """Clear the view and replay the session's turns (user/assistant)."""
        store = self._conversation_store()
        self.chat.clear()
        if store is None:
            return
        try:
            turns = store.load_working_window(session_id=session_id)
        except Exception:  # noqa: BLE001 - history render is optional
            log.warning("session history render failed", exc_info=True)
            return
        for turn in turns:
            role = getattr(turn, "role", "")
            content = getattr(turn, "content", "")
            if not content:
                continue
            if role == "user":
                self.chat.append_user(content)
            elif role == "assistant":
                self.chat.append_assistant(content)

    def _refresh_recent_sessions(self) -> None:
        """Repopulate the recent list and re-apply the current highlight."""
        self.sidebar.refresh_recent_sessions()
        self.sidebar.set_current_session(self._current_session_id)

    def _on_recent_session_clicked(self, session_id: str) -> None:
        """Recent-session navigation: restore the clicked conversation."""
        self._switch_to_session(session_id)

    def _on_session_rename_requested(self, session_id: str) -> None:
        """Rename a session (QInputDialog → set_session_title → refresh)."""
        store = self._conversation_store()
        if store is None:
            return
        current = ""
        if self._recent_provider is not None:
            try:
                current = self._recent_provider.session_display_title(session_id)
            except Exception:  # noqa: BLE001 - default text is best-effort
                current = ""
        text, ok = QInputDialog.getText(self, "重命名会话", "会话名称：", text=current)
        if not ok:
            return
        try:
            store.set_session_title(session_id, text)
        except Exception as exc:  # noqa: BLE001 - rename must not crash
            log.warning("session rename failed: %s", exc)
            self._hint(f"（重命名失败：{exc}）")
            return
        # updated_at 不变 → 排序不变；只刷新标题显示
        self._refresh_recent_sessions()

    def _on_session_delete_requested(self, session_id: str) -> None:
        """Delete a session after explicit confirmation (never one-click)."""
        store = self._conversation_store()
        if store is None:
            return
        if not self._confirm_delete_session():
            return
        try:
            store.delete_session(session_id)
        except Exception as exc:  # noqa: BLE001 - delete must not crash
            log.warning("session delete failed: %s", exc)
            self._hint(f"（删除失败：{exc}）")
            return
        if session_id == self._current_session_id:
            self._switch_after_current_deleted()
        else:
            self._refresh_recent_sessions()

    def _confirm_delete_session(self) -> bool:
        """Explicit user confirmation for deleting a session (never one-click)."""
        dialog = QMessageBox(self)
        dialog.setWindowTitle("删除会话")
        dialog.setIcon(QMessageBox.Warning)
        dialog.setText(
            "删除这个会话？\n该操作会删除本会话的聊天记录，无法从 Firefly 中恢复。"
        )
        delete_button = dialog.addButton("删除", QMessageBox.DestructiveRole)
        dialog.addButton("取消", QMessageBox.RejectRole)
        dialog.exec()
        clicked = dialog.clickedButton()
        return clicked is not None and clicked.text() == delete_button.text()

    def _switch_after_current_deleted(self) -> None:
        """Current session was deleted: move to the newest remaining session,
        or create a fresh one when nothing is left. ``_current_session_id``
        never points at a deleted session."""
        store = self._conversation_store()
        if store is None:
            self._current_session_id = None
            self._refresh_recent_sessions()
            return
        from core.conversation_store import new_session_id

        target: str | None = None
        recent = getattr(store, "most_recent_session_id", None)
        if callable(recent):
            try:
                target = recent()
            except Exception:  # noqa: BLE001
                target = None
        if target is None:  # 没有剩余 session → 新建
            target = new_session_id()
            try:
                store.create_session(target)
            except Exception as exc:  # noqa: BLE001
                log.warning("new session after delete failed: %s", exc)
                return
        try:
            store.switch_session(target)
        except Exception as exc:  # noqa: BLE001
            log.warning("post-delete switch failed: %s", exc)
            return
        self._current_session_id = target
        self._reload_runner_history()
        self._render_history(target)
        self._refresh_recent_sessions()

    def _build_recent_session_provider(self):
        """Wire the recent list to the real conversation store when available."""
        from ui.v2.recent_sessions import RecentSessionProvider

        store = getattr(
            getattr(self.runner, "runtime", None), "conversation_store", None
        )
        return RecentSessionProvider(store=store)

    def _on_ability(self, capability: str) -> None:
        if capability == "video":
            self.input.setFocus()
            self.input.setPlaceholderText("粘贴 B站链接（如 https://www.bilibili.com/video/BV…）")
            self._hint("把 B站视频链接发给我，我就能读给你听并总结～")
        elif capability == "study":
            # Phase 1B: learning mode is no longer bound to Video Study.
            # Entering the mode establishes a learning context (project
            # course); a Bilibili link is just one possible material later.
            if self.learning.state.enabled:
                reply = self.learning.exit_mode()
            else:
                reply = self.learning.enter_mode()
            self.chat.append_assistant(reply)
            self._apply_learning_state()
            # Phase 2-UX: entering the mode shows the learning entry surface
            # (welcome + 继续/选择其他课程/新建) instead of bare chips. Nothing
            # is activated until the user picks a project.
            if self.learning.state.enabled:
                self._show_learning_entry()
        elif capability == "screen_vision":
            self.chat.append_user("看一下我的屏幕")
            self.runner.ask("看一下我的屏幕")
        elif capability == "document":
            path, _ = QFileDialog.getOpenFileName(
                self, "选择文档", "", "文档 (*.pdf *.pptx *.png *.jpg *.jpeg *.txt *.md)"
            )
            if not path:
                return
            self.chat.append_user(f"[文档] {path}")
            self._ask_document(path)
        elif capability == "research":
            self.research_requested.emit()
            self._show_research_tools()
        elif capability == "settings":
            self.settings_requested.emit()
            self._hint("⚙ 设置入口已请求（主界面工具栏）～")

    def _show_research_tools(self) -> None:
        """🔬 科研助手：真实可用的科研工具入口（第一版 = TJU Info Retrieval）。

        Compact chat card — no new window/sidebar. The「打开检索系统」button and
        the Quick Tools「打开」and the chat command all share ONE
        ``plugin.open_ui()`` (via the runner's injected callable)."""
        from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

        card = QFrame(self)
        card.setObjectName("researchToolsCard")
        # ui美化: 深色聊天区内的白色卡片（与 AI 气泡一致）+ 16px 圆角。
        card.setStyleSheet(
            f"#researchToolsCard {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba{theme.V2.CHAT_BORDER};"
            f"  border-radius: {theme.V2.RADIUS_INPUT}px;"
            f"}}"
        )
        from ui.v2 import motion

        motion.purple_shadow(card, blur=12, y_offset=2, alpha=26)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        title = QLabel("🔬 科研助手", card)
        title.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_HEADING}pt; font-weight: 700;"
        )
        layout.addWidget(title)

        # TJU Info Retrieval row
        row = QHBoxLayout()
        row.setSpacing(8)
        tju_name = QLabel("TJU Info Retrieval", card)
        tju_name.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt; font-weight: 600;"
        )
        tju_note = QLabel("天津大学学术信息检索", card)
        tju_note.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        open_btn = QPushButton("打开检索系统", card)
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(
            f"QPushButton {{"
            f"  color: rgba{theme.V2.ON_PRIMARY_TEXT}; background: rgba{theme.V2.PRIMARY};"
            f"  border: none; border-radius: {theme.V2.RADIUS_CARD}px;"
            f"  padding: 5px 14px; font-family: {theme.V2_FONT_STACK};"
            f"  font-size: {theme.V2.FONT_CAPTION}pt; font-weight: 600;"
            f"}}"
            f"QPushButton:hover {{ background: rgba{theme.V2.PRIMARY_HOVER}; }}"
        )
        motion.attach_hover_glow(open_btn)
        open_btn.clicked.connect(self._on_research_open_tju)
        row.addWidget(tju_name)
        row.addWidget(tju_note)
        row.addStretch(1)
        row.addWidget(open_btn)
        layout.addLayout(row)
        self.chat.append_card(card)

    def _on_research_open_tju(self) -> None:
        """科研助手「打开检索系统」→ 同一个 plugin.open_ui()（与 Quick Tools 共用）。"""
        opener = getattr(self.runner, "open_tju_info_retrieval", None)
        if callable(opener):
            try:
                message = opener()
            except Exception as exc:  # noqa: BLE001 - never leak internals
                message = f"（无法打开 TJU Info Retrieval：{str(exc)[:120]}）"
        else:
            message = "（TJU Info Retrieval 打开动作未接线。）"
        self.chat.append_assistant(message)

    def _ask_document(self, path: str) -> None:
        asker = getattr(self.runner, "ask_with_document", None)
        if asker is None:
            self._hint("当前运行器不支持文档附件。")
            return
        try:
            from ui.companion_attachment import load_document_file

            attachment = load_document_file(path)
        except Exception as exc:
            self._hint(f"（文档打不开：{exc}）")
            return
        try:
            ok = asker("总结这个文档", attachment)
        except Exception as exc:
            self._hint(f"（发送失败：{exc}）")
            return
        if not ok:
            self._hint("（我正忙着，稍后再试？）")


# Module-level singleton holder. The console window must survive after the
# caller (app._on_short_ask_requested) discards open_singleton's return value:
# a Python-only reference would let PySide6 garbage-collect the QMainWindow
# the moment the caller returns, so the window would flash and vanish with no
# traceback. Mirrors the legacy CompanionChatWindow._instance pattern.
_console_instance: CompanionConsole | None = None


def _bring_to_front(window: "CompanionConsole") -> None:
    """恢复（若最小化）并把窗口拉到顶层、抢到输入焦点。

    仅 ``raise_()``/``activateWindow()`` 不够：桌宠悬浮件不夺取焦点，
    点击 Ask 时前台仍属于别的应用，Windows 会拦截后台进程的焦点请求
    （只在任务栏闪烁）。以 Win32 ``SetForegroundWindow`` 兜底，配合
    一次瞬时 ALT 敲击满足系统的前台转移许可。
    """
    if window.isMinimized():
        window.showNormal()
    window.show()
    window.raise_()
    window.activateWindow()
    if sys.platform == "win32":
        try:
            import ctypes

            hwnd = int(window.winId())
            user32 = ctypes.windll.user32
            user32.keybd_event(0x12, 0, 0, 0)  # ALT down（仅此瞬间）
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(0x12, 0, 2, 0)  # ALT up
        except Exception:  # noqa: BLE001 - focus hint only, never fatal
            pass
    window.input.setFocus()


def open_singleton(runner: Any, runtime_bus: Any = None) -> "CompanionConsole":
    """Open (or focus) one console window for the shared character runner.

    The instance is held at module level (like the legacy
    ``CompanionChatWindow._instance``) so the window stays alive between Ask
    clicks; an already-open window is raised and focused instead of
    duplicated. Closing the window clears the holder so the next Ask opens a
    fresh instance with the same runner.

    ``runtime_bus`` is bound to the console at creation time only (an
    already-open console keeps its original bus binding).
    """
    global _console_instance
    window = _console_instance
    if window is not None:
        _bring_to_front(window)
        return window
    console = CompanionConsole(runner, runtime_bus=runtime_bus)
    _console_instance = console
    _bring_to_front(console)
    return console


def main() -> int:
    from ui.character_conversation_runner import CharacterConversationRunner

    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    theme.load_application_fonts()  # 科研级字体（assets/fonts/，幂等）
    # Standalone dev entry: fail-closed capability gates (no PluginLoader here).
    # Camera / video analysis stay OFF unless explicitly enabled in code —
    # never silently allow when Quick Tools would show them as Off.
    runner = CharacterConversationRunner(
        video_analysis_enabled=lambda: False,
        camera_vision_enabled=lambda: False,
    )
    console = CompanionConsole(runner)
    console.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
