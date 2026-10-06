"""PaperLens Bridge — browser selection → Firefly protocol + explain chain.

Phase 1 experiment proof: a WebSocket client (the shape the browser
extension sends) delivers a selection to the desktop bridge, which logs
"收到 selection event" and emits ``selection(text, page)``.

Phase 2: ``ExplainBox.show_browser_selection`` reuses the existing consent
gate + async worker + ``PdfQa.explain_selection`` (fake LLM asserts that
page/text travel untouched).
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.pdf_qa import PdfQa
from core.pagelens_bridge import PageLensBridge
from ui.explain_box import ExplainBox
from conftest import teardown_qt_widget

PAIRING_TOKEN = "test-pairing-" + "x" * 40
PAIRING_ORIGIN = "chrome-extension://" + "a" * 32


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _connect_direct(signal, slot) -> None:
    """Fire the slot immediately in the emitting thread (no queued delivery)."""
    signal.connect(slot, type=Qt.DirectConnection)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# 1. Protocol unit: selection message → signal + log
# ---------------------------------------------------------------------------


def test_handle_selection_emits_signal() -> None:
    _app()
    bridge = PageLensBridge()
    received = []
    _connect_direct(bridge.selection, lambda text, page, source: received.append((text, page, source)))

    raw = json.dumps({
        "type": "selection",
        "payload": {
            "text": "Multi-Head Attention",
            "url": "file:///attention.pdf",
            "page": 4,
        },
    })
    bridge._handle_incoming(raw)

    # No source in the payload -> safe "ambient" default (never auto-explains).
    assert received == [("Multi-Head Attention", 4, "ambient")]


def test_handle_selection_carries_user_action_source() -> None:
    _app()
    bridge = PageLensBridge()
    received = []
    _connect_direct(bridge.selection, lambda text, page, source: received.append((text, page, source)))

    bridge._handle_incoming(json.dumps({
        "type": "selection",
        "payload": {"text": "explain me", "page": -1, "source": "user_action"},
    }))
    # An explicit explain request is the ONLY source allowed to open explain.
    assert received == [("explain me", -1, "user_action")]

    # Any non-"user_action" value is normalized to "ambient".
    bridge._handle_incoming(json.dumps({
        "type": "selection",
        "payload": {"text": "highlight only", "page": -1, "source": "something-else"},
    }))
    assert received[1] == ("highlight only", -1, "ambient")


def test_handle_selection_sanitizes_page_and_text() -> None:
    _app()
    bridge = PageLensBridge()
    received = []
    _connect_direct(bridge.selection, lambda text, page, source: received.append((text, page, source)))

    # Unknown / invalid page numbers become -1; text is capped at 2000 chars.
    bridge._handle_incoming(json.dumps({
        "type": "selection",
        "payload": {"text": "x" * 5000, "page": 0},
    }))
    bridge._handle_incoming(json.dumps({
        "type": "selection",
        "payload": {"text": "abc", "page": "not-a-number"},
    }))
    assert len(received) == 2
    assert received[0] == ("x" * 2000, -1, "ambient")
    assert received[1] == ("abc", -1, "ambient")


# ---------------------------------------------------------------------------
# 2. Real loopback: a websockets client (extension-shaped) → bridge signal
# ---------------------------------------------------------------------------

async def _send_extension_frames(port: int) -> None:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{port}",
                                  origin=PAIRING_ORIGIN,
                                  subprotocols=["firefly-auth." + PAIRING_TOKEN]) as ws:
        await ws.send(json.dumps({"type": "bridge_hello", "payload": {}}))
        await ws.send(json.dumps({
            "type": "selection",
            "payload": {
                "text": "Multi-Head Attention",
                "url": "file:///C:/papers/attention.pdf",
                "page": 4,
            },
        }))
        await asyncio.sleep(0.4)  # let the server process both frames


def test_selection_over_real_websocket(monkeypatch, caplog) -> None:
    _app()
    port = _free_port()
    monkeypatch.setattr("core.pagelens_bridge.BRIDGE_PORT", port)

    bridge = PageLensBridge(auth_token=PAIRING_TOKEN, allowed_origins=[PAIRING_ORIGIN])
    received = []
    _connect_direct(bridge.selection, lambda text, page, source: received.append((text, page, source)))

    bridge.start()
    try:
        time.sleep(0.4)  # server binds
        with caplog.at_level(logging.INFO, logger="firefly.pagelens.bridge"):
            asyncio.run(_send_extension_frames(port))
            deadline = time.time() + 3.0
            while not received and time.time() < deadline:
                time.sleep(0.05)
    finally:
        bridge.stop()

    # The desktop received the selection (acceptance: Firefly 收到 selection).
    assert received == [("Multi-Head Attention", 4, "ambient")]
    assert "收到 selection event" in caplog.text
    assert "Multi-Head Attention" in caplog.text
    assert "page=4" in caplog.text


# ---------------------------------------------------------------------------
# 3. Phase 2: ExplainBox.show_browser_selection → explain_selection (fake LLM)
# ---------------------------------------------------------------------------


@pytest.fixture()
def qapp() -> QApplication:
    return _app()


def _wait_worker() -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(2000, loop.quit)
    loop.exec()


def test_browser_selection_explain_chain(qapp, monkeypatch) -> None:
    """Selection arrives → explain entry → consent → PdfQa gets page/text."""
    monkeypatch.setattr("core.pdf_qa.PdfProcessor", lambda *args, **kwargs: MagicMock())
    chat = MagicMock(return_value={"choices": [{"message": {"content": "注意力解释"}}]})

    box = ExplainBox()
    try:
        box.show_browser_selection("Multi-Head Attention", 4)
        assert box._last_selection == ("Multi-Head Attention", 4)
        assert box._explain_btn.isEnabled()
        box._qa = PdfQa(chat_handler=chat)

        # First click gates on consent; nothing reaches the provider.
        box._on_explain_clicked()
        assert chat.call_count == 0
        assert box._pending_selection == ("Multi-Head Attention", 4)

        box._on_consent_granted()
        _wait_worker()

        assert chat.call_count == 1
        content = chat.call_args[0][0][0]["content"]
        assert "页码：4" in content
        assert "Multi-Head Attention" in content
        assert "注意力解释" in box._content._body_label.text()
    finally:
        teardown_qt_widget(box)


def test_browser_selection_without_page_is_guarded(qapp, monkeypatch) -> None:
    """page=-1 (built-in viewer) blocks the provider call with a hint."""
    monkeypatch.setattr("core.pdf_qa.PdfProcessor", lambda *args, **kwargs: MagicMock())
    chat = MagicMock()

    box = ExplainBox()
    try:
        box.show_browser_selection("Multi-Head Attention", -1)
        box._qa = PdfQa(chat_handler=chat)
        box._external_provider_consent = True
        box._explain_consent_granted = True
        assert not box._explain_btn.isEnabled()

        box._on_explain_clicked()
        assert chat.call_count == 0
        assert "缺少页码" in box._pdf_status.text()
    finally:
        teardown_qt_widget(box)


def test_browser_selection_consent_btn_explains_current(qapp, monkeypatch) -> None:
    """Granting consent directly (without clicking explain) still explains."""
    monkeypatch.setattr("core.pdf_qa.PdfProcessor", lambda *args, **kwargs: MagicMock())
    chat = MagicMock(return_value={"choices": [{"message": {"content": "解释"}}]})

    box = ExplainBox()
    try:
        box.show_browser_selection("Query Key Value", 2)
        box._qa = PdfQa(chat_handler=chat)
        assert not box._consent_btn.isHidden()  # consent requested

        box._on_consent_granted()  # user clicks the consent button directly
        _wait_worker()

        assert chat.call_count == 1
        content = chat.call_args[0][0][0]["content"]
        assert "页码：2" in content
        assert "Query Key Value" in content
    finally:
        teardown_qt_widget(box)
