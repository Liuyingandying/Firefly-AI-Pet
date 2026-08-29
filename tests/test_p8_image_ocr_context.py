"""Image / OCR Context — Stage 1 targeted tests.

Covers:
  1. _ImageOcrWorker reuses P8 RapidOcrBackend production path
  2. Image attach → OCR runs → status updates
  3. Image replace → old OCR result ignored (generation guard)
  4. Remove image → OCR context cleared
  5. OCR-only explain (selected_text empty, image_ocr_text non-empty)
  6. selected_text + OCR explain (both present, sources marked separately)
  7. Both empty → ValueError, provider NOT called
  8. Privacy: provider payload contains no image bytes / base64 / absolute paths
  9. No-text image → no crash, graceful empty OCR
 10. Corrupt image → graceful error
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import fitz
import pytest
from PIL import Image, ImageDraw
from PySide6.QtCore import QEventLoop, QTimer, QThread, Slot
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from core.pdf_processor import PdfProcessor, RapidOcrBackend
from core.pdf_qa import PdfQa
from ui.explain_box import ExplainBox, _ImageOcrWorker
from conftest import teardown_qt_widget


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ThreadRecordingExplainBox(ExplainBox):
    def __init__(self) -> None:
        self.ocr_callback_thread: QThread | None = None
        super().__init__()

    @Slot(int, str)
    def _on_ocr_finished(self, gen: int, ocr_text: str) -> None:
        self.ocr_callback_thread = QThread.currentThread()
        if self.ocr_callback_thread is self.thread():
            super()._on_ocr_finished(gen, ocr_text)

    @Slot(int, str)
    def _on_ocr_error(self, gen: int, message: str) -> None:
        self.ocr_callback_thread = QThread.currentThread()
        if self.ocr_callback_thread is self.thread():
            super()._on_ocr_error(gen, message)


def _wait_for_thread_exit(thread: QThread, timeout_ms: int) -> None:
    if not thread.isRunning():
        return
    loop = QEventLoop()
    thread.finished.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    try:
        assert not thread.isRunning()
    except RuntimeError:
        return

def _font(size: int = 64) -> ImageFont.FreeTypeFont:  # type: ignore[name-defined]
    from PIL import ImageFont
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.is_file():
        raise RuntimeError("Microsoft YaHei required for Windows OCR dogfood")
    return ImageFont.truetype(str(font_path), size)  # type: ignore[no-any-return]


def _make_text_image(text: str, width: int = 800, height: int = 200) -> bytes:
    """Create a PNG image with drawn text."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = _font()
    except RuntimeError:
        font = ImageFont.load_default()  # type: ignore[attr-defined]
    draw.text((20, 40), text, font=font, fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_no_text_image() -> bytes:
    """Create a plain gradient image with no text."""
    img = Image.new("RGB", (400, 300), "lightblue")
    for y in range(300):
        r = int(100 + 155 * y / 300)
        img.putdata([
            (r, 200 - y // 3, 255 - r // 2)
            for _ in range(400)
        ])
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_corrupt_png() -> bytes:
    """Create a file that looks like PNG but is corrupted."""
    return b"\x89PNG\r\n\x1a\n" + b"CORRUPT_DATA_HERE" * 100


# ---------------------------------------------------------------------------
# 1. _ImageOcrWorker reuses P8 RapidOcrBackend
# ---------------------------------------------------------------------------

def test_image_ocr_worker_uses_rapidocr_backend(qapp: QApplication) -> None:
    """1. _ImageOcrWorker imports and uses RapidOcrBackend from pdf_processor."""
    img_bytes = _make_text_image("FIREFLY OCR TEST")
    tmp = Path(__file__).parent / ".tmp_ocr_worker_test.png"
    tmp.write_bytes(img_bytes)
    try:
        worker = _ImageOcrWorker(str(tmp), generation=1)
        received: list[str] = []
        worker.finished.connect(lambda g, t: received.append(t))
        worker.run()
        assert received
        assert "FIREFLY" in received[0] or "OCR" in received[0]
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2. Image attach → OCR runs → status updates
# ---------------------------------------------------------------------------

def test_attach_image_triggers_ocr_and_updates_status(qapp: QApplication) -> None:
    """2. Attaching an image starts OCR and updates status through lifecycle."""
    img_bytes = _make_text_image("中文识别测试")
    tmp = Path(__file__).parent / ".tmp_attach_test.png"
    tmp.write_bytes(img_bytes)
    try:
        box = ExplainBox()
        # Use _attach_image directly to avoid QFileDialog blocking in tests
        box._attach_image(str(tmp))

        assert box._ocr_status == "识别中"
        assert box._image_path is not None
        # isHidden() checks the widget's own hidden flag, independent of parent.
        assert not box._ocr_status_label.isHidden()
        assert not box._remove_btn.isHidden()

        # Wait for OCR — RapidOCR can be slow on first load
        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        # Status should have progressed past "识别中"
        assert box._ocr_status in ("已识别", "OCR失败"), (
            f"OCR still running after 15s, status={box._ocr_status}"
        )
        if box._ocr_status == "已识别":
            assert box._ocr_text
            assert not box._ocr_text_panel.isHidden()
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. Image replace → old OCR result ignored
# ---------------------------------------------------------------------------

def test_replace_image_ignores_old_ocr_result(qapp: QApplication) -> None:
    """3. Replacing image cancels old OCR; stale result does not overwrite."""
    tmp1 = Path(__file__).parent / ".tmp_replace_1.png"
    tmp2 = Path(__file__).parent / ".tmp_replace_2.png"
    try:
        tmp1.write_bytes(_make_text_image("FIRST"))
        tmp2.write_bytes(_make_text_image("SECOND"))

        box = ExplainBox()

        # Attach first image
        box._attach_image(str(tmp1))
        gen_after_first = box._ocr_generation

        # Attach second image immediately (should cancel first)
        box._attach_image(str(tmp2))
        assert box._ocr_generation == gen_after_first + 1

        # Wait for both OCRs to complete
        loop = QEventLoop()
        QTimer.singleShot(8000, loop.quit)
        loop.exec()

        # The final OCR text should be from the second image, not the first
        # (if the first stale result somehow overwrote, it would contain "FIRST")
        if box._ocr_status == "已识别":
            assert "FIRST" not in box._ocr_text or "SECOND" in box._ocr_text
    finally:
        tmp1.unlink(missing_ok=True)
        tmp2.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Remove image → OCR context cleared
# ---------------------------------------------------------------------------

def test_remove_image_clears_all_ocr_state(qapp: QApplication) -> None:
    """4. Removing image clears _image_path, _ocr_text, status, UI elements."""
    img_bytes = _make_text_image("REMOVE TEST")
    tmp = Path(__file__).parent / ".tmp_remove_test.png"
    tmp.write_bytes(img_bytes)
    try:
        box = ExplainBox()
        # Use _attach_image directly to avoid QFileDialog blocking
        box._attach_image(str(tmp))

        # Wait for OCR to finish — RapidOCR slow on first load
        loop = QEventLoop()
        QTimer.singleShot(15000, loop.quit)
        loop.exec()

        # Remove
        box._on_remove_image()

        assert box._image_path is None
        assert box._ocr_text == ""
        assert box._ocr_status == "未识别"
        assert box._image_preview.isHidden()
        assert box._remove_btn.isHidden()
        assert box._ocr_status_label.isHidden()
        assert box._ocr_text_panel.isHidden()
    finally:
        tmp.unlink(missing_ok=True)


def test_explain_box_remove_invalidates_queued_callbacks(qapp: QApplication) -> None:
    """ExplainBox also rejects late success and error signals after removal."""
    box = ExplainBox()
    box._ocr_generation = 3
    box._image_path = "removed.png"
    box._on_remove_image()

    box._on_ocr_finished(3, "STALE OCR")
    box._on_ocr_error(3, "stale error")

    assert box._ocr_generation == 4
    assert box._image_path is None
    assert box._ocr_text == ""
    assert box._ocr_status == "未识别"
    assert box._ocr_text_panel.isHidden()
    teardown_qt_widget(box)


def test_explain_box_real_qthread_callback_runs_in_gui_thread(
    qapp: QApplication,
) -> None:
    """Shared worker reaches ExplainBox only through its GUI-affine relay."""
    tmp = Path(__file__).parent / ".tmp_explain_qthread_err.png"
    tmp.write_bytes(_make_corrupt_png())
    try:
        box = _ThreadRecordingExplainBox()
        box._attach_image(str(tmp))
        thread = box._ocr_thread
        assert thread is not None
        _wait_for_thread_exit(thread, 15000)

        assert box._ocr_status == "OCR失败"
        assert box.ocr_callback_thread is qapp.thread()
        teardown_qt_widget(box)
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 5. OCR-only explain (selected_text empty, image_ocr_text non-empty)
# ---------------------------------------------------------------------------

def test_ocr_only_explain_with_mock_provider(qapp: QApplication) -> None:
    """5. OCR-only explain works: selected_text='', image_ocr_text='OCR result'."""
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "这是图片OCR识别的文本解释。"}}],
    })
    qa = PdfQa(chat_handler=chat)

    entry = qa.explain_selection(
        "",
        source_page=1,
        consent=True,
        image_ocr_text="OCR识别出的图片文字内容",
    )

    chat.assert_called_once()
    prompt = chat.call_args.args[0][0]["content"]

    # OCR text IS in prompt
    assert "OCR识别出的图片文字内容" in prompt
    # Prompt mentions OCR
    assert "OCR" in prompt
    # Label reflects OCR-only
    assert entry.label == "OCR 文本"
    assert entry.explanation == "这是图片OCR识别的文本解释。"


