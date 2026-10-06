# -*- coding: utf-8 -*-
"""MockLearningModel — 固定规则模型后端（M3.1 原型, 零网络）。

按关键词把用户消息映射为**固定的 tool_calls 提议**（示例：输入
"演示均匀平面波" → run_experiment(experiment_id="uniform-plane-wave")）；
也支持脚本化响应序列（测试空回复/多轮等场景）。

安全语义：模型只**提议**调用——是否执行由 runtime 的白名单（selector +
registry）决定, mock 无法绕过任何一层。
"""

from __future__ import annotations

from core.learning.agent.model.schema import (
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from core.learning.tools.schema import (
    TOOL_GENERATE_LEARNING_PLAN,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
)

#: 关键词 → 工具调用提议（固定映射, 顺序即优先级）
_KEYWORD_RULES: tuple[tuple[tuple[str, ...], ToolCall], ...] = (
    (
        ("演示", "实验", "仿真", "运行"),
        ToolCall(name=TOOL_RUN_EXPERIMENT, arguments={"experiment_id": "uniform-plane-wave"}),
    ),
    (
        ("计划", "规划", "怎么学"),
        ToolCall(
            name=TOOL_GENERATE_LEARNING_PLAN,
            arguments={"concept_id": "em-uniform-plane-wave"},
        ),
    ),
    (
        ("TE", "te"),
        ToolCall(name=TOOL_QUERY_CONCEPT, arguments={"concept_id": "em-te-polarization"}),
    ),
    (
        ("TM", "tm"),
        ToolCall(name=TOOL_QUERY_CONCEPT, arguments={"concept_id": "em-tm-polarization"}),
    ),
)


class MockLearningModel:
    """固定输入 → 固定 tool_calls 的模型后端（原型/测试用）。"""

    name = "mock"

    def __init__(self, *, scripted: list[ModelResponse] | None = None):
        """
        scripted: 按调用次序回放的响应序列（末尾重复最后一个）;
        不提供时使用关键词默认规则。
        """
        self._scripted = list(scripted) if scripted else []
        self.calls = 0                       # 被调用计数（测试断言用）

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self._scripted:
            index = min(self.calls - 1, len(self._scripted) - 1)
            return self._scripted[index]
        return self._keyword_response(request)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _last_user_text(request: ModelRequest) -> str:
        for message in reversed(request.messages):
            if message.get("role") == "user":
                return str(message.get("content", "") or "")
        return ""

    def _keyword_response(self, request: ModelRequest) -> ModelResponse:
        text = self._last_user_text(request)
        for keywords, tool_call in _KEYWORD_RULES:
            if any(kw in text for kw in keywords):
                return ModelResponse(
                    content="",
                    tool_calls=(tool_call,),
                    finish_reason="tool_calls",
                )
        # 零命中：纯文本安全回复（无工具提议）
        return ModelResponse(
            content="请告诉我你想学习的概念或想运行的实验，"
                    "例如：帮我理解均匀平面波中的TE和TM模式。",
            tool_calls=(),
            finish_reason="stop",
        )
