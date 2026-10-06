# -*- coding: utf-8 -*-
"""M3.1 Agent Model Adapter 验收测试。

覆盖：Mock tool call / Runtime 执行模型返回工具 / 非法工具拒绝 /
空回复安全 / JSON 序列化, 另加默认路径不受影响（无模型时保持规则规划）
与静态安全契约。
隔离：Mock 模型零网络; run_experiment 用真实本地 runner（~2s）;
不接 TJULLM、不修改 ProviderRouter/ToolRegistry/Runtime 核心。
"""

from __future__ import annotations

import json

import pytest

from core.learning.agent import (
    AgentRunResult,
    AgentTask,
    LearningAgentRuntime,
    TASK_COMPLETED,
)
from core.learning.agent.model import (
    MockLearningModel,
    ModelRequest,
    ModelResponse,
    ToolCall,
)
from core.learning.tools.schema import TOOL_RUN_EXPERIMENT

DEMO_QUERY = "演示均匀平面波"


@pytest.fixture(scope="module")
def model():
    return MockLearningModel()


@pytest.fixture(scope="module")
def model_runtime(model):
    return LearningAgentRuntime(model=model)


# ---------------------------------------------------------------------------
# 1. Mock tool call（模型后端单元）
# ---------------------------------------------------------------------------

def test_mock_generates_tool_call(model):
    request = ModelRequest.from_task(AgentTask(user_query=DEMO_QUERY))
    response = model.generate(request)
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.name == TOOL_RUN_EXPERIMENT
    assert call.arguments == {"experiment_id": "uniform-plane-wave"}


def test_mock_keyword_rules_and_fallback(model):
    req = ModelRequest.from_task(AgentTask(user_query="TE极化怎么理解"))
    resp = model.generate(req)
    assert resp.tool_calls[0].name == "query_concept"
    assert resp.tool_calls[0].arguments["concept_id"] == "em-te-polarization"

    fallback = model.generate(ModelRequest(messages=({"role": "user", "content": "zzz"},)))
    assert fallback.finish_reason == "stop"
    assert fallback.tool_calls == ()
    assert fallback.content                                   # 安全文案


def test_mock_scripted_responses():
    scripted_model = MockLearningModel(scripted=[ModelResponse(content="", finish_reason="stop")])
    resp = scripted_model.generate(ModelRequest(messages=({"role": "user", "content": "anything"},)))
    assert resp.finish_reason == "stop" and resp.content == ""   # 脚本回放（末尾重复）
    assert scripted_model.calls == 1


# ---------------------------------------------------------------------------
# 2. Runtime 执行模型返回工具
# ---------------------------------------------------------------------------

def test_runtime_executes_model_tool_call(model_runtime, model):
    result = model_runtime.run(AgentTask(user_query=DEMO_QUERY))
    assert result.success is True
    assert result.task.status == TASK_COMPLETED
    assert result.error == ""
    assert TOOL_RUN_EXPERIMENT in result.tools_used
    act_step = next(s for s in result.steps if s.tool == TOOL_RUN_EXPERIMENT)
    assert act_step.result["success"] is True
    assert act_step.result["result"]["status"] == "completed"
    # 回复组装：模型工具结果进入解释层
    assert "仿真实验结果" in result.response
    assert "electric_field.png" in result.response
    assert model.calls >= 1


def test_model_plan_step_recorded(model_runtime):
    result = model_runtime.run(AgentTask(user_query=DEMO_QUERY))
    plan_steps = [s for s in result.steps if s.thought_type == "plan"]
    assert plan_steps and "模型规划" in plan_steps[0].action


# ---------------------------------------------------------------------------
# 3. 非法工具拒绝
# ---------------------------------------------------------------------------

def test_non_allowlist_tool_rejected():
    rogue = MockLearningModel(scripted=[ModelResponse(
        content="",
        tool_calls=(ToolCall(name="run_python", arguments={"code": "import os"}),),
        finish_reason="tool_calls",
    )])
    runtime = LearningAgentRuntime(model=rogue)
    result = runtime.run(AgentTask(user_query="anything"))
    assert result.success is True                     # Agent 正常收尾
    assert result.tools_used == ()                    # 但未执行任何工具
    assert any("tool_denied" in w for w in result.warnings)
    denied_steps = [s for s in result.steps if s.result.get("denied")]
    assert denied_steps and denied_steps[0].tool == "run_python"


def test_default_runtime_ignores_model():
    """默认 Runtime（model=None）保持 M3.0 规则规划——模型通道不启用。"""
    runtime = LearningAgentRuntime()                  # model=None（默认）
    result = runtime.run(AgentTask(user_query="TE极化是什么概念"))
    assert result.success is True
    assert not any("模型规划" in s.action for s in result.steps)
    assert any("规则规划" in s.action for s in result.steps)
    # 显式 model=None 与省略等价
    explicit = LearningAgentRuntime(model=None).run(AgentTask(user_query="TE极化是什么概念"))
    assert explicit.success is True
    assert any("规则规划" in s.action for s in explicit.steps)


def test_model_without_generate_rejected():
    runtime = LearningAgentRuntime(model=object())
    result = runtime.run(AgentTask(user_query="演示均匀平面波"))
    assert result.success is False
    assert result.error.startswith("invalid_input")


def test_model_tool_failure_structured():
    failing = MockLearningModel(scripted=[ModelResponse(
        content="",
        tool_calls=(ToolCall(name="run_experiment",
                             arguments={"experiment_id": "ghost-experiment"}),),
        finish_reason="tool_calls",
    )])
    result = LearningAgentRuntime(model=failing).run(AgentTask(user_query=DEMO_QUERY))
    assert result.success is False
    assert result.error.startswith("tool_failed:")
    assert "未注册" in result.error


# ---------------------------------------------------------------------------
# 4. 空回复安全
# ---------------------------------------------------------------------------

def test_empty_response_is_safe():
    empty = MockLearningModel(scripted=[ModelResponse(content="", finish_reason="stop")])
    result = LearningAgentRuntime(model=empty).run(AgentTask(user_query="zzz"))
    assert result.success is True                     # Agent 正常收尾, 不崩溃
    assert "模型未返回有效内容" in result.response      # 回退文案
    assert any("empty response" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 5. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(model_runtime, model):
    result = model_runtime.run(AgentTask(user_query=DEMO_QUERY))
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert "run_experiment" in parsed["tools_used"]
    assert any(s["thought_type"] == "plan" and "模型规划" in s["action"] for s in parsed["steps"])

    # 模型协议请求自身可序列化
    request = ModelRequest.from_task(AgentTask(user_query="x"))
    request_parsed = json.loads(json.dumps(request.to_dict()))
    assert request_parsed["messages"][0]["role"] == "system"

    resp = ModelResponse(content="ok", tool_calls=(ToolCall(name="t", arguments={"a": 1}),),
                         finish_reason="tool_calls")
    parsed_resp = json.loads(resp.to_json())
    assert parsed_resp["tool_calls"][0]["arguments"] == {"a": 1}


def test_model_response_json_roundtrip():
    resp = ModelResponse(content="hi", finish_reason="stop")
    assert json.loads(resp.to_json())["content"] == "hi"