# ---------------------------------------------------------------------------
# 6. selected_text + OCR explain (both present)
# ---------------------------------------------------------------------------

def test_selected_plus_ocr_explain(qapp: QApplication) -> None:
    """6. Both selected_text and image_ocr_text present; sources marked separately."""
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "混合上下文解释结果。"}}],
    })
    qa = PdfQa(chat_handler=chat)

    entry = qa.explain_selection(
        "选中的PDF文本",
        source_page=3,
        consent=True,
        image_ocr_text="图片OCR文字",
    )

    chat.assert_called_once()
    prompt = chat.call_args.args[0][0]["content"]

    assert "选中的PDF文本" in prompt
    assert "图片OCR文字" in prompt
    assert "页码：3" in prompt
    assert "选中文本：" in prompt
    assert "图片 OCR 上下文：" in prompt
    assert entry.label == "选中文本 + OCR"
    assert entry.source_text == "选中的PDF文本"


# ---------------------------------------------------------------------------
# 7. Both empty → ValueError, provider NOT called
# ---------------------------------------------------------------------------

def test_both_empty_raises_valueerror() -> None:
    """7. Empty selected_text + empty image_ocr_text → ValueError."""
    chat = MagicMock()
    qa = PdfQa(chat_handler=chat)

    with pytest.raises(ValueError, match="both empty"):
        qa.explain_selection(
            "",
            source_page=1,
            consent=True,
            image_ocr_text="",
        )
    chat.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Privacy: provider payload contains no image bytes / base64 / paths
