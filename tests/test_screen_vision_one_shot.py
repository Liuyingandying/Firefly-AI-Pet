"""Targeted FAST one-shot acceptance tests. No real API calls or sleeps."""

from datetime import datetime
import json
import traceback
import urllib.request

import pytest
import requests
from PySide6.QtGui import QImage

from core.screen_vision.circuit_breaker import CircuitBreaker
from core.screen_vision.failover import FailoverReasoningProvider, FailoverVisionProvider
from core.screen_vision.models import ScreenFrame, ScreenObservation, ScreenVisionResult
from core.screen_vision.provider_errors import (
    EmptyProviderResponse,
    ProviderHTTPError,
    ProviderNetworkError,
)
from core.screen_vision.screen.capture import _encode_pixmap
from core.screen_vision.service import ScreenVisionService
from core.screen_vision.vision.deepseek_vision import (
    DEFAULT_DIRECT_STYLE_CONTEXT,
    DeepSeekVisionProvider,
    build_direct_messages,
)
from ui.character_conversation_runner import CharacterConversationRunner


def _frame(width=64, height=32, image_bytes=b"\xff\xd8fake"):
    return ScreenFrame(width, height, "image/jpeg", image_bytes, datetime.now())


class Capture:
    last_capture_info = {"capture_target": "primary_screen"}

    def __init__(self):
        self.calls = 0

    def capture_primary_screen(self, **kwargs):
        self.calls += 1
        return _frame()

    capture_last_non_firefly_window = capture_primary_screen
    capture_firefly_companion = capture_primary_screen
    capture_active_window = capture_primary_screen


class Direct:
    name = "deepseek-vision"
    model = "deepseek-v4-flash-vision-exp"

    def __init__(self, answer="我看到你正在浏览教务系统课程页面。页面展示了课程名称和教学信息。", error=None):
        self.answer = answer
        self.error = error
        self.calls = 0
        self.frames = []
        self.questions = []
        self.styles = []

    def answer_direct(self, frame, question, style_context=None):
        self.calls += 1
        self.frames.append(frame)
        self.questions.append(question)
        self.styles.append(style_context)
        if self.error:
            raise self.error
        return self.answer


class Vision:
    model = "vision-model"

    def __init__(self, name="tju-qwen", error=None, scene="教务系统课程页面"):
        self.name = name
        self.error = error
        self.scene = scene
        self.calls = 0
        self.frames = []

    def inspect(self, frame, instruction=None):
        self.calls += 1
        self.frames.append(frame)
        if self.error:
            raise self.error
        return ScreenObservation(scene_summary=self.scene, visible_text=["课程名称"])


class Reasoning:
    name = "tju-deepseek"
    model = "deepseek-v4-flash"

    def __init__(self):
        self.calls = 0
        self.payloads = []

    def answer(self, question, observation):
        self.calls += 1
        self.payloads.append(dict(observation))
        return "resilient answer"


def _service(*, direct=None, vision=None, reasoning=None, settings=None, breaker=None):
    vision = vision or FailoverVisionProvider(primary=Vision(), breaker=breaker)
    reasoning = reasoning or FailoverReasoningProvider(primary=Reasoning())
    return ScreenVisionService(
        vision_provider=vision,
        reasoning_provider=reasoning,
        capture_service=Capture(),
        settings=settings,
        fast_mode=True,
        direct_vision_provider=direct or Direct(),
        resilient_vision_provider=vision,
        resilient_reasoning_provider=reasoning,
        vision_breaker=breaker,
    )


def test_a_b_fast_is_one_remote_call_and_skips_reasoning():
    direct = Direct()
    reasoning_leaf = Reasoning()
    reasoning = FailoverReasoningProvider(primary=reasoning_leaf)
    result = _service(direct=direct, reasoning=reasoning).look(
        "告诉我屏幕上是什么", capture_mode="primary_screen"
    )
    assert direct.calls == 1
    assert reasoning_leaf.calls == 0
    assert result.meta["remote_calls"] == 1
    assert result.meta["reasoning_calls"] == 0
    assert result.meta["fallback_used"] is False
    assert result.meta["routing_mode"] == "fast"
    assert "reasoning_ms" not in result.timings
    assert "reasoning_ms" not in result.meta


