"""Companion Image Attachment v1 tests.

The Companion accepts one in-memory image per turn via drag & drop, Ctrl+V
(clipboard image) or the ``+`` file picker, and answers from the image through
DeepSeek Vision one-shot — with exactly one vision remote call, zero screen
captures and zero reasoning calls. The image never touches disk, memory, or
conversation history beyond the safe ``[Image attachment]`` placeholder.

Coverage map (A-X from the task):
  A drag png -> attachment        B drag jpg -> attachment
  C drag txt ignored              D paste image -> attachment
  E paste text unchanged          F file picker -> attachment
  G x removes attachment          H second image replaces first
  I image+text -> 1 vision call   J screen capture calls == 0
  K reasoning calls == 0          L empty text -> internal default question
  M text only -> 0 vision calls   N preprocessing keeps aspect ratio
  O >20MB rejected                P corrupt/unsupported rejected safely
  Q success -> attachment cleared R failure -> attachment retained
  S safe history placeholder      T no memory/suggestion path
  U no screenshot triggered       V payload carries the attachment image
  W payload carries the question  X no full memory/history sent
Plus: attachment beats "看看我的屏幕"; explicit negation falls through.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from PIL import Image as PILImage
from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import (
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeyEvent,
    QKeySequence,
)
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

from core.agent_events import AgentEventType
from core.screen_vision.models import ScreenObservation, ScreenVisionResult
from core.screen_vision.provider_errors import EmptyProviderResponse
from ui import companion_chat_window as window_mod
from ui.companion_attachment import (
    DEFAULT_ATTACHMENT_QUESTION,
    HISTORY_IMAGE_PLACEHOLDER,
    MAX_ATTACHMENT_BYTES,
    AttachmentError,
    attachment_to_frame,
    decode_attachment_bytes,
    load_attachment_file,
)
from ui.companion_chat_window import CompanionChatWindow
from ui.character_conversation_runner import CharacterConversationRunner


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------- fakes


class FakeDirectVision:
    """Records answer_direct calls; no network involved."""

    name = "deepseek-vision"
    model = "deepseek-v4-flash-vision-exp"

    def __init__(self, answer="这是一张图片。", error=None):
        self.calls = []
        self.answer = answer
        self.error = error

    def answer_direct(self, frame, question, style_context=None):
        if self.error is not None:
            raise self.error
        self.calls.append(
            {"frame": frame, "question": question, "style_context": style_context}
        )
        return self.answer


class FakeRuntime:
    """Text-chat stand-in; its call would be the memory/suggestion path."""

    def __init__(self):
        self.chat_calls = []

    def chat(self, prompt, history=None, turn_context=None):
        self.chat_calls.append(
            {"prompt": prompt, "history": history, "turn_context": turn_context}
        )
        return {"choices": [{"message": {"role": "assistant", "content": "普通回复"}}]}


class FakeScreenVisionService:
    """Records look() calls; would be the screen-capture path."""

    def __init__(self):
        self.look_calls = []

    def look(self, user_question, capture_mode="last_non_firefly_window"):
        self.look_calls.append({"question": user_question, "capture_mode": capture_mode})
        return ScreenVisionResult(
            observation=ScreenObservation(),
            answer="屏幕内容回复",
            timings={"total_ms": 1.0},
            meta={"direct_one_shot": True, "remote_calls": 1, "reasoning_calls": 0},
        )


# ---------------------------------------------------------------- helpers


def _qimage(width=64, height=32, color=0xFF33AA):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(color)
    return image


def _png_bytes(width=64, height=32):
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (200, 100, 50)).save(buffer, "PNG")
    return buffer.getvalue()


def _jpg_bytes(width=64, height=32):
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (50, 100, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


def _attachment_file(tmp_path, name="photo.png", width=64, height=32):
    path = tmp_path / name
    path.write_bytes(_png_bytes(width, height))
    return load_attachment_file(path)


def _runner(fake_direct, fake_screen=None, fake_runtime=None):
    return CharacterConversationRunner(
        runtime=fake_runtime or FakeRuntime(),
        screen_vision_service=fake_screen,
        attachment_vision_provider=fake_direct,
    )


def _window(runner=None) -> CompanionChatWindow:
    return CompanionChatWindow(
        runner=runner or CharacterConversationRunner(runtime=FakeRuntime())
    )


def _paste_event():
    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key_V, Qt.ControlModifier)


def _drag_enter(window, mime):
    event = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.dragEnterEvent(event)
    return event.isAccepted()


def _drop(window, mime):
    event = QDropEvent(
        QPointF(10, 10), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier,
        QEvent.Type.Drop,
    )
    window.dropEvent(event)
    return event.isAccepted()


def _wait_turn(window, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and window.runner.running:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()


# ================================================== A/B/C drag & drop


def test_a_drag_png_file_adds_attachment(tmp_path, qapp):
    window = _window()
    path = tmp_path / "drag.png"
    path.write_bytes(_png_bytes())
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    assert _drag_enter(window, mime)
    assert _drop(window, mime)
    assert window._pending_attachment is not None
    assert window._pending_attachment.display_name == "drag.png"
    assert not window.attachment_row.isHidden()


def test_b_drag_jpg_image_mime_adds_attachment(qapp):
    window = _window()
    mime = QMimeData()
    mime.setImageData(_qimage(48, 24))
    assert _drag_enter(window, mime)
    assert _drop(window, mime)
    assert window._pending_attachment is not None


def test_c_drag_txt_ignored(tmp_path, qapp):
    window = _window()
    path = tmp_path / "note.txt"
    path.write_text("hello", encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    assert not _drag_enter(window, mime)
    _drop(window, mime)
    assert window._pending_attachment is None
    assert window.attachment_row.isHidden()


# ================================================== D/E clipboard


def test_d_paste_image_adds_attachment(qapp):
    window = _window()
    QApplication.clipboard().clear()
    QApplication.clipboard().setImage(_qimage(50, 25))
    window.input.keyPressEvent(_paste_event())
    assert window._pending_attachment is not None
    assert window._pending_attachment.display_name == "剪贴板图片"
    QApplication.clipboard().clear()


def test_e_paste_text_unchanged(qapp):
    window = _window()
    QApplication.clipboard().clear()
    QApplication.clipboard().setText("plain text paste")
    window.input.keyPressEvent(_paste_event())
    assert window.input.text() == "plain text paste"   # normal text paste kept
    assert window._pending_attachment is None          # no image attached
    QApplication.clipboard().clear()


# ================================================== F/G/H attachment UI


def test_f_file_picker_adds_attachment(tmp_path, qapp, monkeypatch):
    path = tmp_path / "pick.jpg"
    path.write_bytes(_jpg_bytes())
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        staticmethod(lambda *a, **k: (str(path), "")),
    )
    window = _window()
    window._pick_image()
    assert window._pending_attachment is not None
    assert window._pending_attachment.display_name == "pick.jpg"


def test_g_remove_button_clears_attachment(tmp_path, qapp):
    window = _window()
    window._set_attachment(_attachment_file(tmp_path))
    assert window._pending_attachment is not None
    close = window._chip.findChild(QPushButton)
    close.click()
    assert window._pending_attachment is None
    assert window.attachment_row.isHidden()


def test_h_second_image_replaces_first(tmp_path, qapp):
    window = _window()
    first = _attachment_file(tmp_path, "one.png")
    second_path = tmp_path / "two.jpg"
    second_path.write_bytes(_jpg_bytes())
    second = load_attachment_file(second_path)
    window._set_attachment(first)
    window._set_attachment(second)
    assert window._pending_attachment is second
    assert window._pending_attachment.display_name == "two.jpg"
    assert window.attachment_layout.count() == 1  # exactly one chip


# ================================================== I/J/K/L/M runner semantics


def test_i_image_plus_question_exactly_one_vision_call(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    events, answer = runner.perform_with_image("看看这张图", _attachment_file(tmp_path))
    assert len(fake.calls) == 1
    assert runner.last_attachment_meta["remote_calls"] == 1
    assert answer == "这是一张图片。"
    assert events[0].type is AgentEventType.FINAL


def test_j_image_turn_never_captures_screen(tmp_path, qapp):
    fake = FakeDirectVision()
    fake_screen = FakeScreenVisionService()
    runner = _runner(fake, fake_screen=fake_screen)
    runner.perform_with_image("看看这张图", _attachment_file(tmp_path))
    assert fake_screen.look_calls == []
    assert runner.last_attachment_meta["screen_capture_calls"] == 0


def test_k_image_turn_has_no_reasoning(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    runner.perform_with_image("看看这张图", _attachment_file(tmp_path))
    assert runner.last_attachment_meta["reasoning_calls"] == 0


def test_l_image_without_text_uses_default_question(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    runner.perform_with_image("", _attachment_file(tmp_path))
    assert len(fake.calls) == 1
    assert fake.calls[0]["question"] == DEFAULT_ATTACHMENT_QUESTION


def test_m_text_without_image_uses_normal_chat(qapp):
    fake = FakeDirectVision()
    fake_runtime = FakeRuntime()
    runner = _runner(fake, fake_runtime=fake_runtime)
    runner.perform("你好")
    assert fake.calls == []                        # 0 vision calls
    assert len(fake_runtime.chat_calls) == 1       # normal text chat path


# ================================================== N/O/P preprocessing & limits


def test_n_preprocessing_preserves_aspect_ratio(tmp_path, qapp):
    attach = _attachment_file(tmp_path, "wide.png", width=2000, height=1000)
    frame = attachment_to_frame(attach)
    assert frame.mime_type == "image/jpeg"
    assert frame.width == 1600 and frame.height == 800   # longest edge -> 1600
    assert frame.image_bytes[:3] == b"\xff\xd8\xff"      # JPEG payload


def test_n2_preprocessing_never_upscales_small_images(tmp_path, qapp):
    attach = _attachment_file(tmp_path, "small.png", width=100, height=50)
    frame = attachment_to_frame(attach)
    assert frame.width == 100 and frame.height == 50


def test_o_over_20mb_rejected():
    with pytest.raises(AttachmentError) as exc:
        decode_attachment_bytes(
            b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_ATTACHMENT_BYTES), "huge.png"
        )
    assert "20MB" in str(exc.value)


def test_o2_over_20mb_file_rejected(tmp_path):
    path = tmp_path / "huge.png"
    with open(path, "wb") as handle:
        handle.seek(MAX_ATTACHMENT_BYTES)  # sparse >20MB without huge memory
        handle.write(b"\x00")
    with pytest.raises(AttachmentError) as exc:
        load_attachment_file(path)
    assert "20MB" in str(exc.value)


def test_p_corrupt_and_unsupported_rejected_safely():
    with pytest.raises(AttachmentError) as exc:
        decode_attachment_bytes(b"this is not an image at all", "x.png")
    assert "无法读取" in str(exc.value)
    # PNG magic but truncated body -> undecodable
    with pytest.raises(AttachmentError):
        decode_attachment_bytes(b"\x89PNG\r\n\x1a\nbroken", "x.png")
    # PDF must never be treated as an image
    with pytest.raises(AttachmentError):
        decode_attachment_bytes(b"%PDF-1.4 fake pdf payload", "doc.pdf")


# ================================================== Q/R lifecycle


def test_q_success_clears_attachment(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    window = _window(runner)
    window._set_attachment(_attachment_file(tmp_path))
    window.input.setText("看看这张图")
    window._send()
    _wait_turn(window)
    assert window._pending_attachment is None          # consumed on success
    assert window.attachment_row.isHidden()


def test_r_failure_retains_attachment(tmp_path, qapp):
    fake = FakeDirectVision(error=EmptyProviderResponse())
    runner = _runner(fake)
    window = _window(runner)
    window._set_attachment(_attachment_file(tmp_path))
    window.input.setText("看看这张图")
    window._send()
    _wait_turn(window)
    assert window._pending_attachment is not None      # kept for retry
    assert not window.attachment_row.isHidden()


# ================================================== S/T privacy isolation


def test_s_history_holds_only_safe_placeholder(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    runner.perform_with_image("看看这张图", _attachment_file(tmp_path))
    history = runner.history
    assert HISTORY_IMAGE_PLACEHOLDER in history[0]["content"]
    assert history[-1]["content"] == "这是一张图片。"
    blob = json.dumps(history, ensure_ascii=False)
    assert "data:image" not in blob
    assert "base64" not in blob
    assert str(tmp_path) not in blob                    # no local path


def test_t_no_memory_suggestion_path_used(tmp_path, qapp):
    fake = FakeDirectVision()
    fake_runtime = FakeRuntime()
    runner = _runner(fake, fake_runtime=fake_runtime)
    runner.perform_with_image("看看这张图", _attachment_file(tmp_path))
    assert fake_runtime.chat_calls == []                # runtime/memory path untouched


# ================================================== U/V/W/X payload integrity


def test_u_no_screenshot_triggered_for_attachment_request(tmp_path, qapp):
    fake = FakeDirectVision()
    fake_screen = FakeScreenVisionService()
    runner = _runner(fake, fake_screen=fake_screen)
    runner.perform_with_image("看看这张图", _attachment_file(tmp_path))
    assert fake_screen.look_calls == []


def test_v_payload_contains_attachment_image(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    attach = _attachment_file(tmp_path, "wide.png", width=640, height=480)
    runner.perform_with_image("看看这张图", attach)
    frame = fake.calls[0]["frame"]
    assert frame.image_bytes                                  # non-empty JPEG
    assert frame.mime_type == "image/jpeg"
    assert frame.image_bytes[:3] == b"\xff\xd8\xff"
    assert (frame.width, frame.height) == (640, 480)          # this attachment


def test_w_payload_contains_user_question(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    runner.perform_with_image("这张照片里有什么", _attachment_file(tmp_path))
    assert fake.calls[0]["question"] == "这张照片里有什么"


def test_x_full_history_not_sent(tmp_path, qapp):
    fake = FakeDirectVision()
    runner = _runner(fake)
    runner.perform_with_image("看看这张图", _attachment_file(tmp_path))
    call = fake.calls[0]
    assert set(call) == {"frame", "question", "style_context"}  # bounded input
    assert call["style_context"]                                # minimal persona


# ================================================== attachment priority / negation


def test_attachment_prioritized_over_screen_request(tmp_path, qapp):
    fake = FakeDirectVision()
    fake_screen = FakeScreenVisionService()
    runner = _runner(fake, fake_screen=fake_screen)
    runner.perform_with_image("看看我的屏幕", _attachment_file(tmp_path))
    assert len(fake.calls) == 1          # answered from the attachment
    assert fake_screen.look_calls == []   # no screen capture


def test_negation_falls_through_to_normal_turn(tmp_path, qapp):
    fake = FakeDirectVision()
    fake_screen = FakeScreenVisionService()
    runner = _runner(fake, fake_screen=fake_screen)
    runner.perform_with_image("不要看这张图，看看我的屏幕", _attachment_file(tmp_path))
    assert fake.calls == []
    assert fake_screen.look_calls         # screen vision allowed after negation
