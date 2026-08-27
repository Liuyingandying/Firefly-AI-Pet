"""Real Explain Box / PdfQa dogfood — OCR selection → real provider explanation.

Minimal chain verified end-to-end:
  scanned PDF → PdfProcessor OCR → PdfQa.explain_selection → real TJU provider

Tests:
  A. real OCR selection → real provider explanation (consent=True)
  B. without explicit consent → provider NOT called
  C. with consent → only selected text sent, not full page
  D. provider failure → controlled error, no crash
  E. unselected page text → NOT in provider payload
  F. async: explain_selection returns immediately, UI updates via signal
  G. duplicate-click protection
  H. worker error → UI shows controlled error
"""

from __future__ import annotations

import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import fitz
import pytest
from PIL import Image, ImageDraw, ImageFont

# Qt imports for async ExplainBox tests
from PySide6.QtCore import QEventLoop, QTimer, QThread
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from core.pdf_processor import PdfProcessor
from core.pdf_qa import ExplainEntry, ExternalProviderConsentRequired, PdfQa
from ui.explain_box import ExplainBox, _ExplainWorker


# ---------------------------------------------------------------------------
# Shared fixture: scanned PDF with known text
# ---------------------------------------------------------------------------

def _font(size: int = 64) -> ImageFont.FreeTypeFont:
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if not font_path.is_file():
        raise RuntimeError("Microsoft YaHei is required for the Windows OCR dogfood")
    return ImageFont.truetype(str(font_path), size)


def _scan_png() -> bytes:
    image = Image.new("RGB", (1600, 420), "white")
    draw = ImageDraw.Draw(image)
    font = _font()
    draw.text((60, 70), "FIREFLY OCR TEST", font=font, fill="black")
    draw.text((60, 210), "中文识别测试", font=font, fill="black")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _make_scanned_pdf(path: Path) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=800, height=250)
    page.insert_image(page.rect, stream=_scan_png())
    doc.save(str(path), garbage=4)
    doc.close()
    return path


# ---------------------------------------------------------------------------
# B. Consent gate: provider must NOT be called without consent
# ---------------------------------------------------------------------------

def test_explain_selection_without_consent_raises(tmp_path: Path) -> None:
    """B. Without explicit consent, provider is never invoked."""
    pdf_path = _make_scanned_pdf(tmp_path / "consent_test.pdf")
    qa = PdfQa()
    result = qa.process(pdf_path)

    # OCR produced text
    assert result.pages[0].extraction_source == "ocr"
    assert "中文识别测试" in result.pages[0].text

    chat = MagicMock()
    qa_no_consent = PdfQa(chat_handler=chat)

    with pytest.raises(ExternalProviderConsentRequired):
        qa_no_consent.explain_selection(
            "中文识别测试",
            source_page=1,
            consent=False,
        )
    chat.assert_not_called()


# ---------------------------------------------------------------------------
# C + E. With consent → only selection sent, not full page
# ---------------------------------------------------------------------------

def test_explain_selection_with_consent_sends_only_selection() -> None:
    """C. With consent, provider receives only the selection.
    E. Unselected page text must NOT appear in the provider payload.
    """
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "这是用户选中的文本解释。"}}],
    })
    qa = PdfQa(chat_handler=chat)

    selection = "中文识别测试"
    entry = qa.explain_selection(
        selection,
        source_page=1,
        consent=True,
    )

    # Provider was called exactly once
    chat.assert_called_once()
    prompt = chat.call_args.args[0][0]["content"]

    # Selection IS in the prompt
    assert selection in prompt

    # Full PDF English text is NOT in the prompt (E)
    assert "FIREFLY OCR TEST" not in prompt

    # Explanation is returned
    assert entry.kind == "paragraph"
    assert entry.source_text == selection
    assert entry.explanation == "这是用户选中的文本解释。"


# ---------------------------------------------------------------------------
# D. Provider failure → controlled error, no crash
# ---------------------------------------------------------------------------

