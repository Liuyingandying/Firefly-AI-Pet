# -*- coding: utf-8 -*-
"""M3.2 Multi-turn Tool Calling Loop 验收测试。

覆盖：单轮回答 / 一次 tool call / 两轮 tool call / 连续失败 /
超过最大轮数 / 非法 tool 拒绝 / JSON 序列化, 另加调用数预算与硬顶收口。
隔离：Mock 模型脚本化; 循环内只用快速工具（query_concept/generate_learning_plan）,
不跑仿真; 不接 TJULLM、不修改 ToolRegistry/既有模块。
"""

from __future__ import annotations

import json

import pytest

from core.learning.agent.loop import (
    HARD_MAX_ROUNDS,
    LOOP_COMPLETED,
    LOOP_MAX_ROUNDS,
    LOOP_MAX_TOOL_CALLS,
    AgentLoopState,
    MultiTurnAgentLoop,
    ToolExecutionRecord,
)
from core.learning.agent.model import MockLearningModel, ModelResponse, ToolCall
from core.learning.tools.schema import (
    TOOL_GENERATE_LEARNING_PLAN,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
)


def _tool_call_response(name: str, arguments: dict) -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(ToolCall(name=name, arguments=arguments),),
        finish_reason="tool_calls",
    )


def _stop_response(text: str) -> ModelResponse:
    return ModelResponse(content=text, tool_calls=(), finish_reason="stop")


@pytest.fixture()
def loop():
    return MultiTurnAgentLoop(MockLearningModel())


# ---------------------------------------------------------------------------
# 1. 单轮回答（无工具调用）
# ---------------------------------------------------------------------------

def test_single_round_answer(loop):
    model = MockLearningModel(scripted=[_stop_response("TE 极化是电场垂直入射面的极化方式。")])
    result = MultiTurnAgentLoop(model).run("什么是TE极化")
    assert result.success is True
    assert result.rounds_used == 1
    assert result.tool_calls_used == 0
    assert result.state.tool_history == ()
    assert result.response == "TE 极化是电场垂直入射面的极化方式。"
    assert result.state.status == LOOP_COMPLETED


# ---------------------------------------------------------------------------
# 2. 一次 tool call
# ---------------------------------------------------------------------------

