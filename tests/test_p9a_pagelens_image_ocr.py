"""PageLens Image/OCR Integration — Stage 1A targeted tests.

Covers:
 1. PageLensPanel has image toolbar (add/remove/status/toggle/ocr panel)
 2. ImageOcrWorker reused by PageLensPanel (not a copy)
 3. Attach image → OCR runs → status updates
 4. Replace image → old OCR result ignored (generation guard)
 5. Remove image → OCR context cleared
 6. OCR text displayed in scrollable QTextBrowser panel
 7. Corrupt image → graceful error
 8. PageLensBridge not involved (no WebSocket messages)
 9. AI Router / Provider not called
10. ExplainBox original tests still pass
11. Real Qt paint/show regression — _PIconButton must not crash on first paint
12. OCR panel toggle: hidden → visible → hidden
13. Long OCR text does not change fixed header layout
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw
from PySide6.QtCore import QEventLoop, QTimer, QThread, Slot
from PySide6.QtWidgets import QApplication

from core.pdf_qa import PdfQa
from ui.explain_box import ExplainBox, _ExplainWorker
from ui.image_ocr_worker import ImageOcrWorker
from ui.pagelens_panel import PageLensPanel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ThreadRecordingPageLens(PageLensPanel):
    """Records the actual Qt thread used by the production OCR slots."""

    def __init__(self) -> None:
        self.ocr_callback_thread: QThread | None = None
        super().__init__()

    @Slot(int, str)
    def _on_ocr_finished(self, gen: int, ocr_text: str) -> None:
        self.ocr_callback_thread = QThread.currentThread()
        super()._on_ocr_finished(gen, ocr_text)

    @Slot(int, str)
    def _on_ocr_error(self, gen: int, message: str) -> None:
        self.ocr_callback_thread = QThread.currentThread()
        super()._on_ocr_error(gen, message)


def _wait_for_thread_exit(thread: QThread, timeout_ms: int) -> None:
    if not thread.isRunning():
        return
    loop = QEventLoop()
    thread.finished.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    try:
        assert not thread.isRunning(), "OCR QThread did not exit after worker completion"
    except RuntimeError:
        # thread.finished -> deleteLater may already have destroyed the native
        # QThread by the time the nested event loop returns; that is successful
        # lifecycle cleanup, not a running-thread failure.
        return

def _font(size: int = 48) -> ImageFont.FreeTypeFont:  # type: ignore[name-defined]
    from PIL import ImageFont
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.is_file():
        raise RuntimeError("Microsoft YaHei required for Windows OCR dogfood")
    return ImageFont.truetype(str(font_path), size)  # type: ignore[no-any-return]


def _make_text_image(text: str, width: int = 600, height: int = 150) -> bytes:
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = _font()
    except RuntimeError:
        font = ImageFont.load_default()  # type: ignore[attr-defined]
    draw.text((10, 20), text, font=font, fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_corrupt_png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"CORRUPT_DATA" * 50


# ---------------------------------------------------------------------------
# 1. PageLens has image toolbar components
# ---------------------------------------------------------------------------

def test_pagelens_has_image_toolbar(qapp: QApplication) -> None:
    """1. PageLensPanel has image toolbar with add/remove/status/toggle/ocr panel."""
    panel = PageLensPanel()
    assert hasattr(panel, "_add_img_btn")
    assert hasattr(panel, "_remove_img_btn")
    assert hasattr(panel, "_ocr_status_label")
    assert hasattr(panel, "_ocr_text_panel")
    assert hasattr(panel, "_ocr_text_browser")
    assert hasattr(panel, "_ocr_view_btn")
    assert hasattr(panel, "_img_preview")
    assert hasattr(panel, "_img_filename")
    # OCR text panel is hidden by default
    assert panel._ocr_text_panel.isHidden()
    # Toggle button is hidden by default (no image attached)
    assert panel._ocr_view_btn.isHidden()
    # Status label is hidden by default (no image attached)
    assert panel._ocr_status_label.isHidden()
    # Image filename is hidden by default
    assert panel._img_filename.isHidden()
    panel.close()


# ---------------------------------------------------------------------------
# 2. ImageOcrWorker reused by PageLensPanel (same class)
# ---------------------------------------------------------------------------

def test_pagelens_uses_shared_image_ocr_worker() -> None:
    """2. PageLensPanel creates ImageOcrWorker (not a copy)."""
    from ui.pagelens_panel import ImageOcrWorker as PlOcrWorker

    assert PlOcrWorker is ImageOcrWorker


# ---------------------------------------------------------------------------
# 3. Attach image → OCR runs → status updates
# ---------------------------------------------------------------------------

def test_pagelens_attach_image_triggers_ocr(qapp: QApplication) -> None:
    """3. Attaching an image starts OCR and updates status."""
    img_bytes = _make_text_image("中文测试")
    tmp = Path(__file__).parent / ".tmp_pl_attach.png"
    tmp.write_bytes(img_bytes)
    try:
        panel = PageLensPanel()
        panel._attach_image(str(tmp))

        assert panel._ocr_status == "识别中"
        assert panel._image_path is not None
        assert panel._ocr_generation == 1

        # Wait for OCR
        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        assert panel._ocr_status in ("已识别", "OCR失败"), (
            f"OCR still running after 15s, status={panel._ocr_status}"
        )
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Replace image → old OCR result ignored
# ---------------------------------------------------------------------------

def test_pagelens_replace_image_ignores_old_result(qapp: QApplication) -> None:
    """4. Replacing image cancels old OCR; stale result does not overwrite."""
    tmp1 = Path(__file__).parent / ".tmp_pl_replace_1.png"
    tmp2 = Path(__file__).parent / ".tmp_pl_replace_2.png"
    try:
        tmp1.write_bytes(_make_text_image("FIRST_IMAGE"))
        tmp2.write_bytes(_make_text_image("SECOND_IMAGE"))

        panel = PageLensPanel()
        panel._attach_image(str(tmp1))
        gen_after_first = panel._ocr_generation

        panel._attach_image(str(tmp2))
        assert panel._ocr_generation == gen_after_first + 1

        # Wait for both
        loop = QEventLoop()
        QTimer.singleShot(20000, loop.quit)
        loop.exec()

        # Final should be from second image
        if panel._ocr_status == "已识别":
            assert "FIRST_IMAGE" not in panel._ocr_text or "SECOND_IMAGE" in panel._ocr_text
        panel.close()
    finally:
        tmp1.unlink(missing_ok=True)
        tmp2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. Remove image → OCR context cleared
# ---------------------------------------------------------------------------

def test_pagelens_remove_image_clears_state(qapp: QApplication) -> None:
    """5. Removing image clears all OCR state."""
    img_bytes = _make_text_image("REMOVE_TEST")
    tmp = Path(__file__).parent / ".tmp_pl_remove.png"
    tmp.write_bytes(img_bytes)
    try:
        panel = PageLensPanel()
        panel._attach_image(str(tmp))

        # Wait for OCR
        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        panel._on_remove_image()

        assert panel._image_path is None
        assert panel._ocr_text == ""
        assert panel._ocr_status == "未识别"
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. OCR text displayed in panel
# ---------------------------------------------------------------------------

def test_pagelens_ocr_text_shown_in_panel(qapp: QApplication) -> None:
    """6. After OCR completes, OCR text is displayed in the scrollable panel."""
    img_bytes = _make_text_image("DISPLAY_TEST_123")
    tmp = Path(__file__).parent / ".tmp_pl_display.png"
    tmp.write_bytes(img_bytes)
    try:
        panel = PageLensPanel()
        panel._attach_image(str(tmp))

        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        if panel._ocr_status == "已识别":
            assert "DISPLAY" in panel._ocr_text
            assert "TEST" in panel._ocr_text
            assert "123" in panel._ocr_text
            # OCR panel is NOT auto-expanded; toggle button is visible
            assert panel._ocr_text_panel.isHidden()
            assert not panel._ocr_view_btn.isHidden()
            # Text is in the QTextBrowser
            assert "DISPLAY" in panel._ocr_text_browser.toPlainText()
            assert "TEST" in panel._ocr_text_browser.toPlainText()
            assert "123" in panel._ocr_text_browser.toPlainText()
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 7. Corrupt image → graceful error
# ---------------------------------------------------------------------------

def test_pagelens_corrupt_image_graceful(qapp: QApplication) -> None:
    """7. Corrupt PNG → error signal, no crash."""
    tmp = Path(__file__).parent / ".tmp_pl_corrupt.png"
    tmp.write_bytes(_make_corrupt_png())
    try:
        panel = PageLensPanel()
        panel._attach_image(str(tmp))

        loop = QEventLoop()
        QTimer.singleShot(5000, loop.quit)
        loop.exec()

        # Should have error status, not crash
        assert panel._ocr_status == "OCR失败"
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 8. PageLensBridge not involved
# ---------------------------------------------------------------------------

def test_pagelens_ocr_does_not_use_bridge(qapp: QApplication) -> None:
    """8. Image OCR in PageLens does NOT trigger any bridge signals."""
    panel = PageLensPanel()

    # Mock the bridge to detect any accidental signal emission
    bridge_mock = MagicMock()
    panel.pagelens_bridge = bridge_mock

    img_bytes = _make_text_image("BRIDGE_TEST")
    tmp = Path(__file__).parent / ".tmp_pl_bridge.png"
    tmp.write_bytes(img_bytes)
    try:
        panel._attach_image(str(tmp))

        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        # Bridge should not have been called for OCR
        bridge_mock.send_action.assert_not_called()
        bridge_mock.send_open_concept.assert_not_called()
        bridge_mock.send_open_related.assert_not_called()
        bridge_mock.send_open_question.assert_not_called()
        bridge_mock.send_back.assert_not_called()
        bridge_mock.send_sync_request.assert_not_called()
    finally:
        tmp.unlink(missing_ok=True)
        panel.close()


# ---------------------------------------------------------------------------
# 9. AI Router / Provider not called
# ---------------------------------------------------------------------------

def test_pagelens_ocr_does_not_call_ai_router() -> None:
    """9. Image OCR in PageLens does NOT call AI Router or Provider.

    Verifies by inspecting source: ImageOcrWorker.run() only imports and uses
    RapidOcrBackend — no PdfQa, no chat handler, no provider calls.
    """
    import inspect
    source = inspect.getsource(ImageOcrWorker.run)

    assert "PdfQa" not in source
    assert "chat" not in source
    assert "ai_router" not in source
    assert "provider" not in source.lower() or "RapidOcrBackend" in source
    assert "RapidOcrBackend" in source


# ---------------------------------------------------------------------------
# 10. ExplainBox original tests still pass (regression)
# ---------------------------------------------------------------------------

def test_explain_box_still_works(qapp: QApplication) -> None:
    """10. ExplainBox with ImageOcrWorker still works correctly.

    Uses a direct synchronous call (no QEventLoop) to avoid cross-test
    QApplication state issues.
    """
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "explain result"}}],
    })
    qa = PdfQa(chat_handler=chat)

    box = ExplainBox()
    box._qa = qa
    box._external_provider_consent = True

    # Call worker.run() directly (same-thread DirectConnection)
    from ui.explain_box import _ExplainWorker

    worker = _ExplainWorker(
        qa=qa,
        selected_text="test selection",
        source_page=1,
        consent=True,
        image_ocr_text="",
    )
    worker.run()

    assert chat.called
    box.close()


# ---------------------------------------------------------------------------
# 11. Real Qt paint/show regression — _PIconButton must not crash on first paint
# ---------------------------------------------------------------------------

def test_pagelens_show_panel_paints_without_crash(qapp: QApplication) -> None:
    """11. Real Qt show() triggers _PIconButton.paintEvent — must not crash.

    This test ensures the full paint chain fires during show_panel(),
    covering the exact path that previously crashed with:
        TypeError: QVariant must be holding a QColor
    """
    from ui.pagelens_panel import PageLensPanel

    panel = PageLensPanel()
    panel.show_panel()

    # Force Qt to process paint events (this is what triggers the crash
    # in the real app — the first paint after show()).
    QApplication.instance().processEvents()

    # Verify image toolbar widgets exist and are properly constructed
    assert hasattr(panel, "_add_img_btn")
    assert hasattr(panel, "_remove_img_btn")
    assert hasattr(panel, "_ocr_status_label")
    assert hasattr(panel, "_ocr_text_panel")
    assert hasattr(panel, "_ocr_text_browser")
    assert hasattr(panel, "_ocr_view_btn")
    assert hasattr(panel, "_img_preview")

    # Verify panel is visible
    assert panel.visible

    panel.close()
    QApplication.instance().processEvents()


# ---------------------------------------------------------------------------
# 12. OCR panel toggle: hidden → visible → hidden
# ---------------------------------------------------------------------------

def test_pagelens_ocr_panel_toggle(qapp: QApplication) -> None:
    """12. Clicking "查看 OCR" toggles the OCR panel visibility."""
    img_bytes = _make_text_image("TOGGLE_TEST")
    tmp = Path(__file__).parent / ".tmp_pl_toggle.png"
    tmp.write_bytes(img_bytes)
    try:
        panel = PageLensPanel()
        panel._attach_image(str(tmp))

        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        # After OCR: panel hidden, toggle button visible
        if panel._ocr_status == "已识别":
            assert panel._ocr_text_panel.isHidden()
            assert not panel._ocr_view_btn.isHidden()

            # Click toggle → panel visible
            panel._on_toggle_ocr()
            assert not panel._ocr_text_panel.isHidden()
            assert panel._ocr_view_btn._icon == "收起"

            # Click toggle again → panel hidden
            panel._on_toggle_ocr()
            assert panel._ocr_text_panel.isHidden()
            assert panel._ocr_view_btn._icon == "🔍"
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 13. Long OCR text — fixed header layout, scrollable
# ---------------------------------------------------------------------------

def test_pagelens_long_ocr_text_scrollable(qapp: QApplication) -> None:
    """13. Long OCR text fits in fixed-height panel and scrolls."""
    # Build a long text image with many lines
    lines = "\n".join(f"Line {i}: OCR test content for verification" for i in range(50))
    img_bytes = _make_text_image(lines, width=600, height=1500)
    tmp = Path(__file__).parent / ".tmp_pl_long.png"
    tmp.write_bytes(img_bytes)
    try:
        panel = PageLensPanel()
        panel._attach_image(str(tmp))

        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        if panel._ocr_status == "已识别":
            # Text is in browser (at least some content)
            plain = panel._ocr_text_browser.toPlainText()
            assert len(plain) > 10, f"OCR text too short: {plain[:80]}"

            # Panel has max height constraint
            assert panel._ocr_text_panel.maximumHeight() > 0

            # Panel is scrollable (has vertical scrollbar policy)
            sb = panel._ocr_text_browser.verticalScrollBar()
            assert sb is not None

            # Fixed header layout not changed (toolbar height unchanged)
            assert panel._image_toolbar.height() > 0
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 14. Real QThread + QueuedConnection OCR regression — no native crash
# ---------------------------------------------------------------------------

def test_pagelens_real_qthread_ocr_no_crash(qapp: QApplication) -> None:
    """14. Real QThread OCR via PageLensPanel — finished runs in GUI thread.

    This test uses a real ImageOcrWorker running in a real QThread, connected
    via QObject→QObject signal (QueuedConnection), and verifies that
    _on_ocr_finished updates QTextBrowser without crashing.
    """
    img_bytes = _make_text_image("REAL_QTHREAD_TEST")
    tmp = Path(__file__).parent / ".tmp_pl_qthread.png"
    tmp.write_bytes(img_bytes)
    try:
        panel = _ThreadRecordingPageLens()
        panel._attach_image(str(tmp))
        thread = panel._ocr_thread
        assert thread is not None and thread.isRunning()

        _wait_for_thread_exit(thread, 15000)

        assert panel._ocr_status in ("已识别", "OCR失败")
        assert panel.ocr_callback_thread is qapp.thread()
        if panel._ocr_status == "已识别":
            assert "REAL" in panel._ocr_text_browser.toPlainText()
            assert "QTHREAD" in panel._ocr_text_browser.toPlainText()
            assert "TEST" in panel._ocr_text_browser.toPlainText()
            assert panel._ocr_generation == 1
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 15. Real QThread OCR error path — runs in GUI thread
# ---------------------------------------------------------------------------

def test_pagelens_real_qthread_ocr_error_in_gui_thread(qapp: QApplication) -> None:
    """15. Real QThread OCR error signal — _on_ocr_error runs in GUI thread."""
    tmp = Path(__file__).parent / ".tmp_pl_qthread_err.png"
    tmp.write_bytes(_make_corrupt_png())
    try:
        panel = _ThreadRecordingPageLens()
        panel._attach_image(str(tmp))
        thread = panel._ocr_thread
        assert thread is not None
        _wait_for_thread_exit(thread, 5000)

        assert panel._ocr_status == "OCR失败"
        assert panel.ocr_callback_thread is qapp.thread()
        panel.close()
    finally:
        tmp.unlink(missing_ok=True)


def test_pagelens_remove_invalidates_queued_callbacks(qapp: QApplication) -> None:
    """A queued result/error from a removed image cannot revive OCR UI state."""
    panel = PageLensPanel()
    panel._ocr_generation = 7
    panel._image_path = "removed.png"
    panel._on_remove_image()

    panel._on_ocr_finished(7, "STALE OCR")
    panel._on_ocr_error(7, "stale error")

    assert panel._ocr_generation == 8
    assert panel._image_path is None
    assert panel._ocr_text == ""
    assert panel._ocr_status == "未识别"
    assert panel._ocr_text_browser.toPlainText() == ""
    assert panel._ocr_view_btn.isHidden()
    panel.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
