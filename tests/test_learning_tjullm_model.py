# -*- coding: utf-8 -*-
"""M3.3 TJULLM Model Adapter 验收测试（Mock HTTP Server, 不调用真实 TJULLM）。

覆盖：普通文本回复 / tool_call 解析 / 多个 tool_call / 错误响应 / 超时 /
非法 JSON / 密钥安全 / 循环集成（MultiTurnAgentLoop + 真实注册表）。
隔离：本地 ThreadingHTTPServer 场景队列; 不触 ProviderRouter/ToolRegistry/
Agent Loop 核心; 密钥零泄漏断言。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.learning.agent.loop import MultiTurnAgentLoop
from core.learning.agent.model import ModelRequest, ModelResponse, ToolCall
from core.learning.agent.model.tjullm import (
    ENV_API_KEY,
    ENV_BASE_URL,
    TJULLMModelError,
    TJULLMModel,
)
from core.learning.tools.schema import TOOL_QUERY_CONCEPT

TEXT_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": "OK, how can I help?"},
                 "finish_reason": "stop"}],
    "model": "qwen3.6-35b-a3b",
})
ONE_TOOL_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": None,
                             "tool_calls": [{"id": "call_1", "type": "function",
                                             "function": {"name": TOOL_QUERY_CONCEPT,
                                                          "arguments": '{"concept_id": "em-te-polarization"}'}}]},
                 "finish_reason": "tool_calls"}],
})
TWO_TOOLS_BODY = json.dumps({
    "choices": [{"message": {"role": "assistant", "content": None,
                             "tool_calls": [
                                 {"id": "call_1", "type": "function",
                                  "function": {"name": TOOL_QUERY_CONCEPT,
                                               "arguments": '{"concept_id": "em-te-polarization"}'}},
                                 {"id": "call_2", "type": "function",
                                  "function": {"name": "generate_learning_plan",
                                               "arguments": '{"concept_id": "em-te-polarization"}'}},
                             ]},
                 "finish_reason": "tool_calls"}],
})


# ---------------------------------------------------------------------------
# Mock HTTP Server（场景队列 FIFO, 记录请求供断言）
# ---------------------------------------------------------------------------

class _MockTJULLMServer:
    """本地 ThreadingHTTPServer：按场景队列回放 (status, body, delay)。"""

    def __init__(self):
        self.scenarios: list[tuple[int, str, float]] = []
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    def enqueue(self, status: int = 200, body: str = "{}", delay: float = 0.0) -> None:
        self.scenarios.append((status, body, delay))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v3"

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:      # 静默
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8", "replace")
                with outer._lock:
                    outer.requests.append({
                        "path": self.path,
                        "auth": self.headers.get("Authorization", ""),
                        "payload": json.loads(body) if body.startswith("{") else {},
                    })
                    if outer.scenarios:
                        status, resp_body, delay = outer.scenarios.pop(0)
                    else:                                  # 场景耗尽 → 显式错误
                        status, resp_body, delay = 500, '{"error":{"message":"no scenario"}}', 0.0
                if delay:
                    import time
                    time.sleep(delay)
                data = resp_body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture()
def server():
    server = _MockTJULLMServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def deterministic_model_profile(monkeypatch):
    """Pin the shared ModelRouter selection to ``auto`` for every test.

    ``TJULLMModel.generate`` routes through the process-wide
    ``get_model_router()`` singleton, whose default selection reads the
    machine-persistent ``SettingsManager().ai_model_profile`` (a developer
    may have selected ``tju-max`` in the AI 模型设置 card). Without this
    pin the tests are environment-coupled: the payload model becomes
    ``tju-llm-max`` instead of the shipped ``task_routes['learning']`` →
    ``tju-stable`` → ``tju-llm``, and error tests exhaust the single
    enqueued scenario on the first attempt of the tju-max→tju-stable
    fallback chain, surfacing ``no scenario`` instead of the intended
    error. ``auto`` keeps the shipped profiles file as the contract under
    test — no assertion is weakened.
    """
    from core.model_router import get_model_router
    monkeypatch.setattr(get_model_router(), "selection", lambda: "auto")


def _model(server, timeout_s: float = 5.0) -> TJULLMModel:
    return TJULLMModel(
        api_key="unit-test-key", base_url=server.url, timeout_s=timeout_s
    )


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "演示均匀平面波"},
        ),
    )


# ---------------------------------------------------------------------------
# 1. 普通文本回复
# ---------------------------------------------------------------------------

def test_text_reply(server):
    server.enqueue(200, TEXT_BODY)
    response = _model(server).generate(_request())
    assert response.content == "OK, how can I help?"
    assert response.finish_reason == "stop"
    assert response.tool_calls == ()
    req = server.requests[0]
    assert req["path"] == "/v3/chat/completions"           # URL 面封闭
    assert req["auth"] == "Bearer unit-test-key"           # Bearer 鉴权
    assert req["payload"]["model"] == "tju-llm"
    assert req["payload"]["messages"][0]["role"] == "system"   # messages 透传
    assert "tools" not in req["payload"]                   # 空工具不发送


def test_tools_payload_passthrough(server):
    from core.learning.tools.schema import all_tool_schemas
    server.enqueue(200, TEXT_BODY)
    request = ModelRequest(messages=({"role": "user", "content": "x"},),
                           tools=tuple(all_tool_schemas()))
    _model(server).generate(request)
    assert len(server.requests[0]["payload"]["tools"]) == 4


# ---------------------------------------------------------------------------
# 2/3. tool_call 解析（单个 / 多个）
# ---------------------------------------------------------------------------

def test_single_tool_call_parsed(server):
    server.enqueue(200, ONE_TOOL_BODY)
    response = _model(server).generate(_request())
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.name == TOOL_QUERY_CONCEPT
    assert call.arguments == {"concept_id": "em-te-polarization"}   # JSON 已解析


def test_multiple_tool_calls_parsed(server):
    server.enqueue(200, TWO_TOOLS_BODY)
    response = _model(server).generate(_request())
    assert [c.name for c in response.tool_calls] == [TOOL_QUERY_CONCEPT, "generate_learning_plan"]
    assert response.tool_calls[1].arguments == {"concept_id": "em-te-polarization"}


# ---------------------------------------------------------------------------
# 4. 错误响应
# ---------------------------------------------------------------------------

def test_http_error_structured(server):
    server.enqueue(500, '{"error":{"message":"boom"}}')
    with pytest.raises(TJULLMModelError) as exc_info:
        _model(server).generate(_request())
    message = str(exc_info.value)
    assert "500" in message and "boom" in message
    assert "unit-test-key" not in message                  # 密钥零泄漏
    assert "unit-test-key" not in repr(_model(server))


# ---------------------------------------------------------------------------
# 5. 超时
# ---------------------------------------------------------------------------

def test_timeout_structured(server):
    server.enqueue(200, TEXT_BODY, delay=1.5)
    with pytest.raises(TJULLMModelError, match="timeout"):
        _model(server, timeout_s=0.3).generate(_request())


# ---------------------------------------------------------------------------
# 6. 非法 JSON
# ---------------------------------------------------------------------------

def test_invalid_json_structured(server):
    server.enqueue(200, "not-json{{")
    with pytest.raises(TJULLMModelError, match="invalid json"):
        _model(server).generate(_request())


# ---------------------------------------------------------------------------
# 7. 密钥来源（环境变量 / .env 收口）与安全
# ---------------------------------------------------------------------------

def test_api_key_from_env(monkeypatch, server):
    monkeypatch.setenv(ENV_API_KEY, "env-key-xyz")
    server.enqueue(200, TEXT_BODY)
    TJULLMModel(base_url=server.url).generate(_request())
    assert server.requests[0]["auth"] == "Bearer env-key-xyz"


def test_base_url_from_env(monkeypatch, server):
    monkeypatch.setenv(ENV_API_KEY, "env-key")
    monkeypatch.setenv(ENV_BASE_URL, server.url)
    server.enqueue(200, TEXT_BODY)
    TJULLMModel().generate(_request())                     # 无构造参数 → env 生效
    assert server.requests[0]["path"] == "/v3/chat/completions"


# ---------------------------------------------------------------------------
# 8. 循环集成（MultiTurnAgentLoop + 真实注册表, 两轮：工具 → 最终回答）
# ---------------------------------------------------------------------------

def test_loop_integration_tool_then_final(server):
    server.enqueue(200, ONE_TOOL_BODY)
    server.enqueue(200, TEXT_BODY.replace("OK, how can I help?", "TE 极化需先学均匀平面波。"))
    model = TJULLMModel(api_key="unit-test-key", base_url=server.url)
    result = MultiTurnAgentLoop(model).run("帮我理解TE极化")

    assert result.success is True
    assert result.rounds_used == 2
    assert result.tool_calls_used == 1
    assert result.state.tool_history[0].tool_name == TOOL_QUERY_CONCEPT
    assert result.state.tool_history[0].result["success"] is True   # 真实注册表执行
    assert result.response == "TE 极化需先学均匀平面波。"
    # 第二轮请求包含第一轮的工具结果（role:tool 回填）
    second = server.requests[1]["payload"]["messages"]
    assert any(m.get("role") == "tool" for m in second)


def test_loop_error_is_structured(server):
    server.enqueue(500, '{"error":{"message":"boom"}}')
    model = TJULLMModel(api_key="unit-test-key", base_url=server.url)
    result = MultiTurnAgentLoop(model).run("anything")
    assert result.success is False
    assert result.error.startswith("execution_failed:")
    assert "TJULLMModelError" in result.error              # 类型化异常被循环收敛


def test_static_no_providerrouter_reference():
    """静态契约: 适配器不引用 ProviderRouter / 不注册工具。"""
    import core.learning.agent.model.tjullm as tjullm_mod
    import inspect

    source = inspect.getsource(tjullm_mod)
    assert "ProviderRouter" not in source
    assert "register" not in source
    assert "eval(" not in source and "exec(" not in source