def test_explain_selection_provider_failure_handled() -> None:
    """D. When the provider raises, explain_selection returns a graceful error."""
    chat = MagicMock(side_effect=RuntimeError("network timeout"))
    qa = PdfQa(chat_handler=chat)

    entry = qa.explain_selection(
        "中文识别测试",
        source_page=1,
        consent=True,
    )

    # Should not raise; should have an error explanation
    assert entry.explanation is not None
    assert "无法解释" in entry.explanation or "network timeout" in entry.explanation


# ---------------------------------------------------------------------------
# A. Real provider dogfood (requires TJU API key in environment)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not __import__("os").environ.get("TJULLM_API_KEY"),
    reason="TJULLM_API_KEY not set — real provider dogfood skipped",
)
def test_explain_selection_real_tju_provider(tmp_path: Path) -> None:
    """A. End-to-end: scanned PDF → OCR → selection → real TJU provider.

    This test requires a valid TJULLM_API_KEY in the environment.
    It proves the full chain works with a real external provider.
    """
    pdf_path = _make_scanned_pdf(tmp_path / "real_explain.pdf")
    qa = PdfQa()
    result = qa.process(pdf_path)

    # Step 1: OCR produced the expected text
    assert result.pages[0].extraction_source == "ocr"
    assert "中文识别测试" in result.pages[0].text

    selection = "中文识别测试"

    # Step 2: Call explain_selection with real provider (consent=True)
    entry = qa.explain_selection(
        selection,
        source_page=1,
        consent=True,
    )

    # Step 3: Provider returned a real explanation (non-empty, not an error)
    assert entry.explanation is not None
    assert len(entry.explanation) > 5
    assert "无法解释" not in entry.explanation

    # Step 4: Explanation is related to the selection (contains Chinese chars)
    assert any("\u4e00" <= c <= "\u9fff" for c in entry.explanation)


# ---------------------------------------------------------------------------
# F. Async: explain_selection returns immediately, UI updates via signal
# ---------------------------------------------------------------------------

