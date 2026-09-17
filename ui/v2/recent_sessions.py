"""RecentSessionsCard — ChatGPT-style recent conversation list for the sidebar.

Phase UI-2: real-data driven. ``RecentSessionProvider`` reads the existing
``ConversationStore`` (session ids + working windows) when one is injected, and
falls back to a UI mock list otherwise. The card stays a dumb view: it renders
icon + title + relative time and emits ``session_clicked(session_id)``. No chat
storage, learning mode, backend or account system is touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui import theme

MOCK_SESSIONS = (
    ("自动控制原理", "今天"),
    ("物理光学", "昨天"),
    ("解读并联谐振公式", "3天前"),
    ("输出今日材料", "3天前"),
    ("对比5打2和2S", "5天前"),
)

_TITLE_MAX_CHARS = 18


class RecentSessionProvider:
    """Real recent-session source over the existing ``ConversationStore``.

    ``store`` is any object exposing ``list_sessions()`` +
    ``load_working_window(session_id)`` (the ConversationStore contract). When
    ``store`` is None, or reading it raises, the mock list is returned so the
    UI always renders. When the store is present but empty, ``[]`` is returned
    (the UI then shows its empty state, never the mock).
    """

    def __init__(self, store: Any | None = None) -> None:
        self.store = store

    def get_recent_sessions(self, limit: int = 5) -> list[dict[str, str]]:
        if self.store is None:
            return self._mock_sessions()
        try:
            entries: list[dict[str, Any]] = []
            for session_id in self.store.list_sessions():
                turns = self.store.load_working_window(session_id=session_id)
                entries.append(
                    {
                        "id": session_id,
                        "title": _session_title(self.store, session_id, turns),
                        "time": _derive_time(turns),
                        "_ts": _latest_ts(turns),
                    }
                )
            # newest last-turn first (never the database insertion order)
            entries.sort(key=lambda item: item["_ts"], reverse=True)
            return [
                {"id": item["id"], "title": item["title"], "time": item["time"]}
                for item in entries[:limit]
            ]
        except Exception:  # noqa: BLE001 - a broken store must never break the UI
            return self._mock_sessions()

    def session_display_title(self, session_id: str) -> str:
        """Display title of one session (rename-dialog default)."""
        if self.store is None:
            return ""
        try:
            turns = self.store.load_working_window(session_id=session_id)
            return _session_title(self.store, session_id, turns)
        except Exception:  # noqa: BLE001
            return ""

    def _mock_sessions(self) -> list[dict[str, str]]:
        return [
            {"id": f"mock-{index}", "title": title, "time": time}
            for index, (title, time) in enumerate(MOCK_SESSIONS)
        ]


def _derive_title(turns) -> str:
    """First user turn's text (no AI, no summary); ``新对话`` when none."""
    for turn in turns:
        if getattr(turn, "role", None) == "user":
            content = (getattr(turn, "content", "") or "").strip()
            if content:
                return _truncate(content)
    return "新对话"


def _session_title(store: Any, session_id: str, turns) -> str:
    """Display title: explicit custom title → first user turn → ``新对话``."""
    getter = getattr(store, "get_session_title", None)
    if callable(getter):
        try:
            explicit = getter(session_id)
        except Exception:  # noqa: BLE001 - degraded title derivation
            explicit = None
        if explicit:
            return _truncate(explicit)
    return _derive_title(turns)


def _derive_time(turns) -> str:
    """Relative time from the newest turn's ``ts`` (milliseconds)."""
    latest = _latest_ts(turns)
    return _format_relative(latest) if latest > 0 else ""


def _latest_ts(turns) -> int:
    """Newest turn timestamp (milliseconds); 0 when there are no turns."""
    latest = 0
    for turn in turns:
        ts = getattr(turn, "ts", None) or 0
        if ts > latest:
            latest = ts
    return latest


