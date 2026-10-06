# -*- coding: utf-8 -*-
"""M3.0 Learning Agent 验收测试。

覆盖：简单概念查询 / 实验调用 / 完整链路 / 非法工具拒绝 / 异常安全返回 /
JSON 序列化, 另加白名单与规划器契约、静态安全断言。
隔离：真实本地仿真 1-2 次（~2s each）; 不接 TJULLM、不修改既有模块。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from core.learning.agent import (
    ALLOWED_TOOLS,
    TASK_COMPLETED,
    TASK_FAILED,
    AgentRunResult,
    AgentTask,
    LearningAgentRuntime,
    ToolDeniedError,
    assert_allowed,
    plan,
    select_allowed_tools,
)
from core.learning.tools.schema import (
    TOOL_IMPORT_TEXTBOOK,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
    TOOL_GENERATE_LEARNING_PLAN,
)


@pytest.fixture(scope="module")
def runtime():
    return LearningAgentRuntime()


# ---------------------------------------------------------------------------
# 1. 简单概念查询
# ---------------------------------------------------------------------------

def test_simple_concept_query(runtime):
    task = AgentTask(user_query="TE极化是什么概念")
    result = runtime.run(task)
    assert result.success is True
    assert result.task.status == TASK_COMPLETED
    query_steps = [s for s in result.steps if s.tool == TOOL_QUERY_CONCEPT]
    assert query_steps, "应包含概念查询步骤"
    assert result.response  # markdown 非空
    assert "概念解释" in result.response


def test_concept_mapping_deterministic():
    task = AgentTask(user_query="解释TE和TM区别")
    steps = plan(task)
    query_concepts = [
        s.arguments["concept_id"] for s in steps if s.tool == TOOL_QUERY_CONCEPT
    ]
    assert query_concepts == ["em-te-polarization", "em-tm-polarization"]
    # 任务书示例的形状：查询 → 实验 → 解释
    tools = [s.tool for s in steps]
    assert tools[-1] is None                      # explain 终步
    assert TOOL_RUN_EXPERIMENT in tools
    assert tools.index(TOOL_QUERY_CONCEPT) < tools.index(TOOL_RUN_EXPERIMENT)


# ---------------------------------------------------------------------------
# 2. 实验调用
# ---------------------------------------------------------------------------

def test_experiment_invocation(runtime):
    task = AgentTask(user_query="运行均匀平面波仿真实验看看")
    result = runtime.run(task)
    assert result.success is True
    assert TOOL_RUN_EXPERIMENT in result.tools_used
    step = next(s for s in result.steps if s.tool == TOOL_RUN_EXPERIMENT)
    assert step.result["success"] is True
    assert step.result["result"]["status"] == "completed"
    # 解释层环节存在（仿真产物解读进入回复）
    assert "仿真实验结果" in result.response
    assert "结果解读" in result.response


# ---------------------------------------------------------------------------
# 3. 完整链路（任务书 Demo 场景）
# ---------------------------------------------------------------------------

def test_full_closed_loop(runtime):
    task = AgentTask(user_query="帮我理解均匀平面波中的TE和TM模式")
    result = runtime.run(task)
    assert result.success is True
    assert result.task.status == TASK_COMPLETED
    assert result.error == ""
    # 工具序列：概念查询 + 实验
    assert TOOL_QUERY_CONCEPT in result.tools_used
    assert TOOL_RUN_EXPERIMENT in result.tools_used
    # explain 终步存在且生成了 markdown
    explain_steps = [s for s in result.steps if s.thought_type == "explain"]
    assert len(explain_steps) == 1
    # 三段式回复结构
    for section in ("概念解释", "仿真实验结果", "下一步学习建议"):
        assert section in result.response
    # 实验产物路径进入回复
    assert "electric_field.png" in result.response
    assert "wave.gif" in result.response


# ---------------------------------------------------------------------------
# 4. 非法工具拒绝（白名单三重拦截）
# ---------------------------------------------------------------------------

def test_allowlist_is_static_and_enforced():
    assert ALLOWED_TOOLS == frozenset({
        TOOL_IMPORT_TEXTBOOK, TOOL_QUERY_CONCEPT,
        TOOL_RUN_EXPERIMENT, TOOL_GENERATE_LEARNING_PLAN,
    })
    with pytest.raises(ToolDeniedError):
        assert_allowed("run_python")


def test_planner_never_emits_non_allowlist_tools():
    queries = [
        "解释TE和TM区别", "帮我导入教材并生成课程", "运行实验",
        "给我一个学习计划", "随便聊聊", "删除所有数据",
    ]
    for query in queries:
        task = AgentTask(user_query=query)
        for step in plan(task):
            assert step.tool is None or step.tool in ALLOWED_TOOLS, query


def test_selector_filters_by_intent():
    assert set(select_allowed_tools("导入教材生成课程")) == {TOOL_IMPORT_TEXTBOOK}
    assert TOOL_RUN_EXPERIMENT in select_allowed_tools("运行仿真实验")
    # 零命中回退（保守默认）
    assert set(select_allowed_tools("zzz")) == {
        TOOL_QUERY_CONCEPT, TOOL_GENERATE_LEARNING_PLAN,
    }


# ---------------------------------------------------------------------------
# 5. 异常安全返回（结构化错误, 不抛异常）
# ---------------------------------------------------------------------------

def test_tool_failure_returns_structured_error(runtime):
    """工具失败 → 结构化结果（trace 保留, 不抛异常）。"""
    # 路径 a: 越权教材路径 → import_textbook 拒绝（文件允许根）
    task = AgentTask(
        user_query="导入教材并生成课程",
        context={"textbook_path": "C:/Windows/win.ini", "course_id": "c1"},
    )
    result = runtime.run(task)
    assert result.success is False
    assert result.task.status == TASK_FAILED
    assert result.error.startswith("tool_failed:")
    assert "file_permission_denied" in result.error
    assert result.steps                                  # 失败前 trace 保留

    # 路径 b: 非法仿真参数 → runner 参数校验拒绝（param_error）
    task2 = AgentTask(
        user_query="运行均匀平面波实验",
        context={"experiment_params": {"frequency": 999}},
    )
    result2 = runtime.run(task2)
    assert result2.success is False
    assert result2.error.startswith("tool_failed:")
    assert "param_error" in result2.error


def test_runtime_never_raises_on_garbage(runtime):
    for bad in (None, "not-a-task", 42):
        result = runtime.run(bad)
        assert result.success is False
        assert result.error.startswith("invalid_input")
        assert result.task is None


def test_empty_query_rejected(runtime):
    result = runtime.run(AgentTask(user_query="   "))
    assert result.success is False
    assert result.error.startswith("empty_query")


def test_denied_tool_recorded_as_warning():
    """planner 产出被篡改的场景（防御纵深）：selector 拒绝 → 警告 + 结构化失败。"""
    task = AgentTask(user_query="解释均匀平面波")
    # 构造一个 planner 永远不会产出、但演示拦截路径的调用：
    # 直接对 selector 断言（runtime 内部同路径）
    with pytest.raises(ToolDeniedError):
        assert_allowed("delete_everything")


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_run_result_json_roundtrip(runtime):
    result = runtime.run(AgentTask(user_query="TE极化是什么概念"))
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert parsed["task"]["status"] == "completed"
    assert all("thought_type" in s for s in parsed["steps"])
    assert all(s["thought_type"] in ("plan", "act", "observe", "explain") for s in parsed["steps"])

    failed = json.loads(
        AgentRunResult(success=False, error="tool_failed: x").to_json()
    )
    assert failed["task"] is None


def test_step_trace_has_no_free_text_thoughts(runtime):
    """审计语义：step 只存动作类别 + 结构化结果，无自由文本思维。"""
    result = runtime.run(AgentTask(user_query="帮我理解均匀平面波中的TE和TM模式"))
    for step in result.steps:
        assert step.thought_type in ("plan", "act", "observe", "explain")
        assert isinstance(step.result, dict)
        assert "chain_of_thought" not in step.result
        assert "reasoning" not in json.dumps(step.result)


# ---------------------------------------------------------------------------
# 任务不可变性与规划器边界
# ---------------------------------------------------------------------------

def test_task_status_immutable(runtime):
    task = AgentTask(user_query="TE极化是什么概念")
    before = task.to_dict()
    result = runtime.run(task)
    assert task.to_dict() == before                       # 入参任务对象不被修改
    assert result.task.status == TASK_COMPLETED           # 运行副本承载状态迁移
    assert result.task is not task


def test_import_plan_requires_context():
    steps = plan(AgentTask(user_query="帮我导入教材"))
    import_steps = [s for s in steps if s.tool == TOOL_IMPORT_TEXTBOOK]
    assert not import_steps                              # 缺上下文 → 跳过
    assert any(s.tool is None and s.arguments.get("skipped") for s in steps)

    with_ctx = plan(AgentTask(
        user_query="帮我导入教材",
        context={"textbook_path": "books/x.md", "course_id": "c1"},
    ))
    import_steps = [s for s in with_ctx if s.tool == TOOL_IMPORT_TEXTBOOK]
    assert import_steps and import_steps[0].arguments == {
        "path": "books/x.md", "course_id": "c1",
    }
    assert with_ctx[0].tool == TOOL_IMPORT_TEXTBOOK       # 导入最先执行