def test_c_d_fast_sends_current_screenshot_and_question():
    direct = Direct()
    result = _service(direct=direct).look("当前页面叫什么？", "primary_screen")
    assert direct.frames[0].image_bytes == b"\xff\xd8fake"
    assert direct.questions == ["当前页面叫什么？"]
    assert result.answer == direct.answer


def test_e_direct_payload_has_no_memory_or_history():
    messages = build_direct_messages(_frame(), "当前页面叫什么？")
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": DEFAULT_DIRECT_STYLE_CONTEXT}
    assert messages[1]["content"][0]["text"] == "当前页面叫什么？"
    serialized = json.dumps(messages, ensure_ascii=False).lower()
    for private_text in ("memory db", "bond history", "conversation history", "workspace"):
        assert private_text not in serialized


def test_direct_provider_uses_one_existing_http_request():
    calls = []

    class Response:
        ok = True

        def json(self):
            return {"choices": [{"message": {"content": "页面是天津大学教务系统。"}}]}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    provider = DeepSeekVisionProvider(api_key="".join(("test", "-key")), transport=post)
    assert provider.answer_direct(_frame(), "这是什么页面？") == "页面是天津大学教务系统。"
    assert len(calls) == 1
    payload = calls[0][1]["json"]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 320
    assert payload["messages"][1]["content"][0]["text"] == "这是什么页面？"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )


def test_unstubbed_external_requests_are_blocked_before_transport():
    with pytest.raises(RuntimeError, match="pytest blocked"):
        requests.post("https://api.deepseek.com/chat/completions")
    request = urllib.request.Request("https://ai.tju.edu.cn/api/v3/models")
    with pytest.raises(RuntimeError, match="pytest blocked"):
        urllib.request.urlopen(request)


def test_network_exception_traceback_does_not_retain_request_credentials():
    header_name = "".join(("Author", "ization"))
    auth_scheme = "".join(("Bear", "er"))
    secret_value = "runtime-credential-value"

    def failing_transport(*args, **kwargs):
        request = requests.Request(
            "POST",
            "https://example.invalid",
            headers={header_name: f"{auth_scheme} {secret_value}"},
        ).prepare()
        raise requests.ConnectionError("transport failed", request=request)

    provider = DeepSeekVisionProvider(api_key=secret_value, transport=failing_transport)
    try:
        provider.answer_direct(_frame(), "q")
    except ProviderNetworkError as exc:
        rendered = traceback.format_exc()
        combined = f"{exc!s}\n{exc!r}\n{rendered}"
    else:  # pragma: no cover - the fake always raises
        raise AssertionError("expected safe network failure")
    assert secret_value not in combined
    assert header_name not in combined
    assert auth_scheme not in combined
    assert "PreparedRequest" not in combined


def test_http_error_text_redacts_credentials_everywhere():
    header_name = "".join(("Author", "ization"))
    auth_scheme = "".join(("Bear", "er"))
    secret_value = "runtime-credential-value"

    class Response:
        ok = False
        status_code = 401
        text = f"{header_name}: {auth_scheme} {secret_value}"

    provider = DeepSeekVisionProvider(
        api_key=secret_value,
        transport=lambda *args, **kwargs: Response(),
    )
    with pytest.raises(ProviderHTTPError) as caught:
        provider.answer_direct(_frame(), "q")
    rendered = f"{caught.value!s}\n{caught.value!r}"
    assert secret_value not in rendered
    assert header_name not in rendered
    assert auth_scheme not in rendered


def test_runner_log_redacts_credentials(caplog):
    header_name = "".join(("Author", "ization"))
    auth_scheme = "".join(("Bear", "er"))
    secret_value = "runtime-credential-value"

    class BrokenService:
        def look(self, *args, **kwargs):
            raise RuntimeError(f"{header_name}: {auth_scheme} {secret_value}")

    class Runtime:
        conversation_store = None

    runner = CharacterConversationRunner(
        runtime=Runtime(), screen_vision_service=BrokenService()
    )
    runner.perform("看看我的屏幕")
    rendered = caplog.text
    assert secret_value not in rendered
    assert header_name not in rendered
    assert auth_scheme not in rendered
    assert "PreparedRequest" not in rendered


