"""UI V2 companion console tests: widgets + wiring (offscreen Qt)."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QObject, QEventLoop, QTimer, Signal
from PySide6.QtWidgets import QLabel, QTextBrowser

from core.agent_events import AgentEvent, AgentEventType
from ui.v2.ability_panel import AbilityPanel, CAPABILITIES
from ui.v2.chat_view import ChatView
from ui.v2.character_header import CharacterHeader
from ui.v2.console import CompanionConsole
from ui.v2.video_card import VideoCard, VideoCardInfo


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


# ---------------------------------------------------------------------------
# CharacterHeader
# ---------------------------------------------------------------------------

def test_header_state_task_ability():
    header = CharacterHeader()
    header.set_state("working")
    assert header.state == "working"
    header.set_state("success")
    header.set_state("idle")
    header.set_task("正在总结 Docker 视频…" * 5)  # long text gets trimmed
    assert len(header._task) <= 91
    header.set_ability("视频阅读")
    assert "视频阅读" in header._ability_label.text()


# ---------------------------------------------------------------------------
# AbilityPanel
# ---------------------------------------------------------------------------

def test_ability_panel_emits_capability():
    panel = AbilityPanel()
    emitted: list[str] = []
    panel.requested.connect(emitted.append)
    panel.button("study").click()
    assert emitted == ["study"]
    assert {c for c, _, _ in CAPABILITIES} == {
        "video", "study", "screen_vision", "document", "research", "settings",
    }


# ---------------------------------------------------------------------------
# VideoCard
# ---------------------------------------------------------------------------

def test_video_card_renders_info_and_signals():
    info = VideoCardInfo(
        bvid="BV1s54y1n7Ev", title="Docker 10分钟快速入门",
        owner="Ross-Ning", duration="11:00",
        read_status="已阅读 · 237 段转录", study_stage="watching",
        url="https://www.bilibili.com/video/BV1s54y1n7Ev",
    )
    card = VideoCard(info)
    signals: list[str] = []
    card.continue_study.connect(lambda: signals.append("study"))
    card.timestamp_qa.connect(lambda: signals.append("ts"))
    card.open_video.connect(lambda: signals.append("open"))

    from PySide6.QtWidgets import QPushButton

    buttons = card.findChildren(QPushButton)
    assert len(buttons) == 3
    for button in buttons:
        button.click()
    assert signals == ["study", "ts", "open"]

    texts = " ".join(label.text() for label in card.findChildren(QLabel))
    assert "已阅读 · 237 段转录" in texts
    assert "学习模式 · 可考我" in texts


# ---------------------------------------------------------------------------
# ChatView
# ---------------------------------------------------------------------------

def test_chat_view_bubbles_and_card():
    view = ChatView()
    view.append_user("你好")
    view.append_assistant("**你好呀**")
    assert view._column.count() == 3  # stretch + two bubble wrappers
    card = VideoCard(VideoCardInfo(bvid="B", title="T"))
    view.append_card(card)
    assert view._column.count() == 4  # card appended
    view.set_status("思考中…")
    assert not view._status_chip.isHidden()  # visible flag independent of window
    view.clear()
    assert view._column.count() == 1  # only the stretch remains


def test_chat_view_follows_new_question_and_feedback(_qapp):
    view = ChatView()
    view.resize(620, 220)
    view.show()
    for index in range(8):
        view.append_assistant(f"旧消息 {index}：" + "内容" * 30)
    _qapp.processEvents()
    bar = view._scroll.verticalScrollBar()
    bar.setValue(0)

    view.append_assistant("题干\nA. 选项一\nB. 选项二")
    _qapp.processEvents()
    assert bar.maximum() > 0
    assert bar.value() == bar.maximum()

    bar.setValue(0)
    view.append_assistant("回答正确。")
    _qapp.processEvents()
    assert bar.value() == bar.maximum()
    view.close()


def test_chat_view_follows_feedback_after_deferred_layout(_qapp):
    view = ChatView()
    view.resize(620, 330)
    view.show()
    for text in (
        "你好",
        "你好呀。" + "今天过得怎么样？" * 3,
        "在 1 THz 频率、10 m 距离下，自由空间路径损耗约为？\n"
        "A. 60 dB\nB. 80 dB\nC. 约 112.45 dB\nD. 150 dB",
    ):
        view.append_assistant(text)
    view.append_user("C")
    view.append_assistant("正在判题，请稍候……")
    view.append_assistant("回答正确。按 ITU-R P.525 自由空间公式，"
                          "92.45+20log10(1000)+20log10(0.01)=112.45 dB。")

    loop = QEventLoop()
    QTimer.singleShot(250, loop.quit)
    loop.exec()
    bar = view._scroll.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() == bar.maximum()
    view.close()


# ---------------------------------------------------------------------------
# CompanionConsole wiring (fake runner)
# ---------------------------------------------------------------------------

class FakeRunner(QObject):
    agent_event = Signal(object)

    def __init__(self):
        super().__init__()
        self.asked: list[str] = []
        self.session_video = None
        self.video_study = None

    def ask(self, text: str) -> bool:
        self.asked.append(text)
        return True


def _video_session():
    return SimpleNamespace(
        bvid="BV1s54y1n7Ev", title="Docker 10分钟快速入门", owner="Ross-Ning",
        duration_formatted="11:00", segment_count=237, url="https://bilibili.com/video/BV1s54y1n7Ev",
        tags=["教程"],
    )


def test_console_three_column_structure():
    """Phase UI-1: Sidebar | Conversation | CompanionPanel layout wiring."""
    runner = FakeRunner()
    console = CompanionConsole(runner)

    # Left column reuses the SAME header/ability instances (signals intact).
    assert console.sidebar.header is console.header
    assert console.sidebar.ability is console.ability
    assert console.sidebar.width() == 240
    # Right column exists with the context status card.
    assert console.companion.width() == 300
    assert console.companion.context_status.view.mode == "自由对话"
    assert "With you" in console.companion.slogan_label.text()
    # Bottom action buttons emit action_requested.
    emitted: list[str] = []
    console.sidebar.action_requested.connect(emitted.append)
    console.sidebar.action_button("settings").click()
    console.sidebar.action_button("new_chat").click()
    assert emitted == ["settings", "new_chat"]


def test_console_send_and_events():
    runner = FakeRunner()
    console = CompanionConsole(runner)

    console.input.setText("你好")
    console._send()
    assert runner.asked == ["你好"]

    # STATUS -> header state + chat status chip
    runner.agent_event.emit(AgentEvent.make("firefly", AgentEventType.STATUS, status="thinking"))
    assert console.header.state == "working"

    # FINAL -> assistant bubble, header task + success
    runner.agent_event.emit(AgentEvent.make(
        "firefly", AgentEventType.FINAL, text="总结内容", status=None))
    assert "成功" in console.header._status_chip.text() or console.header.state == "success"
    assert console.header._task == "总结内容"

    # ERROR -> header error state
    runner.agent_event.emit(AgentEvent.make("firefly", AgentEventType.ERROR, text="boom"))
    assert console.header.state == "error"


def test_console_video_card_inserted_once():
    runner = FakeRunner()
    runner.session_video = _video_session()
    runner.video_study = SimpleNamespace(stage="watching")
    console = CompanionConsole(runner)

    runner.agent_event.emit(AgentEvent.make("firefly", AgentEventType.FINAL, text="📺 我看完啦"))
    # one card was inserted into the chat flow
    card_found = _find_video_cards(console)
    assert len(card_found) == 1
    card = card_found[0]
    assert card.info.title == "Docker 10分钟快速入门"
    assert card.info.study_stage == "watching"

    # second FINAL for the same video must not duplicate the card
    runner.agent_event.emit(AgentEvent.make("firefly", AgentEventType.FINAL, text="again"))
    assert len(_find_video_cards(console)) == 1


def _find_video_cards(console):
    found = []
    layout = console.chat._column
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget() if item else None
        if widget is None:
            continue
        if isinstance(widget, VideoCard):
            found.append(widget)
            continue
        for child in widget.findChildren(VideoCard):
            found.append(child)
    return found


# ---------------------------------------------------------------------------
# Phase: 字体兼容（数学符号/希腊字母 fallback 栈）
# ---------------------------------------------------------------------------

def test_theme_font_stack_defined():
    from ui.theme import V2_FONT_STACK, V2

    for family in (V2.FONT_FAMILY_PRIMARY, "Segoe UI",
                   V2.FONT_FAMILY_MATH, V2.FONT_FAMILY_FALLBACK):
        assert family in V2_FONT_STACK
    assert V2_FONT_STACK.count('"') == 14  # 7 quoted families


def test_chat_view_math_symbols_render():
    from PySide6.QtWidgets import QTextBrowser

    view = ChatView()
    text = "ε₀ μ σ → ∑ ∫ 渐近分析"
    view.append_assistant(f"讨论符号：{text}")
    browsers = view.findChildren(QTextBrowser)
    assert browsers, "assistant card must contain a text browser"
    browser = browsers[0]
    # 渲染不为空：符号完整保留在文档中
    plain = browser.toPlainText()
    for token in ("ε₀", "μ", "σ", "→", "∑", "∫"):
        assert token in plain, token
    # 字体栈未被覆盖：QSS 携带完整 fallback 链
    assert "Cambria Math" in browser.styleSheet()
    assert "Noto Sans CJK SC" in browser.styleSheet()


def test_input_area_font_stack_and_symbols():
    runner = FakeRunner()
    console = CompanionConsole(runner)
    console.input.setText("ε₀ μ σ → ∑ ∫")
    assert "ε₀" in console.input.text()
    # 字体栈在内部 QPlainTextEdit 的 QSS 上（容器卡片样式不含字体）
    edit_style = console.input.text_edit.styleSheet()
    assert "font-family" in edit_style
    assert "Cambria Math" in edit_style
    assert "Noto Sans CJK SC" in edit_style


def test_video_card_font_stack():
    card = VideoCard(VideoCardInfo(
        bvid="BV1", title="∑ 符号说明", owner="o", duration="1:00"))
    from PySide6.QtWidgets import QLabel

    styled = " ".join(label.styleSheet() for label in card.findChildren(QLabel))
    assert styled.count("font-family") >= 1
    assert "Cambria Math" in styled


def test_message_cards_adaptive_height(_qapp):
    """短消息卡片高度明显小于长文本卡片；高度随内容自动增长。"""
    from ui.v2.chat_view import _MessageCard

    view = ChatView()
    view.resize(1280, 800)
    view.show()  # 布局激活需要可见窗口（offscreen 安全）
    view.append_assistant("你好")
    view.append_assistant("这是一个五百字的测试消息。" * 100)  # ~1000 字
    cards: list = []
    for i in range(view._column.count()):
        item = view._column.itemAt(i)
        widget = item.widget() if item else None
        if widget is not None:
            cards.extend(widget.findChildren(_MessageCard))
    assert len(cards) == 2
    short_card, long_card = cards
    _qapp.processEvents()
    assert short_card.height() < long_card.height()
    assert long_card.height() > short_card.height() * 2  # 长文本明显更高
    # 内部无滚动条（高度完全由文档驱动）
    for card in cards:
        browser = card.findChildren(QTextBrowser)[0]
        assert browser.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        assert browser.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
    view.close()


def test_user_bubble_not_squeezed_vertical(_qapp):
    """回归：用户短句不得被压成单字竖排（内容最小宽 120px）。"""
    from ui.v2.chat_view import _MessageCard

    view = ChatView()
    view.resize(1280, 800)
    view.show()
    view.append_user("你看得见我吗，萤宝")
    _qapp.processEvents()
    cards = [w for i in range(view._column.count())
             for c in ([view._column.itemAt(i).widget()] if view._column.itemAt(i).widget() else [])
             for w in c.findChildren(_MessageCard)]
    assert len(cards) == 1
    card = cards[0]
    browser = card.findChildren(QTextBrowser)[0]
    assert browser.width() >= 120            # 内容最小宽生效
    assert card.width() > 80                 # 任务验收："你好"气泡 > 80px
    assert int(browser.document().size().height()) < 40  # 单行高度，未竖排
    view.close()


def test_user_bubble_short_text_width_over_80(_qapp):
    """任务验收：短文本（"你好"）显示为正常小气泡（宽 > 80px）。"""
    from ui.v2.chat_view import _MessageCard

    view = ChatView()
    view.resize(1280, 800)
    view.show()
    view.append_user("你好")
    _qapp.processEvents()
    card = [w for i in range(view._column.count())
            for c in ([view._column.itemAt(i).widget()] if view._column.itemAt(i).widget() else [])
            for w in c.findChildren(_MessageCard)][0]
    assert card.width() > 80
    browser = card.findChildren(QTextBrowser)[0]
    assert browser.width() >= 120
    # 高度 = 单行（短文本不折行）
    assert int(browser.document().size().height()) < 40
    view.close()


# ---------------------------------------------------------------------------
# Phase: InputArea Enter 发送 / Shift+Enter 换行
# ---------------------------------------------------------------------------

def test_input_area_enter_sends_and_clears():
    from PySide6.QtCore import Qt as QtKey
    from PySide6.QtTest import QTest
    from ui.v2.input_area import InputArea

    ia = InputArea()
    sent: list[str] = []
    ia.send_requested.connect(sent.append)

    ia.setText("测试")
    QTest.keyClick(ia.text_edit, QtKey.Key_Return)
    assert sent == ["测试"]      # send_requested 触发，携带文本
    assert ia.text() == ""       # 发送后输入框清空


def test_input_area_shift_enter_inserts_newline():
    from PySide6.QtCore import Qt as QtKey
    from PySide6.QtTest import QTest
    from ui.v2.input_area import InputArea

    ia = InputArea()
    sent: list[str] = []
    ia.send_requested.connect(sent.append)

    ia.setText("第一行")
    from PySide6.QtGui import QTextCursor

    ia.text_edit.moveCursor(QTextCursor.End)  # 模拟打字后光标在末尾
    QTest.keyClick(ia.text_edit, QtKey.Key_Return, QtKey.ShiftModifier)
    assert ia.text() == "第一行\n"   # Shift+Enter 在末尾插入换行
    assert sent == []                # 不触发发送


def test_console_enter_key_sends_via_input_area():
    from PySide6.QtCore import Qt as QtKey
    from PySide6.QtTest import QTest

    runner = FakeRunner()
    console = CompanionConsole(runner)
    console.input.setText("测试")
    QTest.keyClick(console.input.text_edit, QtKey.Key_Return)
    assert runner.asked == ["测试"]  # console._send 兼容保持
    assert console.input.text() == ""


# ---------------------------------------------------------------------------
# Phase: 数学/物理符号渲染链（sub/sup → Unicode，无异常标签）
# ---------------------------------------------------------------------------

def test_markdown_sub_sup_html_converted_to_unicode():
    from ui.chat_markup import markdown_to_html

    out = markdown_to_html("E<sub>0</sub>=mc<sup>2</sup>")
    assert "E₀" in out and "mc²" in out
    assert "<sub>" not in out and "<sup>" not in out
    assert "&lt;sub&gt;" not in out and "&lt;sup&gt;" not in out


def test_chat_view_sub_sup_html_renders_unicode():
    from ui.chat_markup import _KNOWN_QT_TAGS
    from ui.v2.chat_view import _MessageCard

    view = ChatView()
    view.append_assistant("质能方程：E<sub>0</sub>=mc<sup>2</sup>，其中 ε₀ 是真空介电常数。")
    cards: list = []
    for i in range(view._column.count()):
        item = view._column.itemAt(i)
        widget = item.widget() if item else None
        if widget is not None:
            cards.extend(widget.findChildren(_MessageCard))
    assert len(cards) == 1
    browser = cards[0].findChildren(QTextBrowser)[0]
    plain = browser.toPlainText()
    assert "E₀" in plain and "mc²" in plain and "ε₀" in plain
    assert "<sub>" not in plain and "&lt;sub&gt;" not in plain
    # toHtml 只包含 Qt 标准标签（无异常标签、无未解析的字面标记）
    tags = set(re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", browser.toHtml()))
    assert tags <= _KNOWN_QT_TAGS, tags - _KNOWN_QT_TAGS


# ---------------------------------------------------------------------------
# Phase: 科研级字体系统（STIX 数学优先 + 应用级字体加载）
# ---------------------------------------------------------------------------

def test_v2_font_stack_research_grade():
    from ui.theme import V2, V2_FONT_STACK

    expected = (
        '"STIX Two Math", "Cambria Math", "Microsoft YaHei UI", "Segoe UI", '
        '"Noto Sans CJK SC", "DejaVu Sans", "Symbola"'
    )
    assert V2_FONT_STACK == expected
    assert V2_FONT_STACK.count('"') == 14
    # 数学族必须排在中文与西文之前（公式优先渲染）
    assert V2_FONT_STACK.index("STIX Two Math") < V2_FONT_STACK.index("Microsoft YaHei UI")
    assert V2.FONT_STACK_FAMILIES[0] == "STIX Two Math"


def test_load_application_fonts_empty_dir_is_safe(tmp_path, monkeypatch):
    from ui import theme

    monkeypatch.setattr(theme, "_FONTS_LOADED", False)
    loaded = theme.load_application_fonts(directory=tmp_path)  # 空目录
    assert loaded == []
    assert theme.load_application_fonts(directory=tmp_path) == []  # 幂等


def test_load_application_fonts_skips_invalid_file(tmp_path, monkeypatch):
    from ui import theme

    (tmp_path / "broken.ttf").write_bytes(b"not a real font")
    monkeypatch.setattr(theme, "_FONTS_LOADED", False)
    loaded = theme.load_application_fonts(directory=tmp_path)
    assert loaded == []  # 损坏文件静默跳过，不抛异常


def test_math_symbols_render_research_grade(_qapp):
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QTextBrowser

    from ui.theme import V2, V2_FONT_STACK

    view = ChatView()
    view.resize(1280, 800)
    view.show()
    symbol_sets = (
        "E₀ mc²",
        "ε₀ μ σ",
        "∑ ∫ ∂ ∇ ∞",
        "ψ ℏ λ α β γ",
    )
    for symbols in symbol_sets:
        view.append_assistant(f"符号测试：{symbols}")
    _qapp.processEvents()
    browsers = view.findChildren(QTextBrowser)
    assert len(browsers) >= len(symbol_sets)
    for symbols, browser in zip(symbol_sets, browsers[0:len(symbol_sets)]):
        for token in symbols.split():
            assert token in browser.toPlainText(), token
    # QSS 携带科研级完整字体链
    for family in V2.FONT_STACK_FAMILIES:
        assert family in browsers[0].styleSheet(), family


def test_primary_cjk_font_available_on_system():
    """Windows 桌面必须存在中文主字体（雅黑），保证中文渲染兜底。

    offscreen 平台可能不枚举系统字体：无枚举结果时跳过（真实桌面已验证）。
    """
    from PySide6.QtGui import QFontDatabase

    families = QFontDatabase.families()
    if not families:
        pytest.skip("offscreen platform exposes no system font families")
    assert any("雅黑" in f or "YaHei" in f for f in families)


def test_console_ability_mapping():
    runner = FakeRunner()
    runner.video_study = SimpleNamespace(stage="watching")
    console = CompanionConsole(runner)

    # Current study entry forwards to the host bridge, without asking chat.
    opened = []
    console.learning_bridge_requested.connect(lambda: opened.append(True))
    console._on_ability("study")
    assert opened == [True]
    assert runner.asked == []
    assert console.learning.state.enabled is False

    # screen vision -> existing trigger text
    console._on_ability("screen_vision")
    assert "看一下我的屏幕" in runner.asked

    # settings / research -> host signals, no crash standalone
    console._on_ability("settings")
    console._on_ability("research")
    console.close()


# ---------------------------------------------------------------------------
# Phase UI-3A: RuntimeState -> CompanionPanel (fake RuntimeBus)
# ---------------------------------------------------------------------------

class FakeRuntimeBus(QObject):
    """Records subscriptions; publishes runtime.activity envelopes."""

    def __init__(self):
        super().__init__()
        self.callbacks: list = []
        self.unsubscribed = 0

    def subscribe_event(self, callback):
        self.callbacks.append(callback)
        return self._unsubscribe

    def _unsubscribe(self):
        self.unsubscribed += 1
        if self.callbacks:
            self.callbacks.clear()

    def publish_activity(self, state: str):
        from core.runtime_bus import RuntimeEvent

        event = RuntimeEvent(kind="runtime.activity", source="test", payload=state)
        for callback in list(self.callbacks):
            callback(event)


def test_runtime_bus_updates_companion_state():
    from core.runtime_state_aggregator import RuntimeActivityState

    bus = FakeRuntimeBus()
    runner = FakeRunner()
    console = CompanionConsole(runner, runtime_bus=bus)
    assert len(bus.callbacks) == 1  # subscribed

    expected = {
        "idle": "等待指令",
        "working": "正在工作",
        "tool_running": "正在调用工具",
        "waiting_input": "等待回复",
        "success": "任务完成",
        "error": "出现问题",
    }
    for state, label in expected.items():
        bus.publish_activity(RuntimeActivityState(state))
        assert console.companion.companion_state_label.text() == label, state


def test_runtime_bus_ignores_other_kinds():
    from core.runtime_bus import RuntimeEvent

    bus = FakeRuntimeBus()
    console = CompanionConsole(FakeRunner(), runtime_bus=bus)
    bus.callbacks[0](RuntimeEvent(kind="plugin.status", source="test", payload="x"))
    assert console.companion.companion_state_label.text() == "在线"  # unchanged


def test_console_without_runtime_bus_is_unchanged():
    console = CompanionConsole(FakeRunner())
    assert console._runtime_unsubscribe is None
    console.companion.set_companion_state("等待指令")
    assert console.companion.companion_state_label.text() == "等待指令"


def test_console_close_unsubscribes_runtime_bus():
    bus = FakeRuntimeBus()
    console = CompanionConsole(FakeRunner(), runtime_bus=bus)
    console.close()
    assert bus.unsubscribed == 1
    assert bus.callbacks == []

    # Publishing after close must not touch the closed console.
    label_before = console.companion.companion_state_label.text()
    bus.publish_activity("working")
    assert console.companion.companion_state_label.text() == label_before


@pytest.fixture(autouse=True)
def _isolated_console_windows(_qapp):
    """Dispose test-created windows; singleton tests must not inherit prior cases."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from ui.v2 import console as console_module
    def dispose():
        console_module._console_instance = None
        for widget in list(_qapp.topLevelWidgets()):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        _qapp.processEvents()
    dispose()
    yield
    dispose()