def _truncate(text: str, max_chars: int = _TITLE_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def _format_relative(ts_ms: int, now: datetime | None = None) -> str:
    """Human relative time from a millisecond timestamp (stdlib only).

    ``now`` is injectable for deterministic tests; production uses the wall
    clock.
    """
    dt = datetime.fromtimestamp(ts_ms / 1000)
    ref = now or datetime.now()
    diff_s = max(0, (ref - dt).total_seconds())
    if diff_s < 60:
        return "刚刚"
    if diff_s < 3600:
        return f"{int(diff_s // 60)}分钟前"
    if dt.date() == ref.date():
        return "今天 " + dt.strftime("%H:%M")
    if dt.date() == (ref.date() - timedelta(days=1)):
        return "昨天 " + dt.strftime("%H:%M")
    return dt.strftime("%Y-%m-%d")


_TITLE_STYLE = (
    f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent;"
    f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
)
_TIME_STYLE = (
    f"color: rgba{theme.V2.TEXT_SECONDARY}; background: transparent;"
    f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
)


class _SessionRow(QFrame):
    """One clickable row: icon + title (left) and relative time (right).

    A QFrame with manual hover/press styling (QFrame has no QSS :hover), kept
    intentionally tiny so no third-party widget is introduced. Right-click
    opens a lightweight 重命名/删除 menu; the card only relays the ids.
    """

    clicked = Signal(str)  # session_id
    rename_requested = Signal(str)  # session_id
    delete_requested = Signal(str)  # session_id

    def __init__(
        self,
        session_id: str,
        title: str,
        time: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_id = session_id
        self._title = title
        self._hover = False
        self._current = False
        self.setObjectName("sessionRow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(38)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(6)

        title_label = QLabel(f"📄 {title}")
        title_label.setStyleSheet(_TITLE_STYLE)
        title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        time_label = QLabel(time)
        time_label.setStyleSheet(_TIME_STYLE)
        time_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        layout.addWidget(title_label, 1)
        layout.addWidget(time_label)
        self._apply_style()

    def _apply_style(self) -> None:
        if self._current:
            background = "rgba(228, 238, 243, 0.9)"  # 当前会话 · 浅青蓝高亮
        elif self._hover:
            background = "rgba(255, 255, 255, 0.5)"
        else:
            background = "transparent"
        self.setStyleSheet(
            f"QFrame#sessionRow {{ background: {background}; border-radius: 8px; }}"
        )

    def set_current(self, current: bool) -> None:
        """Mark this row as the active session (visual highlight only)."""
        self._current = bool(current)
        self._apply_style()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._hover = False
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._session_id)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt override
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: rgba{theme.V2.CARD_BG};"
            f" border: 1px solid rgba{theme.V2.BORDER_SOFT}; border-radius: 10px;"
            f" padding: 4px; font-family: {theme.V2_FONT_STACK};"
            f" font-size: {theme.V2.FONT_BODY}pt; color: rgba{theme.V2.TEXT_MAIN}; }}"
            f"QMenu::item {{ padding: 5px 16px; border-radius: 6px; }}"
            f"QMenu::item:selected {{ background: rgba{theme.V2.CARD_BG_USER}; }}"
        )
        rename_action = menu.addAction("重命名")
        delete_action = menu.addAction("删除")
        chosen = menu.exec(event.globalPos())
        if chosen is rename_action:
            self.rename_requested.emit(self._session_id)
        elif chosen is delete_action:
            self.delete_requested.emit(self._session_id)


class RecentSessionsCard(QFrame):
    """Display-only list of recent sessions (icon + title + relative time)."""

    session_clicked = Signal(str)  # session_id
    rename_requested = Signal(str)  # session_id
    delete_requested = Signal(str)  # session_id

    def __init__(
        self,
        provider: RecentSessionProvider | None = None,
        parent: QWidget | None = None,
        limit: int = 200,
    ) -> None:
        super().__init__(parent)
        self._provider = provider or RecentSessionProvider()
        self._limit = max(1, int(limit))
        self._current_session_id: str | None = None
        self._rows: list[_SessionRow] = []
        self.setObjectName("recentSessionsCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(120)
        self.setStyleSheet(
            f"#recentSessionsCard {{"
            f"  background: rgba{theme.V2.CARD_BG};"
            f"  border: 1px solid rgba(0, 0, 0, 0.05);"
            f"  border-radius: 16px;"
            f"}}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(4)

        header = QLabel("最近")
        header.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_SECONDARY}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_CAPTION}pt;"
        )
        root.addWidget(header)

        self._empty_label = QLabel("暂无历史对话")
        self._empty_label.setStyleSheet(_TIME_STYLE)
        self._empty_label.setVisible(False)
        root.addWidget(self._empty_label)

        # v1.4: 会话行放进可滚动容器 — 数量多时卡片内部滚动, 不再裁切/撑爆侧栏
        self._rows_container = QWidget(self)
        self._list = QVBoxLayout(self._rows_container)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(2)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(0, 0, 0, 0.15);"
            " border-radius: 3px; min-height: 24px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            " { background: transparent; }"
        )
        self._scroll.setWidget(self._rows_container)
        root.addWidget(self._scroll, 1)

        self.reload()

    def reload(self) -> None:
        """Rebuild the rows from the provider (clear + repopulate)."""
        while self._list.count():
            item = self._list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = []
        sessions = self._provider.get_recent_sessions(self._limit)
        self._empty_label.setVisible(not sessions)
        for session in sessions:
            row = _SessionRow(session["id"], session["title"], session["time"], self)
            row.clicked.connect(self.session_clicked)
            row.rename_requested.connect(self.rename_requested)
            row.delete_requested.connect(self.delete_requested)
            self._list.addWidget(row)
            self._rows.append(row)
        self._apply_current()

    def set_current_session(self, session_id: str | None) -> None:
        """Mark the given session as active (visual highlight only)."""
        self._current_session_id = session_id
        self._apply_current()

    def _apply_current(self) -> None:
        for row in self._rows:
            row.set_current(row._session_id == self._current_session_id)