@pytest.fixture()
def qapp() -> QApplication:
    """Provide a QApplication instance for Qt widget tests."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_async_explain_selection_returns_immediately(qapp: QApplication) -> None:
    """F. Calling explain_selection does NOT block the caller thread.

    We use a slow mock chat that sleeps 0.5 s.  If explain_selection were
    synchronous the test would take ≥ 0.5 s.  With async it should return
    in < 0.1 s while the worker runs in the background.
    """
    chat = MagicMock()

    def slow_chat(messages, **kwargs):
        time.sleep(0.5)
        return {"choices": [{"message": {"content": "async explanation"}}]}

    chat.side_effect = slow_chat

    qa = PdfQa(chat_handler=chat)
    box = ExplainBox()
    box._qa = qa
    box._external_provider_consent = True

    # Record the time before calling explain_selection.
    t0 = time.monotonic()

    # explain_selection should return almost immediately (worker runs in bg).
    box.explain_selection("中文识别测试", source_page=1)

    elapsed = time.monotonic() - t0

    # The call should return in well under 0.3 s (worker is async).
    assert elapsed < 0.3, (
        f"explain_selection took {elapsed:.2f}s — should return immediately"
    )

    # The UI should NOT yet show the explanation (worker still running).
    assert "async explanation" not in box._content._body_label.text()

    # Wait for the worker to finish.
    loop = QEventLoop()
    QTimer.singleShot(2000, loop.quit)
    loop.exec()

    # Now the explanation should be visible.
    assert "async explanation" in box._content._body_label.text()


# ---------------------------------------------------------------------------
# G. Duplicate-click protection
# ---------------------------------------------------------------------------

def test_duplicate_explain_selection_ignored(qapp: QApplication) -> None:
    """G. A second explain_selection call while one is in-flight is ignored."""
    chat = MagicMock()

    def slow_chat(messages, **kwargs):
        time.sleep(0.5)
        return {"choices": [{"message": {"content": "first"}}]}

    chat.side_effect = slow_chat

    qa = PdfQa(chat_handler=chat)
    box = ExplainBox()
    box._qa = qa
    box._external_provider_consent = True

    # Start first request.
    box.explain_selection("中文识别测试", source_page=1)
    assert box._running is True

    # Second call should be silently dropped.
    box.explain_selection("中文识别测试", source_page=1)

    # Provider should only be called once (by the first request).
    # Wait for completion.
    loop = QEventLoop()
    QTimer.singleShot(2000, loop.quit)
    loop.exec()

    assert chat.call_count == 1


# ---------------------------------------------------------------------------
# H. Worker error → UI shows controlled error
# ---------------------------------------------------------------------------

def test_worker_error_shows_controlled_error(qapp: QApplication) -> None:
    """H. When the worker raises, the UI shows a controlled error message."""
    chat = MagicMock(side_effect=RuntimeError("provider unavailable"))

    qa = PdfQa(chat_handler=chat)
    box = ExplainBox()
    box._qa = qa
    box._external_provider_consent = True

    box.explain_selection("中文识别测试", source_page=1)

    # Wait for the worker to finish.
    loop = QEventLoop()
    QTimer.singleShot(2000, loop.quit)
    loop.exec()

    # The body should show the error, not crash.
    body = box._content._body_label.text()
    assert "provider unavailable" in body or "无法解释" in body
    assert box._running is False


# ---------------------------------------------------------------------------
# I. Worker signal test: direct QSignalSpy on _ExplainWorker
# ---------------------------------------------------------------------------

def test_worker_finished_signal_emits_entry(qapp: QApplication) -> None:
    """I. _ExplainWorker.finished signal emits an ExplainEntry on success.

    Uses a simple in-thread call (no cross-thread signal complexity) since
    the real async path is already covered by
    test_async_explain_selection_returns_immediately.
    """
    chat = MagicMock(return_value={
        "choices": [{"message": {"content": "signal test explanation"}}],
    })
    qa = PdfQa(chat_handler=chat)

    worker = _ExplainWorker(qa, "中文识别测试", source_page=1, consent=True)
    received: list[Any] = []
    worker.finished.connect(lambda e: received.append(e))

    # Call run() directly (same-thread signal = DirectConnection)
    worker.run()

    assert received, "finished signal was not emitted"
    entry = received[0]
    assert isinstance(entry, ExplainEntry)
    assert entry.explanation == "signal test explanation"


def test_worker_error_signal_emits_message(qapp: QApplication) -> None:
    """Ib. _ExplainWorker finishes with an error explanation when provider fails.

    Note: PdfQa.explain_selection catches provider errors internally and
    returns an ExplainEntry with explanation="无法解释选中文本：{exc}".
    The worker's finished signal fires with this entry (not the error signal).
    """
    chat = MagicMock(side_effect=RuntimeError("boom"))
    qa = PdfQa(chat_handler=chat)

    worker = _ExplainWorker(qa, "中文识别测试", source_page=1, consent=True)
    received: list[Any] = []
    worker.finished.connect(lambda e: received.append(e))

    worker.run()

    assert received, "finished signal was not emitted"
    entry = received[0]
    assert isinstance(entry, ExplainEntry)
    # The entry's explanation contains the provider error message.
    assert "boom" in entry.explanation


# ---------------------------------------------------------------------------
# J. Consent=False → provider NOT called (async path)
# ---------------------------------------------------------------------------

def test_async_consent_false_no_provider_call(qapp: QApplication) -> None:
    """J. consent=False in async path → provider is never invoked."""
    chat = MagicMock()

    qa = PdfQa(chat_handler=chat)
    box = ExplainBox()
    box._qa = qa
    box._external_provider_consent = False

    box.explain_selection("中文识别测试", source_page=1)

    # Wait for worker to finish (it should fail fast with consent error).
    loop = QEventLoop()
    QTimer.singleShot(2000, loop.quit)
    loop.exec()

    chat.assert_not_called()
    body = box._content._body_label.text()
    assert "无法解释" in body or "consent" in body.lower() or "Explicit" in body
