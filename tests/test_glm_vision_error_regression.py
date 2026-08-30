"""Regression for the manual-dogfood NameError (2026-08-30, evening).

Real flow that failed: explicit look -> TJU vision 500 -> Zhipu GLM-4.6V
vision fallback -> Zhipu free-tier HTTP 429 -> glm_vision's 429 retry path
referenced an unimported ProviderHTTPError -> NameError surfaced to the
companion as "视觉模块这次没能看成屏幕（NameError）".

These tests run the REAL provider objects with the transport stubbed, so the
old code fails them with NameError and the fixed code passes.
"""

from datetime import datetime

import pytest

import providers.base as pbase
import providers.base
from providers.zhipu_glm import ZhipuGLMProvider

from core.screen_vision import foreground_tracker as ft_module
from core.screen_vision.foreground_tracker import ForegroundContextTracker
from core.screen_vision.failover import FailoverVisionProvider
from core.screen_vision.models import ScreenFrame, ScreenObservation
from core.screen_vision.provider_errors import (
    ProviderHTTPError,
    VisionTemporarilyUnavailable,
)
from core.screen_vision.vision import glm_vision
from core.screen_vision.vision.glm_vision import GlmVisionProvider
from core.screen_vision.vision.qwen_vision import QwenVisionProvider


def _frame():
    return ScreenFrame(32, 16, "image/jpeg", b"\xff\xd8x", datetime.now())


class _Transport429:
    """Zhipu transport that always answers the free-tier rate limit."""

    def __init__(self, status=429):
        self.status = status
        self.calls = 0

    def __call__(self, endpoint, payload, headers, timeout):
        self.calls += 1
        raise pbase.ProviderHTTPError(self.status, "rate limited")


def _glm_with_failing_transport(status=429):
    provider = GlmVisionProvider(provider=ZhipuGLMProvider(api_key="k"))
    failing = _Transport429(status)
    return provider, failing


def test_glm_transport_error_is_classified_not_nameerror(monkeypatch):
    """THE regression: the 429 retry path must raise ProviderHTTPError.
    The pre-fix code raised NameError here."""
    provider, failing = _glm_with_failing_transport(429)
    monkeypatch.setattr(glm_vision, "default_transport", failing)
    monkeypatch.setattr(glm_vision.time, "sleep", lambda s: None)
    with pytest.raises(ProviderHTTPError) as excinfo:
        provider.inspect(_frame())
    assert excinfo.value.status_code == 429
    assert failing.calls == 2  # exactly one bounded retry, then propagate


def test_glm_non_429_transport_error_propagates_once(monkeypatch):
    provider, failing = _glm_with_failing_transport(401)
    monkeypatch.setattr(glm_vision, "default_transport", failing)
    monkeypatch.setattr(glm_vision.time, "sleep", lambda s: None)
    with pytest.raises(ProviderHTTPError) as excinfo:
        provider.inspect(_frame())
    assert excinfo.value.status_code == 401
    assert failing.calls == 1  # credential errors get no retry


def test_full_chain_tju500_glm429_is_temporarily_unavailable(monkeypatch):
    """The exact manual-dogfood scenario: TJU 500 -> GLM fallback 429 ->
    a clean VisionTemporarilyUnavailable, never NameError."""
    # isolate credential resolution
    import providers.base as base

    monkeypatch.setattr(base, "DEFAULT_ENV_FILE", _empty_env_file())
    monkeypatch.setenv("TJULLM_API_KEY", "tju-key")
    monkeypatch.setenv("ZHIPU_API_KEY", "zhipu-key")

    # primary: REAL QwenVisionProvider with transport answering TJU's 500
    primary = QwenVisionProvider()
    monkeypatch.setattr(
        "core.screen_vision.vision.qwen_vision.requests.post",
        lambda *a, **k: _http_response(500, "Internal Server Error"),
    )
    # fallback: REAL GlmVisionProvider with transport answering Zhipu's 429
    glm, failing = _glm_with_failing_transport(429)
    monkeypatch.setattr(glm_vision, "default_transport", failing)
    monkeypatch.setattr(glm_vision.time, "sleep", lambda s: None)

    chain = FailoverVisionProvider(primary=primary, fallback=glm)
    with pytest.raises(VisionTemporarilyUnavailable) as excinfo:
        chain.inspect(_frame())
    assert excinfo.value.failure_type.startswith("HTTP_")
    assert failing.calls == 2


class _HttpResponse:
    def __init__(self, status, text):
        self.status_code = status
        self.ok = status < 400
        self.text = text if status >= 400 else "ok"
        self._payload = {"choices": [{"message": {"content": text}}]} if status < 400 else {
            "error": {"message": text}
        }

    def json(self):
        return self._payload


def _http_response(status, text):
    return _HttpResponse(status, text)


def _empty_env_file():
    import tempfile
    from pathlib import Path

    handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    handle.write("")
    handle.close()
    path = Path(handle.name)
    import providers.base as base

    base.DEFAULT_ENV_FILE = path  # stays for the process lifetime of these tests
    return path


# ------------------------------------------------ gate/target consistency


def test_companion_phrases_pass_the_explicit_gate():
    """Second real gap found during reproduction: "看看这个聊天框" passed the
    target resolver but was rejected by the explicit-look gate, so scenario 3
    never captured anything."""
    from core.screen_vision.trigger import (
    is_explicit_screen_vision_request,
    resolve_capture_target,
)

    for text in ("流萤，看看这个聊天框", "看看你的窗口", "看看流萤窗口"):
        assert is_explicit_screen_vision_request(text), text
        assert resolve_capture_target(text) == "firefly_companion"


def test_degenerate_sliver_crop_is_rejected():
    """Third real gap: an 8x1080 crop sliver of an off-screen window was
    returned as a "screenshot". Degenerate crops must fall back instead."""
    from core.screen_vision.screen.capture import _crop_rect_for_rect

    class Screen:
        devicePixelRatio = lambda self: 1.0  # noqa: E731

        def geometry(self):
            return type("G", (), {
                "x": lambda self: 0, "y": lambda self: 0,
                "width": lambda self: 1000, "height": lambda self: 800,
            })()

    assert _crop_rect_for_rect((962, 0, 970, 1080), Screen(), None) is None
    assert _crop_rect_for_rect((0, 790, 1000, 800), Screen(), None) is None
    assert _crop_rect_for_rect((0, 0, 800, 600), Screen(), None) == (0, 0, 800, 600)