class UserInfoCard(QFrame):
    """Avatar + username + plan badge (display-only; no account system)."""

    def __init__(
        self,
        username: str | None = None,
        plan: str = "Plus",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._username = (username or _current_username()).strip() or "用户"
        self.setObjectName("userInfoCard")
        self.setStyleSheet(
            f"#userInfoCard {{"
            f"  background: transparent; border: none; border-radius: 16px;"
            f"}}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(8)

        avatar = QLabel()
        avatar.setFixedSize(28, 28)
        avatar.setStyleSheet(
            f"background: qradialgradient(cx:0.4, cy:0.35, radius:1.0, "
            f"fx:0.4, fy:0.35, stop:0 rgba{theme.V2.CARD_BG_USER}, "
            f"stop:1 rgba{theme.V2.ACCENT_PURPLE});"
            f"border: 1px solid rgba{theme.V2.BORDER_SOFT}; border-radius: 14px;"
        )

        name_label = QLabel(self._username)
        name_label.setStyleSheet(
            f"color: rgba{theme.V2.TEXT_MAIN}; background: transparent;"
            f"font-family: {theme.V2_FONT_STACK}; font-size: {theme.V2.FONT_BODY}pt;"
            f"font-weight: 600;"
        )

        plan_badge = QLabel(plan)
        plan_badge.setStyleSheet(
            f"color: rgba{theme.V2.PRIMARY_BLUE}; background: rgba{theme.V2.CARD_BG_USER};"
            f"border: 1px solid rgba{theme.V2.BORDER_SOFT}; border-radius: 9px;"
            f"padding: 1px 8px; font-family: {theme.V2_FONT_STACK};"
            f"font-size: {theme.V2.FONT_CAPTION}pt;"
        )

        layout.addWidget(avatar)
        layout.addWidget(name_label)
        layout.addStretch(1)
        layout.addWidget(plan_badge)


def _current_username() -> str | None:
    """Best-effort OS username (display-only); never touches any account system."""
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 - degrade to the generic fallback
        return None


__all__ = [
    "RecentSessionProvider",
    "RecentSessionsCard",
    "UserInfoCard",
    "MOCK_SESSIONS",
]