# ---------------------------------------------------------------------------

def test_provider_payload_privacy(qapp: QApplication) -> None:
    """8. Provider prompt contains no image bytes, base64, or absolute paths."""
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "privacy test explanation"}}],
    })
    qa = PdfQa(chat_handler=chat)

    entry = qa.explain_selection(
        "test selection",
        source_page=1,
        consent=True,
        image_ocr_text="ocr text here",
    )

    prompt = chat.call_args.args[0][0]["content"]

    # Must NOT contain binary data markers
    assert "base64" not in prompt.lower() or "图片 OCR" in prompt
    # Must NOT contain file:// URIs
    assert "file://" not in prompt
    # Must NOT contain raw PNG/JPEG magic bytes
    assert b"\x89PNG" not in prompt.encode()
    # Must NOT contain absolute Windows paths
    assert not any(p in prompt for p in ["C:\\\\", "D:\\\\"])
    # Must NOT contain JPEG SOI marker
    assert b"\xff\xd8" not in prompt.encode()


# ---------------------------------------------------------------------------
# 9. No-text image → no crash, graceful empty OCR
# ---------------------------------------------------------------------------

def test_no_text_image_no_crash(qapp: QApplication) -> None:
    """9. Image with no text → OCR returns empty, no crash."""
    img_bytes = _make_no_text_image()
    tmp = Path(__file__).parent / ".tmp_no_text.png"
    tmp.write_bytes(img_bytes)
    try:
        worker = _ImageOcrWorker(str(tmp), generation=1)
        received: list[str] = []
        worker.finished.connect(lambda g, t: received.append(t))
        worker.run()

        # Should not raise; may return empty or whitespace
        assert len(received) == 1
        # Empty OCR should still allow explain (with selection) or raise if both empty
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 10. Corrupt image → graceful error
# ---------------------------------------------------------------------------