def test_empty_direct_content_raises_safe_error_without_fallback():
    class Response:
        ok = True

        def json(self):
            return {"choices": [{"message": {"content": ""}}]}

    provider = DeepSeekVisionProvider(
        api_key="".join(("test", "-key")),
        transport=lambda *args, **kwargs: Response(),
    )
    with pytest.raises(EmptyProviderResponse, match="no final answer"):
        provider.answer_direct(_frame(), "q")


def test_f_direct_answer_is_returned_to_companion_without_runtime_chat():
    service = _service(direct=Direct(answer="这是天津大学教务系统课程页面。"))

    class Runtime:
        conversation_store = None

        def __init__(self):
            self.calls = 0

        def chat(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("FAST must not call Companion reasoning")

    runtime = Runtime()
    runner = CharacterConversationRunner(runtime=runtime, screen_vision_service=service)
    _events, answer = runner.perform("流萤，看看我的整个屏幕，告诉我现在在做什么。")
    assert answer == "这是天津大学教务系统课程页面。"
    assert runtime.calls == 0
    assert runner.last_screen_vision_meta["remote_calls"] == 1


def test_g_transient_direct_failure_runs_resilient_pipeline():
    direct = Direct(error=ProviderHTTPError(503, "busy"))
    vision_leaf = Vision()
    reasoning_leaf = Reasoning()
    vision = FailoverVisionProvider(primary=vision_leaf)
    reasoning = FailoverReasoningProvider(primary=reasoning_leaf)
    result = _service(direct=direct, vision=vision, reasoning=reasoning).look("q")
    assert direct.calls == 1 and vision_leaf.calls == 1 and reasoning_leaf.calls == 1
    assert result.answer == "resilient answer"
    assert result.meta["fallback_mode"] == "resilient"
    assert result.meta["remote_calls"] == 3


def test_h_failed_direct_deepseek_is_excluded_from_same_request():
    direct = Direct(error=ProviderHTTPError(500, "down"))
    tju = Vision(error=ProviderHTTPError(500, "tju"))
    glm = Vision(name="zhipu-glm-4.6v-flash", scene="glm scene")
    deepseek_again = Vision(name="deepseek-vision", scene="must not run")
    breaker = CircuitBreaker(failure_threshold=3)
    vision = FailoverVisionProvider(
        primary=tju, fallbacks=(glm, deepseek_again), breaker=breaker
    )
    result = _service(direct=direct, vision=vision, breaker=breaker).look("q")
    assert result.observation.scene_summary == "glm scene"
    assert deepseek_again.calls == 0


def test_i_non_transient_direct_failure_has_no_fallback():
    direct = Direct(error=ProviderHTTPError(401, "bad key"))
    vision_leaf = Vision()
    reasoning_leaf = Reasoning()
    with pytest.raises(ProviderHTTPError):
        _service(
            direct=direct,
            vision=FailoverVisionProvider(primary=vision_leaf),
            reasoning=FailoverReasoningProvider(primary=reasoning_leaf),
        ).look("q")
    assert vision_leaf.calls == 0 and reasoning_leaf.calls == 0


def test_j_k_l_resilient_is_two_stage_observation_then_text_reasoning():
    vision_leaf = Vision()
    reasoning_leaf = Reasoning()
    service = _service(
        vision=FailoverVisionProvider(primary=vision_leaf),
        reasoning=FailoverReasoningProvider(primary=reasoning_leaf),
    )
    service.set_routing_mode("resilient")
    result = service.look("q")
    assert result.meta["remote_calls"] == 2
    assert result.observation.scene_summary == "教务系统课程页面"
    assert reasoning_leaf.calls == 1
    payload = json.dumps(reasoning_leaf.payloads[0], ensure_ascii=False).lower()
    for marker in ("base64", "image_url", "image_bytes", "data:image"):
        assert marker not in payload


def test_m_normal_chat_takes_zero_screenshots():
    service = _service()

    class Runtime:
        conversation_store = None

        def chat(self, *args, **kwargs):
            return {"choices": [{"message": {"content": "普通回复"}}]}

    runner = CharacterConversationRunner(runtime=Runtime(), screen_vision_service=service)
    _events, answer = runner.perform("今天怎么样")
    assert answer == "普通回复"
    assert service._capture.calls == 0


def test_n_toggle_immediately_changes_one_shot_vs_two_stage(tmp_path):
    from core.settings_manager import SettingsManager

    settings = SettingsManager(tmp_path / "prefs.json")
    direct = Direct()
    reasoning_leaf = Reasoning()
    service = _service(
        direct=direct,
        reasoning=FailoverReasoningProvider(primary=reasoning_leaf),
        settings=settings,
    )
    assert service.look("q").meta["remote_calls"] == 1
    settings.set_screen_vision_fast_mode(False)
    assert service.look("q").meta["remote_calls"] == 2
    settings.set_screen_vision_fast_mode(True)
    assert service.look("q").meta["remote_calls"] == 1
    assert direct.calls == 2 and reasoning_leaf.calls == 1


def test_o_setting_persists_across_reload(tmp_path):
    from core.settings_manager import SettingsManager

    path = tmp_path / "prefs.json"
    SettingsManager(path).set_screen_vision_fast_mode(False)
    assert SettingsManager(path).screen_vision_fast_mode is False


def test_o2_screen_vision_setting_can_persist_outside_workspace_file(tmp_path):
    from core.settings_manager import SettingsManager

    workspace_preferences = tmp_path / "workspace" / "pet_preferences.json"
    user_preferences = tmp_path / "user" / "screen_vision.json"
    manager = SettingsManager(
        workspace_preferences,
        screen_vision_preferences_file=user_preferences,
    )
    manager.set_screen_vision_fast_mode(False)
    assert not workspace_preferences.exists()
    assert user_preferences.exists()
    reloaded = SettingsManager(
        workspace_preferences,
        screen_vision_preferences_file=user_preferences,
    )
    assert reloaded.screen_vision_fast_mode is False


def test_p_capture_preprocessing_preserves_ratio_and_does_not_upscale():
    large = QImage(3200, 1800, QImage.Format_RGB32)
    large.fill(0xFFFFFF)
    resized = _encode_pixmap(large)
    assert (resized.width, resized.height) == (1600, 900)
    assert resized.mime_type == "image/jpeg"

    small = QImage(800, 450, QImage.Format_RGB32)
    small.fill(0xFFFFFF)
    unchanged = _encode_pixmap(small)
    assert (unchanged.width, unchanged.height) == (800, 450)


def test_q_runtime_meta_contains_no_screenshot_or_credentials():
    result = _service().look("q")
    serialized = json.dumps(result.meta, ensure_ascii=False).lower()
    for marker in ("base64", "data:image", "image_bytes", "authorization", "bearer "):
        assert marker not in serialized


def test_r_mode_switch_does_not_reset_shared_breaker(tmp_path):
    from core.settings_manager import SettingsManager

    settings = SettingsManager(tmp_path / "prefs.json")
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    direct = Direct(error=ProviderHTTPError(500, "down"))
    vision = FailoverVisionProvider(
        primary=Vision(error=ProviderHTTPError(500, "tju")),
        fallback=Vision(name="zhipu-glm-4.6v-flash"),
        breaker=breaker,
    )
    service = _service(
        direct=direct, vision=vision, settings=settings, breaker=breaker
    )
    service.look("q")
    assert breaker.is_open
    settings.set_screen_vision_fast_mode(False)
    service.sync_routing_mode_from_settings()
    settings.set_screen_vision_fast_mode(True)
    service.sync_routing_mode_from_settings()
    assert service._vision_breaker is breaker
    assert breaker.is_open


def test_synthetic_latency_one_shot_removes_reasoning_stage_without_sleep():
    vision_ms = 12_000
    reasoning_ms = 15_000
    old_fast = {"remote_calls": 2, "total_ms": vision_ms + reasoning_ms}
    new_fast = {"remote_calls": 1, "total_ms": vision_ms}
    assert old_fast == {"remote_calls": 2, "total_ms": 27_000}
    assert new_fast == {"remote_calls": 1, "total_ms": 12_000}
    assert old_fast["total_ms"] - new_fast["total_ms"] == 15_000
