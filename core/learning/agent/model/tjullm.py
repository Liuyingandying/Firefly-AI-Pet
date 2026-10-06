# -*- coding: utf-8 -*-
"""TJULLMModel — 真实 TJULLM（OpenAI Chat Completions）模型后端（M3.3）。

形状转换::

    ModelRequest(messages/tools/context)
      → OpenAI payload：messages 原样透传 + tools（非空时）
      → POST {base_url}/chat/completions（Authorization: Bearer）
      → choices[0].message → ModelResponse(content/tool_calls/finish_reason)

安全:
- API Key 只从构造参数或环境变量 ``TJULLM_API_KEY`` 读取; repr 遮蔽;
  错误消息零密钥;
- URL 面封闭: 只 POST ``{base_url}/chat/completions`` 一个地址
  （base_url 来自构造/``TJULLM_BASE_URL``, 无逐调用 URL 注入）;
- 超时控制 + 类型化异常 ``TJULLMModelError``（网络/HTTP/JSON 解析失败）——
  由 Agent 循环的既有 catch-all 收敛为结构化结果;
- 适配器**只提议调用**, 永不执行工具、不注册工具。

失败语义: 网络失败抛 ``TJULLMModelError``（Agent 边界结构化; 与 mock 的
"永不抛异常"承诺区分——那是模型提议语义, 本类异常是传输故障）。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Callable

from core.learning.agent.model.schema import ModelRequest, ModelResponse, ToolCall
from providers.base import resolve_setting  # 只读复用: env > store > .env 单一收口

#: 环境变量名（与 core/providers/catalog.py 的 credential_env 对齐）
ENV_API_KEY = "TJULLM_API_KEY"
ENV_BASE_URL = "TJULLM_BASE_URL"

DEFAULT_BASE_URL = "https://ai.tju.edu.cn/api/v3"
DEFAULT_MODEL = "tju-llm"
DEFAULT_TIMEOUT_S = 30.0

_COMPLETIONS_SUFFIX = "/chat/completions"     # 唯一允许的路径后缀（URL 面封闭）

#: 传输函数: (url, payload, headers, timeout_s) -> (status, body_text)
Transport = Callable[[str, dict, dict, float], "tuple[int, str]"]


class TJULLMModelError(RuntimeError):
    """TJULLM 传输/协议失败（网络/超时/HTTP 错误/JSON 解析）。

    Agent 循环的既有 catch-all 会将其收敛为结构化结果——本类只承载
    类型化详情, 消息中永不包含密钥。
    """


class TJULLMModel:
    """真实 TJULLM 模型后端（OpenAI Chat Completions + tools）。"""

    name = "tjullm"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Transport | None = None,
    ):
        # 显式参数 > 环境变量 > .env（resolve_setting 单一收口） > 默认值
        # （URL 面封闭: 仅这一个地址）
        self._api_key = str(
            api_key or resolve_setting(ENV_API_KEY) or ""
        ).strip()
        configured = base_url or resolve_setting(ENV_BASE_URL) or DEFAULT_BASE_URL
        self._base_url = str(configured).rstrip("/")
        self._model = str(model or DEFAULT_MODEL)
        self._timeout_s = max(float(timeout_s), 0.1)
        self._transport = transport if transport is not None else _default_transport

    # -- 协议入口 -------------------------------------------------------------

    def generate(self, request: ModelRequest) -> ModelResponse:
        """ModelRequest → TJULLM completions → ModelResponse。

        网络/HTTP/解析失败抛 TJULLMModelError（由 Agent 循环收敛为
        结构化结果）; 成功时 tool_calls 的 arguments 已解析为 dict。
        """
        if not self._api_key:
            raise TJULLMModelError(
                f"api key not configured（设置环境变量 {ENV_API_KEY} 或构造参数 api_key）"
            )

        url = self._base_url + _COMPLETIONS_SUFFIX
        payload = {
            "model": self._model,
            "messages": [dict(m) for m in request.messages],
        }
        if request.tools:
            payload["tools"] = [dict(t) for t in request.tools]   # 空工具不发送
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Only the provider boundary changes; ModelRequest/Response and all
        # learning transitions, tools and session owners remain untouched.
        from core.model_router import get_model_router
        def send(body, budget):
            status, body_text = self._transport(url, body, headers, budget)
            if status != 200:
                raise TJULLMModelError(f"http error {status}")
            return self._parse_completion(body_text)
        return get_model_router().request(
            payload, send, task="learning", timeout=self._timeout_s,
            model=self._model if self._model != DEFAULT_MODEL else None,
            capabilities=("text", "tools") if request.tools else ("text",),
            validator=lambda _result: None,  # existing parser above is authoritative
        )

    # -- 响应解析 -------------------------------------------------------------

    @staticmethod
    def _parse_completion(body_text: str) -> ModelResponse:
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise TJULLMModelError(f"invalid json response: {exc}") from None
        if not isinstance(data, dict):
            raise TJULLMModelError("response body must be a JSON object")

        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise TJULLMModelError("response has no choices")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        content = str(message.get("content") or "")
        finish_reason = str(first.get("finish_reason") or "stop")

        tool_calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") if isinstance(raw, dict) else None
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                raise TJULLMModelError("tool_call missing function.name")
            raw_arguments = function.get("arguments", "{}")
            try:
                arguments = json.loads(raw_arguments if isinstance(raw_arguments, str)
                                       else json.dumps(raw_arguments))
            except (json.JSONDecodeError, TypeError) as exc:
                raise TJULLMModelError(
                    f"invalid tool arguments json for {name!r}: {exc}"
                ) from None
            if not isinstance(arguments, dict):
                raise TJULLMModelError(f"tool arguments for {name!r} must decode to an object")
            tool_calls.append(ToolCall(name=name, arguments=arguments))

        return ModelResponse(
            content=content,
            tool_calls=tuple(tool_calls),
            finish_reason="tool_calls" if tool_calls else finish_reason,
        )

    # -- 密钥安全 -------------------------------------------------------------

    def __repr__(self) -> str:  # 密钥遮蔽（防日志泄漏）
        return (f"<TJULLMModel base_url={self._base_url!r} "
                f"model={self._model!r} api_key=***>")


# ---------------------------------------------------------------------------
# 默认传输（stdlib urllib; 测试可注入 transport 替换）
# ---------------------------------------------------------------------------

def _default_transport(url: str, payload: dict, headers: dict, timeout_s: float):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        raise TJULLMModelError(f"http error {exc.code}: {body[:200]}") from None
    except (TimeoutError, socket.timeout):
        raise TJULLMModelError(f"timeout after {timeout_s}s") from None
    except (urllib.error.URLError, OSError) as exc:
        raise TJULLMModelError(f"connection failed: {exc}") from None
    except json.JSONDecodeError as exc:
        raise TJULLMModelError(f"invalid json response: {exc}") from None
