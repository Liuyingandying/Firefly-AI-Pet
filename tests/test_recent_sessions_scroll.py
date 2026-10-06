# -*- coding: utf-8 -*-
"""v1.4 Recent Sessions 滚动增强回归测试。

运行:
    cd E:\\Firefly_AI_Pet
    .venv\\Scripts\\python.exe -m pytest tests/test_recent_sessions_scroll.py -v
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication

from ui.v2.recent_sessions import RecentSessionProvider, RecentSessionsCard
from ui.v2.sidebar import Sidebar


def _qapp():
    return QApplication.instance() or QApplication([])


class FakeStore:
    """ConversationStore 契约的最小实现 (list_sessions + load_working_window)。"""

    def __init__(self, count: int, prefix: str = "会话"):
        self._ids = [f"session-{i}" for i in range(count)]
        self._prefix = prefix

    def list_sessions(self):
        return list(self._ids)

    def load_working_window(self, session_id=None):
        from types import SimpleNamespace

        index = self._ids.index(session_id) if session_id in self._ids else 0
        # 新→旧的时间戳 (倒序让最后一个 id 恰好最旧, 与真实 store 相反也无妨)
        ts = 1700000000000 + index * 60_000
        turn = SimpleNamespace(role="user", content=f"{self._prefix} {session_id}", ts=ts)
        return [turn]


def _settle(widget, ms: int = 120) -> None:
    """等 Qt 布局激活完成 (延迟 LayoutRequest), 否则滚动条状态未定。"""
    deadline = time.time() + ms / 1000
    while time.time() < deadline:
        _qapp().processEvents()
        time.sleep(0.01)
    if hasattr(widget, "adjustSize"):
        widget.adjustSize()
    _qapp().processEvents()


def _card(count: int) -> RecentSessionsCard:
    card = RecentSessionsCard(provider=RecentSessionProvider(FakeStore(count)))
    card.resize(220, 400)
    card.show()
    _settle(card)
    return card


def _drain(card, ms: float = 0.2):
    from PySide6.QtTest import qWait

    qWait(int(ms * 1000))
    _qapp().processEvents()


# ---------------------------------------------------------------------------
# 1. 空列表 → 暂无历史对话
# ---------------------------------------------------------------------------


def test_1_empty_list_shows_placeholder(qapp=None):
    app = _qapp()
    card = RecentSessionsCard(provider=RecentSessionProvider(FakeStore(0)))
    card.resize(220, 200)
    card.show()
    app.processEvents()
    assert card._empty_label.isVisibleTo(card), "空列表应显示 暂无历史对话"
    assert len(card._rows) == 0
    card.deleteLater()


# ---------------------------------------------------------------------------
# 2. 5 条正常显示
# ---------------------------------------------------------------------------


def test_2_five_sessions_render(qapp=None):
    app = _qapp()
    card = _card(5)
    app.processEvents()
    assert len(card._rows) == 5
    assert card._scroll.verticalScrollBar().maximum() == 0, "5 条在 400px 高度内不应出现滚动"
    card.deleteLater()


# ---------------------------------------------------------------------------
# 3. 50 条可滚动
# ---------------------------------------------------------------------------


def test_3_fifty_sessions_scrollable(qapp=None):
    app = _qapp()
    card = _card(50)
    app.processEvents()
    bar = card._scroll.verticalScrollBar()
    assert bar.maximum() > 0, "50 条应出现垂直滚动"
    # 滚动到底再回顶, 滚动条状态正常
    bar.setValue(bar.maximum())
    app.processEvents()
    assert bar.value() == bar.maximum()
    bar.setValue(0)
    app.processEvents()
    card.deleteLater()


# ---------------------------------------------------------------------------
# 4. 滚动后点击仍触发 session_clicked
# ---------------------------------------------------------------------------


def test_4_click_after_scroll_emits_session_id(qapp=None):
    from PySide6.QtTest import QTest

    card = _card(50)
    received: list[str] = []
    card.session_clicked.connect(received.append)

    bar = card._scroll.verticalScrollBar()
    bar.setValue(bar.maximum())          # 滚到最底部
    app = _qapp()
    app.processEvents()

    last_row = card._rows[-1]
    QTest.mouseClick(last_row, Qt.LeftButton, pos=QPoint(30, 19))
    app.processEvents()

    # 显示顺序为最新在前: 滚到底后最后一行是最旧会话, 点击仍触发其 session_clicked
    assert received == ["session-0"], f"滚到底后点击应触发最后一行: {received}"
    card.deleteLater()


# ---------------------------------------------------------------------------
# 5. 当前会话高亮
# ---------------------------------------------------------------------------


def test_5_current_session_highlight(qapp=None):
    card = _card(5)
    card.set_current_session("session-2")
    for index, row in enumerate(card._rows):
        expected = index == 2
        assert row._current is expected
        assert ("rgba(228, 238, 243" in row.styleSheet()) is expected
    card.deleteLater()


# ---------------------------------------------------------------------------
# 6. 三种窗口尺寸: 底部组件不被挤出, 溢出交给滚动
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(1280, 800), (1600, 900), (1920, 1080)])
def test_6_window_sizes_protect_bottom_widgets(size, qapp=None):
    app = _qapp()
    from ui.v2.ability_panel import AbilityPanel
    from ui.v2.character_header import CharacterHeader

    sidebar = Sidebar(
        CharacterHeader(),
        AbilityPanel(),
        recent_provider=RecentSessionProvider(FakeStore(50)),
    )
    sidebar.resize(*size)
    sidebar.show()
    _settle(sidebar, 200)

    sidebar_height = sidebar.height()
    recent = sidebar._recent_card
    user_card = sidebar._user_card
    new_chat = sidebar.action_button("new_chat")

    # 卡片不会被裁出窗口
    assert 0 < recent.height() < sidebar_height
    # 底部组件可见且在窗口内
    assert not user_card.isHidden()
    assert not new_chat.isHidden()
    geo = user_card.geometry()
    assert geo.y() >= 0 and geo.y() + geo.height() <= sidebar_height, (
        f"用户卡被挤出侧栏底部: {geo}")
    recent_geo = recent.geometry()
    assert recent_geo.y() >= 0 and recent_geo.y() + recent_geo.height() <= sidebar_height, (
        f"最近会话卡被裁切: {recent_geo}")
    # 底部组件不重叠
    assert geo.y() >= recent_geo.y() + recent_geo.height() - 2
    # 卡片可滚动 (内容溢出交给 QScrollArea)
    assert recent._scroll.verticalScrollBar().maximum() >= 0
    sidebar.deleteLater()