def test_one_tool_call_round():
    model = MockLearningModel(scripted=[
        _tool_call_response(TOOL_QUERY_CONCEPT, {"concept_id": "em-te-polarization"}),
        _stop_response("TE 极化需要先理解均匀平面波。"),
    ])
    result = MultiTurnAgentLoop(model).run("帮我理解TE极化")
    assert result.success is True
    assert result.rounds_used == 2
    assert result.tool_calls_used == 1
    assert len(result.state.tool_history) == 1
    record = result.state.tool_history[0]
    assert record.tool_name == TOOL_QUERY_CONCEPT
    assert record.arguments == {"concept_id": "em-te-polarization"}
    assert record.result["success"] is True
    assert record.timestamp                                           # UTC-ISO 已打点
    # 工具结果已回填消息流（模型下一轮可见）
    tool_messages = [m for m in result.state.messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "concept" in tool_messages[0]["content"]
    assert result.response == "TE 极化需要先理解均匀平面波。"


# ---------------------------------------------------------------------------
# 3. 两轮 tool call
# ---------------------------------------------------------------------------

def test_two_round_tool_calls():
    model = MockLearningModel(scripted=[
        _tool_call_response(TOOL_QUERY_CONCEPT, {"concept_id": "em-te-polarization"}),
        _tool_call_response(TOOL_GENERATE_LEARNING_PLAN,
                            {"concept_id": "em-te-polarization"}),
        _stop_response("先学前置，再按计划推进。"),
    ])
    result = MultiTurnAgentLoop(model).run("给我TE极化的学习计划")
    assert result.success is True
    assert result.rounds_used == 3
    assert result.tool_calls_used == 2
    assert [r.tool_name for r in result.state.tool_history] == [
        TOOL_QUERY_CONCEPT, TOOL_GENERATE_LEARNING_PLAN,
    ]
    assert result.state.status == LOOP_COMPLETED
    assert len(result.state.messages) == 2 + 2 * 2 + 1   # 初始2 + 每轮(assistant+tool) + 最终assistant


# ---------------------------------------------------------------------------
# 4. 连续失败（台账记录, 循环不崩）
# ---------------------------------------------------------------------------

def test_consecutive_failures_recorded():
    fail_call = _tool_call_response(TOOL_RUN_EXPERIMENT,
                                    {"experiment_id": "ghost-experiment"})
    model = MockLearningModel(scripted=[
        fail_call,
        fail_call,
        _stop_response("两次调用都失败了，请检查实验 ID。"),
    ])
    result = MultiTurnAgentLoop(model).run("运行 ghost 实验")
    assert result.success is True
    failed = [r for r in result.state.tool_history if not r.result.get("success")]
    assert len(failed) == 2
    assert all("未注册" in r.result["error"] for r in failed)
    assert any("2 tool call(s) failed" in w for w in result.warnings)
    assert result.response == "两次调用都失败了，请检查实验 ID。"


# ---------------------------------------------------------------------------
# 5. 超过最大轮数（预算硬顶 5 轮）
# ---------------------------------------------------------------------------

def test_max_rounds_reached():
    endless = _tool_call_response(TOOL_QUERY_CONCEPT, {"concept_id": "em-te-polarization"})
    model = MockLearningModel(scripted=[endless])       # 末尾重放 → 永远提议工具
    result = MultiTurnAgentLoop(model).run("无限查询")
    assert result.success is True                        # 安全停止, 非异常
    assert result.rounds_used == HARD_MAX_ROUNDS == 5
    assert result.state.status == LOOP_MAX_ROUNDS
    assert any("max rounds" in w for w in result.warnings)
    assert result.response                               # 回退文案


def test_round_hard_cap_cannot_be_relaxed():
    endless = _tool_call_response(TOOL_QUERY_CONCEPT, {"concept_id": "em-te-polarization"})
    model = MockLearningModel(scripted=[endless])
    loop = MultiTurnAgentLoop(model, max_rounds=100)     # 试图放宽 → 被硬顶压制
    result = loop.run("无限查询")
    assert result.rounds_used == HARD_MAX_ROUNDS


# ---------------------------------------------------------------------------
# 6. 非法 tool 拒绝（白名单不变）
# ---------------------------------------------------------------------------

def test_illegal_tool_rejected():
    model = MockLearningModel(scripted=[
        _tool_call_response("run_python", {"code": "import os"}),
        _stop_response("已跳过非法工具。"),
    ])
    result = MultiTurnAgentLoop(model).run("帮我执行代码")
    assert result.success is True
    denied = result.state.tool_history[0]
    assert denied.tool_name == "run_python"
    assert "tool_denied" in denied.result["error"]
    assert result.tool_calls_used == 1                   # 仅 1 次提议（第二条是 stop）
    assert any("tool_denied" in w for w in result.warnings)
    assert result.response == "已跳过非法工具。"


# ---------------------------------------------------------------------------
# 6b. 调用数预算（10 次硬顶）
# ---------------------------------------------------------------------------

def test_max_tool_calls_budget():
    three_calls = ModelResponse(
        content="",
        tool_calls=tuple(
            ToolCall(name=TOOL_QUERY_CONCEPT,
                     arguments={"concept_id": f"em-te-polarization-{i}"})
            for i in range(3)
        ),
        finish_reason="tool_calls",
    )
    model = MockLearningModel(scripted=[three_calls])    # 每轮 3 提议, 末尾重放
    result = MultiTurnAgentLoop(model).run("批量查询")
    assert result.tool_calls_used == 10                  # 硬顶 10
    assert result.state.status == LOOP_MAX_TOOL_CALLS
    assert len(result.state.tool_history) == 10
    assert any("max tool calls" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 7. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(loop):
    model = MockLearningModel(scripted=[
        _tool_call_response(TOOL_QUERY_CONCEPT, {"concept_id": "em-te-polarization"}),
        _stop_response("完成。"),
    ])
    result = MultiTurnAgentLoop(model).run("TE极化")
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert parsed["rounds_used"] == 2
    assert parsed["state"]["status"] == "completed"
    assert parsed["state"]["tool_history"][0]["tool_name"] == TOOL_QUERY_CONCEPT

    state_json = json.loads(result.state.to_json())
    assert state_json["step_count"] == 2


def test_empty_query_structured_failure(loop):
    result = loop.run("   ")
    assert result.success is False
    assert result.error.startswith("empty_query")


def test_state_record_frozen():
    record = ToolExecutionRecord(tool_name="t", timestamp="2026-09-23T00:00:00+00:00")
    with pytest.raises(Exception):
        record.tool_name = "x"                           # frozen dataclass
    state = AgentLoopState(status="running")
    with pytest.raises(Exception):
        state.status = "completed"
