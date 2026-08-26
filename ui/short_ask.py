"""Short Talk panel: compact, non-chat, AgentEvent-driven Short Ask (Phase 8C.3).

The panel is a small bounded surface — "Firefly is saying one thing to the
user" — never a chat window. It consumes only neutral AgentEvents (no provider
JSON names, no raw protocol) and owns a tiny state machine so the user always
has honest feedback while the provider streams: Connecting / Thinking /
Generating / first-token STREAMING / Complete / Error / Cancelled. Long answers
scroll inside a bounded viewport with follow-tail auto-scroll.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.agent_events import (
    STATUS_CONNECTING,
    STATUS_GENERATING,
    STATUS_ORGANIZING,
    STATUS_PROCESSING,
    STATUS_READING,
    STATUS_RUNNING_TOOL,
    STATUS_THINKING,
    AgentEvent,
    AgentEventType,
    ErrorCategory,
)

from . import theme
from .popover_base import PopoverBase


AGENT_DISPLAY = {
    "firefly": "Firefly",
    "claude": "Claude",
    "codex": "Codex",
    "chatgpt": "ChatGPT",
}
MAX_ANSWER_HEIGHT = 110

SLOW_AFTER_MS = 8_000  # no first token yet -> "Still working…"
VERY_SLOW_AFTER_MS = 20_000  # still no text -> light "Open Claude" action (never auto-cancel)
HARD_TIMEOUT_MS = 45_000  # Short Talk hard timeout -> "taking too long" + Retry/Open
CANCELLED_BACK_MS = 1_200  # "Cancelled" -> back to READY


class ShortTalkState(str, Enum):
    IDLE = "idle"
    READY = "ready"
    CONNECTING = "connecting"
    THINKING = "thinking"
    GENERATING = "generating"
    STREAMING = "streaming"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


_RUNNING_STATES = frozenset(
    {
        ShortTalkState.CONNECTING,
        ShortTalkState.THINKING,
        ShortTalkState.GENERATING,
        ShortTalkState.STREAMING,
        ShortTalkState.CANCELLING,
    }
)

# Neutral STATUS tokens -> faint waiting-stage text. Only honest states are
# shown; nothing here invents fake percentage progress.
_STATUS_DISPLAY = {
    STATUS_CONNECTING: "Connecting…",
    STATUS_THINKING: "Thinking…",
    STATUS_GENERATING: "Generating…",
    STATUS_ORGANIZING: "Working…",
    STATUS_READING: "Reading…",
    STATUS_PROCESSING: "Processing…",
    STATUS_RUNNING_TOOL: "Working…",
}

_ERROR_CATEGORY_DISPLAY = {
    ErrorCategory.AUTH: "Authentication issue",
    ErrorCategory.TIMEOUT: "Request timed out",
    ErrorCategory.TRANSPORT: "Connection problem",
    ErrorCategory.PROTOCOL: "Response format problem",
    ErrorCategory.PROVIDER: "Agent returned an error",
    ErrorCategory.PROCESS_START: "Failed to start agent",
    ErrorCategory.CANCELLED: "Cancelled",
    ErrorCategory.UNKNOWN: "Something went wrong",
}


class ScrollFollowTextBrowser(QTextBrowser):
    """Read-only, scrollable answer viewport with follow-tail auto-scroll.

    Streaming keeps the view pinned to the bottom only while the user is already
    there. Scrolling up pauses following; scrolling back to the bottom resumes
    it. A programmatic scroll never reads as a user scroll.
    """

    resized = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet(self._style())
        self._follow_tail = True
        self._guard = False
        self.verticalScrollBar().valueChanged.connect(self._on_value_changed)
        self.document().documentLayout().documentSizeChanged.connect(self._sync_height)

    def text(self) -> str:
        """QLabel-compatible accessor so existing callers/tests keep working."""
        return self.toPlainText()

    @property
    def follow_tail(self) -> bool:
        return self._follow_tail

    def reset(self) -> None:
        """Clear the viewport and resume follow-tail for a fresh turn."""
        self._follow_tail = True
        self.set_answer("")

    def set_answer(self, text: str) -> None:
        """Replace the whole answer and (when following) pin to the tail."""
        self._guard = True
        try:
            self.setPlainText(text or "")
        finally:
            self._guard = False
        if self._follow_tail:
            self.scroll_to_bottom()

    def append_answer(self, delta: str) -> None:
        """Append a streamed delta without disturbing an upward scroll."""
        bar = self.verticalScrollBar()
        previous = bar.value()
        # insertText moves the cursor to the end and scrolls it into view; guard
        # that internal scroll so it is never mistaken for a user scroll-up.
        self._guard = True
        try:
            cursor = self.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(delta)
            self.setTextCursor(cursor)
        finally:
            self._guard = False
        if self._follow_tail:
            self.scroll_to_bottom()
        else:
            # insertText scrolled the cursor into view; put the user back where
            # they were reading instead of yanking them to the tail.
            self._guard = True
            try:
                bar.setValue(previous)
            finally:
                self._guard = False

    def scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        self._guard = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._guard = False

    def _on_value_changed(self, value: int) -> None:
        if self._guard:
            return
        self._follow_tail = value >= self.verticalScrollBar().maximum()

    def _sync_height(self, size) -> None:
        # Compact for short answers (no empty viewport), bounded + scrollable
        # for long ones. +1 avoids truncating a fractional last line.
        height = min(int(size.height()) + 1, MAX_ANSWER_HEIGHT)
        self.setFixedHeight(max(height, 0))
        self.resized.emit()

    @staticmethod
    def _style() -> str:
        handle = theme.css_color((150, 180, 205, 90))
        handle_hover = theme.css_color((120, 160, 195, 150))
        return (
            f"QTextBrowser {{ background: transparent; border: none; "
            f"color: {theme.css_color(theme.TEXT_SECONDARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 9pt; }}"
            "QTextBrowser QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            f"QTextBrowser QScrollBar::handle:vertical {{ background: {handle}; border-radius: 3px; min-height: 20px; }}"
            f"QTextBrowser QScrollBar::handle:vertical:hover {{ background: {handle_hover}; }}"
            "QTextBrowser QScrollBar::add-line:vertical, QTextBrowser QScrollBar::sub-line:vertical { height: 0; }"
            "QTextBrowser QScrollBar::add-page:vertical, QTextBrowser QScrollBar::sub-page:vertical { background: transparent; }"
        )


class AskPill(QWidget):
    """Small clickable \"Ask…\" entry anchored to the greeting bubble.

    A separate top-level window on purpose: the bubble itself is input
    transparent (so it never blocks pet dragging), and Qt cannot make part of
    a window interactive while leaving the rest click-through.
    """

    ask_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle("Firefly Ask")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(theme.transparent_window_style())
        self.setFixedSize(84, 32)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("Ask")
        self.setMouseTracking(True)
        self._hovered = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._card = theme.GlassPanel(theme.RADIUS_ITEM, self)
        theme.apply_soft_shadow(self._card, blur=18, y_offset=3)
        layout.addWidget(self._card)

        inner = QHBoxLayout(self._card)
        inner.setContentsMargins(theme.SPACE_SM, 0, theme.SPACE_SM, 0)
        self._label = QLabel("Ask…")
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(theme.primary_label_style(size=9))
        inner.addWidget(self._label)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._label.setStyleSheet(
            f"color: {theme.css_color(theme.CYAN_ACCENT)}; background: {theme.TRANSPARENT}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: 9pt; font-weight: {theme.FONT_WEIGHT_MEDIUM};"
        )
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self._label.setStyleSheet(theme.primary_label_style(size=9))
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.ask_clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ShortAskPanel(PopoverBase):
    send_requested = Signal(str)
    force_send_requested = Signal(str)
    stop_requested = Signal()
    retry_requested = Signal()
    open_agent_requested = Signal(str)

    def __init__(self, *, width: int = theme.POPOVER_WIDTH, parent=None):
        super().__init__(width=width, parent=parent)
        self.setWindowTitle("Firefly Short Talk")
        # PopoverBase binds Escape -> dismiss via a QShortcut, which intercepts
        # the key before keyPressEvent. The panel needs Escape to stop a running
        # turn, so disable the shortcut and handle Escape ourselves.
        self._esc.setEnabled(False)
        self._agent: str = "claude"
        self._full_answer = ""
        self._state = ShortTalkState.IDLE
        self._session_ready = False
        self._pending_prompt = ""
        self._secondary_action = ""  # "" | "ask_again" | "open_agent"
        self._retry_active = False
        self._last_telemetry = None
        self._error_detail = ""
        self._completed_while_hidden = False

        self._slow_timer = QTimer(self)
        self._slow_timer.setSingleShot(True)
        self._slow_timer.timeout.connect(self._on_slow_timeout)
        self._very_slow_timer = QTimer(self)
        self._very_slow_timer.setSingleShot(True)
        self._very_slow_timer.timeout.connect(self._on_very_slow_timeout)
        self._hard_timeout_timer = QTimer(self)
        self._hard_timeout_timer.setSingleShot(True)
        self._hard_timeout_timer.timeout.connect(self._on_hard_timeout)
        self._cancelled_timer = QTimer(self)
        self._cancelled_timer.setSingleShot(True)
        self._cancelled_timer.timeout.connect(self._on_cancel_back_ready)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(0)
        self._resize_timer.timeout.connect(self.adjustSize)

        # Header: agent name + status.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_SM)
        self._title = QLabel("Ask")
        self._title.setStyleSheet(theme.primary_label_style(size=11))
        header.addWidget(self._title, 0, Qt.AlignVCenter)
        self._status = QLabel("")
        self._status.setStyleSheet(theme.secondary_label_style())
        header.addWidget(self._status, 1, Qt.AlignVCenter)
        self._close_btn = QPushButton("×")
        self._close_btn.setObjectName("shortAskClose")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setStyleSheet(theme.link_button_style("shortAskClose"))
        self._close_btn.clicked.connect(self.dismiss)
        header.addWidget(self._close_btn, 0, Qt.AlignVCenter)
        self.content_layout.addLayout(header)

        # Single-line input.
        self._input = QLineEdit()
        self._input.setPlaceholderText("Ask a short question…")
        self._input.setStyleSheet(
            f"QLineEdit {{ background: {theme.css_color(theme.GLASS_BACKGROUND)}; "
            f"border: 1px solid {theme.css_color(theme.GLASS_BORDER)}; "
            f"border-radius: {theme.RADIUS_ITEM}px; padding: {theme.SPACE_XS}px {theme.SPACE_SM}px; "
            f"color: {theme.css_color(theme.TEXT_PRIMARY)}; "
            f"font-family: '{theme.FONT_FAMILY}'; font-size: {theme.FONT_SIZE_BODY}pt; }}"
            f"QLineEdit:focus {{ border: 1px solid {theme.css_color(theme.CYAN_ACCENT)}; }}"
        )
        self._input.returnPressed.connect(self._on_submit)
        self._input.setEnabled(False)
        self.content_layout.addWidget(self._input)

        # Output: scrollable, height-bounded answer viewport.
        self._output = ScrollFollowTextBrowser()
        self._output.setVisible(False)
        self._output.resized.connect(self._resize_timer.start)
        self.content_layout.addWidget(self._output)

        # Actions row.
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(theme.SPACE_XS)
        actions.addStretch(1)
        self._secondary_btn = QPushButton("")
        self._secondary_btn.setObjectName("shortAskSecondary")
        self._secondary_btn.setCursor(Qt.PointingHandCursor)
        self._secondary_btn.setStyleSheet(theme.link_button_style("shortAskSecondary"))
        self._secondary_btn.setVisible(False)
        actions.addWidget(self._secondary_btn)
        self._primary_btn = QPushButton("")
        self._primary_btn.setObjectName("shortAskPrimary")
        self._primary_btn.setCursor(Qt.PointingHandCursor)
        self._primary_btn.setStyleSheet(theme.popover_button_style("shortAskPrimary"))
        self._primary_btn.setVisible(False)
        actions.addWidget(self._primary_btn)
        self.content_layout.addLayout(actions)

        self._secondary_btn.clicked.connect(self._on_secondary)
        self._primary_btn.clicked.connect(self._on_primary)

    # -- public API -----------------------------------------------------

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def running(self) -> bool:
        return self._state in _RUNNING_STATES

    @property
    def state(self) -> ShortTalkState:
        return self._state

    @property
    def session_ready(self) -> bool:
        return self._session_ready

    def show_input(self, agent: str, *, resume: bool = False) -> None:
        self._agent = agent
        self._state = ShortTalkState.READY
        self._full_answer = ""
        self._session_ready = False
        self._error_detail = ""
        self._completed_while_hidden = False
        self._last_telemetry = None
        self._stop_timers()
        self._title.setText(self._agent_title())
        self._status.setText("Resuming session" if resume else "New session")
        self._status.setToolTip("")
        self._output.setVisible(False)
        self._output.reset()
        self._input.setEnabled(True)
        self._input.clear()
        self._input.setFocus()
        self._primary_btn.setVisible(False)
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self.adjustSize()
        self.show()
        self.raise_()

    def show_notice(self, agent: str, message: str, *, open_label: str) -> None:
        """Unavailable-agent notice: no backend is invoked."""
        self._agent = agent
        self._state = ShortTalkState.READY
        self._full_answer = ""
        self._session_ready = False
        self._error_detail = ""
        self._completed_while_hidden = False
        self._last_telemetry = None
        self._stop_timers()
        self._title.setText(self._agent_title())
        self._status.setText(message)
        self._status.setToolTip("")
        self._output.setVisible(False)
        self._input.setEnabled(False)
        self._input.clear()
        self._primary_btn.setText(open_label)
        self._primary_btn.setVisible(True)
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self.adjustSize()
        self.show()
        self.raise_()

    def show_recommendation(self, agent: str, message: str, *, open_label: str, prompt: str = "") -> None:
        """Complex/long-prompt steering: recommend the native agent."""
        self._agent = agent
        self._pending_prompt = prompt or ""
        self._state = ShortTalkState.READY
        self._full_answer = ""
        self._session_ready = False
        self._error_detail = ""
        self._completed_while_hidden = False
        self._last_telemetry = None
        self._stop_timers()
        self._title.setText(self._agent_title())
        self._status.setText(message)
        self._status.setToolTip("")
        self._output.setVisible(False)
        self._input.setEnabled(False)
        self._input.clear()
        self._primary_btn.setText(open_label)
        self._primary_btn.setVisible(True)
        self._secondary_btn.setText("Ask anyway")
        self._secondary_btn.setVisible(True)
        self._secondary_action = "ask_again"
        self.adjustSize()
        self.show()
        self.raise_()

    def set_running(self, status_text: str = "Connecting…") -> None:
        self._state = ShortTalkState.CONNECTING
        self._full_answer = ""
        self._session_ready = False
        self._error_detail = ""
        self._completed_while_hidden = False
        self._last_telemetry = None
        self._retry_active = False
        self._start_slow_timers()
        self._status.setText(status_text or "Connecting…")
        self._status.setToolTip("")
        self._output.setVisible(True)
        self._output.reset()
        self._input.setEnabled(False)
        self._primary_btn.setText("Stop")
        self._primary_btn.setVisible(True)
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self.adjustSize()

    def set_status(self, status_text: str) -> None:
        """Update the status line without touching the streamed answer."""
        self._status.setText(status_text)

    def add_answer(self, delta: str) -> None:
        if not delta:
            return
        if self._state not in _RUNNING_STATES or self._state == ShortTalkState.CANCELLING:
            return  # ignore late deltas after the turn ended
        self._mark_first_text()
        self._full_answer += delta
        self._output.append_answer(delta)

    def set_answer(self, text: str) -> None:
        self._full_answer = text or ""
        self._output.set_answer(self._full_answer)

    def show_done(self) -> None:
        self._state = ShortTalkState.COMPLETE
        if not self.isVisible():
            self._completed_while_hidden = True
        self._stop_timers()
        self._refresh_done()
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self._output.set_answer(self._full_answer)
        self._input.setEnabled(True)
        self.adjustSize()

    def show_error(self, message: str, *, open_label: str | None = None) -> None:
        self._error_from(message or "", ErrorCategory.UNKNOWN, open_label=open_label)

    def on_agent_event(self, ev) -> None:
        """Single consumer of neutral AgentEvents for this turn."""
        if not isinstance(ev, AgentEvent):
            return
        t = ev.type
        if t == AgentEventType.SESSION:
            self._session_ready = bool(ev.session_id)
        elif t == AgentEventType.STARTED:
            if self._state == ShortTalkState.READY:
                self._state = ShortTalkState.CONNECTING
                self._start_slow_timers()
        elif t == AgentEventType.STATUS:
            self._on_status_token(ev.status)
        elif t == AgentEventType.TEXT_DELTA:
            self.add_answer(ev.text)
        elif t == AgentEventType.FINAL:
            self._on_final(ev.text)
        elif t == AgentEventType.ERROR:
            self._error_from(ev.text or "", ev.error_code or ErrorCategory.UNKNOWN)
        elif t == AgentEventType.CANCELLED:
            self._finish_cancel()

    def begin_cancel(self) -> None:
        """User pressed Stop / Esc: show honest Cancelling… until transport CANCELLED."""
        if not self.running or self._state == ShortTalkState.CANCELLING:
            return
        self._state = ShortTalkState.CANCELLING
        self._status.setText("Cancelling…")
        self.adjustSize()

    def on_cancelled(self) -> None:
        self._finish_cancel()

    def finish_turn(self, text: str) -> None:
        """Process fully finished. Idempotent with the event-driven path."""
        if self._state == ShortTalkState.CANCELLING:
            self._finish_cancel()
            return
        if self._state in (ShortTalkState.COMPLETE, ShortTalkState.ERROR, ShortTalkState.CANCELLED):
            self._input.setEnabled(True)
            return
        if text:
            self.set_answer(text)
        self.show_done()

    def reset_with_note(self, note: str) -> None:
        if self._state == ShortTalkState.ERROR:
            return  # timeout/error already failed the turn; keep the failure UI
        self._state = ShortTalkState.READY
        self._completed_while_hidden = False
        self._stop_timers()
        self._status.setText(note)
        self._status.setToolTip("")
        self._output.setVisible(False)
        self._input.setEnabled(True)
        self._primary_btn.setVisible(False)
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self.adjustSize()

    def on_telemetry(self, telemetry) -> None:
        self._last_telemetry = telemetry
        if self._state == ShortTalkState.COMPLETE:
            self._refresh_done()

    def suspend(self) -> None:
        """Hide but keep the live turn state (PermissionCard / popover priority)."""
        self.hide()

    def resume_show(self) -> None:
        """Re-show with the preserved turn state; next open starts fresh."""
        self._completed_while_hidden = False
        self.adjustSize()
        self.show()
        self.raise_()
        if self._state == ShortTalkState.READY and self._input.isEnabled():
            self._input.setFocus()

    def has_pending_state(self) -> bool:
        """True when a live/hidden turn should be resumed instead of re-initialized."""
        if self.running:
            return True
        return self._completed_while_hidden

    def is_cancelling(self) -> bool:
        return self._state == ShortTalkState.CANCELLING

    def open_label(self) -> str:
        return self._open_label()

    def full_answer(self) -> str:
        return self._full_answer

    def stop(self) -> None:
        # Legacy alias; the transport must drive the real cancel completion.
        self.begin_cancel()

    def reset(self) -> None:
        self._state = ShortTalkState.READY
        self._completed_while_hidden = False
        self._stop_timers()
        self._full_answer = ""
        self._session_ready = False
        self._error_detail = ""
        self._status.setText("")
        self._status.setToolTip("")
        self._output.setVisible(False)
        self._output.reset()
        self._input.clear()
        self._input.setEnabled(True)
        self._primary_btn.setVisible(False)
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self.adjustSize()

    # -- internals ------------------------------------------------------

    def _agent_title(self) -> str:
        return f"Ask {AGENT_DISPLAY.get(self._agent, self._agent.title())}"

    def _open_label(self) -> str:
        return f"Open {AGENT_DISPLAY.get(self._agent, self._agent.title())}"

    def _mark_first_text(self) -> None:
        # First token arrived: the "still working" / "open agent" progress cues
        # are done, but the hard deadline stays armed. The hard timeout is a
        # wall-clock deadline measured from Send, never a "no first token yet"
        # timer, so a turn that streams a little then stalls still times out.
        self._slow_timer.stop()
        self._very_slow_timer.stop()
        if self._state != ShortTalkState.STREAMING:
            self._state = ShortTalkState.STREAMING
            self._status.setText("")

    def _on_status_token(self, token: str | None) -> None:
        if self._state in (
            ShortTalkState.STREAMING,
            ShortTalkState.COMPLETE,
            ShortTalkState.ERROR,
            ShortTalkState.CANCELLED,
            ShortTalkState.CANCELLING,
        ):
            return
        if token == STATUS_THINKING:
            self._state = ShortTalkState.THINKING
            self._status.setText("Thinking…")
        elif token == STATUS_GENERATING:
            self._state = ShortTalkState.GENERATING
            self._status.setText("Generating…")
        elif token == STATUS_CONNECTING:
            # Never regress once the turn is actually working: a late
            # "connecting" system event must not replace the reliable state.
            if self._state in (ShortTalkState.READY, ShortTalkState.CONNECTING):
                self._state = ShortTalkState.CONNECTING
                self._status.setText("Connecting…")
        elif token == "reconnecting":
            self._status.setText("Reconnecting…")
        elif token in _STATUS_DISPLAY:
            self._status.setText(_STATUS_DISPLAY[token])

    def _on_final(self, text: str | None) -> None:
        if self._state not in _RUNNING_STATES or self._state == ShortTalkState.CANCELLING:
            return
        self._stop_timers()
        if text:
            self.set_answer(text)
        self.show_done()

    def _error_from(self, detail: str, category: ErrorCategory, *, open_label: str | None = None) -> None:
        if self._state in (ShortTalkState.COMPLETE, ShortTalkState.CANCELLED):
            return
        if self._state == ShortTalkState.ERROR:
            return  # a late error after timeout/error must not override the state
        self._state = ShortTalkState.ERROR
        self._error_detail = detail
        if not self.isVisible():
            self._completed_while_hidden = True
        self._stop_timers()
        self._status.setText(_ERROR_CATEGORY_DISPLAY.get(category, _ERROR_CATEGORY_DISPLAY[ErrorCategory.UNKNOWN]))
        self._status.setToolTip(detail[:160] if detail else "")
        self._output.setVisible(True)
        self._output.set_answer(self._full_answer)
        self._primary_btn.setText(open_label or self._open_label())
        self._primary_btn.setVisible(True)
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self._input.setEnabled(True)
        self.adjustSize()

    def _finish_cancel(self) -> None:
        if self._state == ShortTalkState.ERROR:
            return  # a late cancel after timeout/error must not override the state
        self._state = ShortTalkState.CANCELLED
        if not self.isVisible():
            self._completed_while_hidden = True
        self._stop_timers()
        self._status.setText("Cancelled")
        self._status.setToolTip("")
        self._primary_btn.setVisible(False)
        self._secondary_btn.setVisible(False)
        self._secondary_action = ""
        self._input.setEnabled(True)
        self.adjustSize()
        self._cancelled_timer.start(CANCELLED_BACK_MS)

    def _refresh_done(self) -> None:
        self._status.setText(f"Done{self._duration_text()}")
        self._primary_btn.setVisible(False)
        first = self._last_telemetry.first_text_ms() if self._last_telemetry is not None else None
        total = self._last_telemetry.total_ms() if self._last_telemetry is not None else None
        if first is not None and total is not None:
            self._status.setToolTip(f"First response: {first / 1000:.1f}s · Total: {total / 1000:.1f}s")
        else:
            self._status.setToolTip("")

    def _duration_text(self) -> str:
        total = self._last_telemetry.total_ms() if self._last_telemetry is not None else None
        if total is None:
            return ""
        return f" · {total / 1000:.1f}s"

    def _start_slow_timers(self) -> None:
        self._slow_timer.start(SLOW_AFTER_MS)
        self._very_slow_timer.start(VERY_SLOW_AFTER_MS)
        self._hard_timeout_timer.start(HARD_TIMEOUT_MS)

    def _stop_timers(self) -> None:
        self._slow_timer.stop()
        self._very_slow_timer.stop()
        self._hard_timeout_timer.stop()
        self._cancelled_timer.stop()

    def _on_slow_timeout(self) -> None:
        if self._state in _RUNNING_STATES and self._state not in (
            ShortTalkState.CANCELLING,
            ShortTalkState.STREAMING,
        ):
            self._status.setText("Still working…")

    def _on_very_slow_timeout(self) -> None:
        if self._state in _RUNNING_STATES and self._state not in (
            ShortTalkState.CANCELLING,
            ShortTalkState.STREAMING,
        ):
            if self._agent == "firefly":
                return
            self._secondary_btn.setText(self._open_label())
            self._secondary_btn.setVisible(True)
            self._secondary_action = "open_agent"

    def _on_hard_timeout(self) -> None:
        """Short Talk hard timeout: stop the turn and offer Retry / Open."""
        if self._state not in _RUNNING_STATES or self._state == ShortTalkState.CANCELLING:
            return
        self._stop_timers()
        self._state = ShortTalkState.ERROR
        self._retry_active = True
        display = AGENT_DISPLAY.get(self._agent, self._agent.title())
        self._status.setText(f"{display} is taking too long.")
        self._status.setToolTip("")
        self._primary_btn.setText("Retry")
        self._primary_btn.setVisible(True)
        if self._agent == "firefly":
            self._secondary_btn.setVisible(False)
            self._secondary_action = ""
        else:
            self._secondary_btn.setText(self._open_label())
            self._secondary_btn.setVisible(True)
            self._secondary_action = "open_agent"
        self._input.setEnabled(True)
        self.adjustSize()
        self.stop_requested.emit()

    def _on_cancel_back_ready(self) -> None:
        self._state = ShortTalkState.READY
        self._status.setText("")
        self.adjustSize()

    def _on_submit(self) -> None:
        prompt = self._input.text().strip()
        if not prompt or self.running:
            return
        self._pending_prompt = ""
        self.send_requested.emit(prompt)

    def _on_primary(self) -> None:
        if self.running:
            self.stop_requested.emit()
            self.begin_cancel()
            return
        if self._retry_active:
            self._retry_active = False
            self.retry_requested.emit()
            return
        self.open_agent_requested.emit(self._agent)

    def _on_secondary(self) -> None:
        if self._secondary_action == "ask_again" and self._pending_prompt:
            prompt = self._pending_prompt
            self._pending_prompt = ""
            self.force_send_requested.emit(prompt)
            return
        if self._secondary_action == "open_agent":
            self.open_agent_requested.emit(self._agent)
            return
        if not self.running:
            self.show_input(self._agent)

    def dismiss(self) -> None:
        """Dismiss; a running turn is cancelled (explicit user action)."""
        if self.running:
            self.stop_requested.emit()
            self.begin_cancel()
        super().dismiss()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            if self.running:
                self.stop_requested.emit()
                self.begin_cancel()
                event.accept()
                return
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)