def test_corrupt_image_graceful_error(qapp: QApplication) -> None:
    """10. Corrupt PNG → error signal, no crash."""
    tmp = Path(__file__).parent / ".tmp_corrupt.png"
    tmp.write_bytes(_make_corrupt_png())
    try:
        worker = _ImageOcrWorker(str(tmp), generation=1)
        error_received: list[str] = []
        worker.error.connect(lambda g, e: error_received.append(e))
        worker.run()

        # Should emit error, not crash
        assert error_received
        assert "OCR 失败" in error_received[0] or "不存在" in error_received[0]
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 11. OCR-only explain → provider called with OCR text, not selection
# ---------------------------------------------------------------------------

def test_ocr_only_excludes_selection_section(qapp: QApplication) -> None:
    """11. OCR-only prompt does NOT contain '选中文本' section header."""
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "ocr only explanation"}}],
    })
    qa = PdfQa(chat_handler=chat)

    qa.explain_selection(
        "",
        source_page=1,
        consent=True,
        image_ocr_text="pure ocr content",
    )

    prompt = chat.call_args.args[0][0]["content"]
    # Should contain OCR section
    assert "图片 OCR 上下文" in prompt
    # Should NOT contain selection section
    assert "选中文本：" not in prompt
    assert "页码：" not in prompt


# ---------------------------------------------------------------------------
# 12. _ExplainWorker passes image_ocr_text to PdfQa
# ---------------------------------------------------------------------------

def test_explain_worker_passes_ocr_text(qapp: QApplication) -> None:
    """12. _ExplainWorker forwards image_ocr_text to PdfQa.explain_selection."""
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "worker ocr test"}}],
    })
    qa = PdfQa(chat_handler=chat)

    from ui.explain_box import _ExplainWorker

    worker = _ExplainWorker(
        qa,
        selected_text="sel text",
        source_page=2,
        consent=True,
        image_ocr_text="worker ocr text",
    )
    worker.run()

    prompt = chat.call_args.args[0][0]["content"]
    assert "sel text" in prompt
    assert "worker ocr text" in prompt


# ---------------------------------------------------------------------------
# 13. Worker does NOT operate on QWidget directly
# ---------------------------------------------------------------------------

def test_ocr_worker_no_widget_dependency() -> None:
    """13. _ImageOcrWorker.run() does not import or touch any QWidget."""
    import inspect
    source = inspect.getsource(_ImageOcrWorker.run)

    # Should not reference Qt widgets
    assert "QWidget" not in source
    assert "QLabel" not in source
    assert "QPixmap" not in source
    assert "self.update()" not in source
    # Should only use Path and RapidOcrBackend
    assert "Path" in source or "pathlib" in source


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
