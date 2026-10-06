# -*- coding: utf-8 -*-
"""M3.5 Agent Context Grounding 验收测试。

覆盖：context 生成 / concept 列表 / experiment 列表 / 模型收到 grounding /
未知 concept 减少 / JSON 序列化, 另加渲染格式、provider 归一与降级、
默认路径不受影响。
隔离：Mock/Capturing 模型零网络; 不修改 TJULLMModel/ToolRegistry/Runner/
KnowledgeGraph 数据结构。
"""

from __future__ import annotations

import json

import pytest

from core.learning.agent import AgentTask, LearningAgentRuntime
from core.learning.agent.context import ContextBuilder, LearningContext
from core.learning.agent.model import ModelRequest, ModelResponse, ToolCall
from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.simulation.registry import ExperimentRegistry
from core.learning.tools.schema import TOOL_QUERY_CONCEPT


@pytest.fixture(scope="module")
def builder():
    return ContextBuilder()


@pytest.fixture(scope="module")
def context(builder):
    return builder.build()


# ---------------------------------------------------------------------------
# 1. context 生成
# ---------------------------------------------------------------------------

def test_context_generation(context):
    assert isinstance(context, LearningContext)
    assert context.available_concepts
    assert context.available_experiments
    assert context.learning_goal == ""
    assert context.hints == ()


# ---------------------------------------------------------------------------
# 2. concept 列表
# ---------------------------------------------------------------------------

def test_concept_list_from_graph(context):
    assert context.available_concepts == tuple(sorted({
        "em-uniform-plane-wave", "em-te-polarization", "em-tm-polarization",
    }))


def test_graph_list_concepts_readonly():
    """新增只读枚举: 与图内容一致, 且不改变图。"""
    graph = KnowledgeGraph()
    ids = graph.list_concepts()
    assert ids == ("em-te-polarization", "em-tm-polarization", "em-uniform-plane-wave")
    for cid in ids:
        assert graph.get_concept(cid) is not None          # 图内容原样
    assert graph.find_learning_path("em-te-polarization") == (
        "em-uniform-plane-wave", "em-te-polarization",
    )


# ---------------------------------------------------------------------------
# 3. experiment 列表
# ---------------------------------------------------------------------------

def test_experiment_list_from_registry(context):
    assert "uniform-plane-wave" in context.available_experiments


# ---------------------------------------------------------------------------
# 4. 模型收到 grounding（Runtime 注入）
# ---------------------------------------------------------------------------

class _CapturingModel:
    """记录收到的 request 并按接地词表回第一个概念查询提议。"""

    name = "capturing"

    def __init__(self):
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(content="grounded answer", finish_reason="stop")


def _extract_grounding(request: ModelRequest) -> str:
    for message in request.messages:
        if message.get("role") == "system" and "Available concepts" in str(message.get("content")):
            return str(message["content"])
    return ""


def test_model_receives_grounding():
    captured = _CapturingModel()
    runtime = LearningAgentRuntime(
        model=captured,
        context_provider=ContextBuilder(),                 # 带 .build() 的 provider
    )
    result = runtime.run(AgentTask(user_query="TE极化是什么概念"))
    assert result.success is True
    grounding = _extract_grounding(captured.requests[0])
    assert grounding != ""
    assert "Available concepts:" in grounding
    assert "em-uniform-plane-wave" in grounding
    assert "em-te-polarization" in grounding
    assert "Available experiments:" in grounding
    assert "uniform-plane-wave" in grounding
    # 合并进首条 system 消息（保留原 system 内容）
    first = captured.requests[0].messages[0]
    assert first["role"] == "system"
    assert "学习规划助手" in first["content"]


def test_callable_provider_and_hints():
    captured = _CapturingModel()
    provider = lambda: LearningContext(                    # noqa: E731 - 零参 callable
        available_concepts=("c1",),
        available_experiments=("e1",),
        learning_goal="掌握 TE/TM",
        hints=("只允许使用列出的 id",),
    )
    runtime = LearningAgentRuntime(model=captured, context_provider=provider)
    runtime.run(AgentTask(user_query="x"))
    grounding = _extract_grounding(captured.requests[0])
    assert "c1" in grounding and "e1" in grounding
    assert "掌握 TE/TM" in grounding and "只允许使用列出的 id" in grounding


def test_no_provider_keeps_default_path():
    """context_provider=None（默认）→ 无接地注入, 与 M3.1 行为一致。"""
    captured = _CapturingModel()
    runtime = LearningAgentRuntime(model=captured)
    runtime.run(AgentTask(user_query="x"))
    assert _extract_grounding(captured.requests[0]) == ""
    first = captured.requests[0].messages[0]
    assert "Available concepts" not in first["content"]


def test_failing_provider_degrades_silently():
    """provider 抛异常 → 静默降级（无接地）, 任务照常完成。"""

    def _boom():
        raise RuntimeError("context source down")

    captured = _CapturingModel()
    runtime = LearningAgentRuntime(model=captured, context_provider=_boom)
    result = runtime.run(AgentTask(user_query="x"))
    assert result.success is True
    assert _extract_grounding(captured.requests[0]) == ""


# ---------------------------------------------------------------------------
# 5. 未知 concept 减少（接地 → 模型使用合法 id）
# ---------------------------------------------------------------------------

class _GroundedMockModel:
    """模拟真实平台行为: 从 system 接地中提取概念词表并使用之（不编造）。"""

    name = "grounded-mock"

    def generate(self, request: ModelRequest) -> ModelResponse:
        grounding = _extract_grounding(request)
        available = [
            line.strip("- ").split(" ")[0]
            for line in grounding.splitlines()
            if line.startswith("- em-")
        ]
        if available:
            return ModelResponse(
                content="",
                tool_calls=(ToolCall(name=TOOL_QUERY_CONCEPT,
                                     arguments={"concept_id": available[0]}),),
                finish_reason="tool_calls",
            )
        # 无接地时: 模拟 M3.4 实测的幻构行为
        return ModelResponse(
            content="",
            tool_calls=(ToolCall(name=TOOL_QUERY_CONCEPT,
                                 arguments={"concept_id": "TE_TM_modes"}),),
            finish_reason="tool_calls",
        )


def test_unknown_concept_reduced_by_grounding():
    """无接地 → 幻构 id 被拒（not_found）; 有接地 → 合法 id 执行成功。"""
    task = AgentTask(user_query="帮我理解TE和TM模式")

    naive_runtime = LearningAgentRuntime(model=_GroundedMockModel())
    naive = naive_runtime.run(task)
    assert naive.success is False                          # 幻构 id → not_found
    assert "not_found" in naive.error

    grounded_runtime = LearningAgentRuntime(
        model=_GroundedMockModel(), context_provider=ContextBuilder(),
    )
    grounded = grounded_runtime.run(task)
    assert grounded.success is True                        # 接地后使用合法 id
    assert any(
        s.tool == TOOL_QUERY_CONCEPT and s.result.get("success")
        for s in grounded.steps
    )


# ---------------------------------------------------------------------------
# 6. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(context, builder):
    d = context.to_dict()
    text = json.dumps(d, ensure_ascii=False)
    parsed = json.loads(text)
    assert len(parsed["available_concepts"]) == 3
    assert "uniform-plane-wave" in parsed["available_experiments"]

    grounding = builder.render_grounding(
        LearningContext(available_concepts=("c1",), available_experiments=("e1",),
                        learning_goal="g", hints=("h",))
    )
    parsed_g = json.loads(grounding.to_json())
    assert "[Learning Environment]" in parsed_g["text"]
    assert parsed_g["context"]["learning_goal"] == "g"


def test_render_format(builder):
    grounding = builder.render_grounding()
    assert grounding.text.startswith("[Learning Environment]")
    assert "Use ONLY the concept ids" in grounding.text    # 防幻构指令
    assert "- em-uniform-plane-wave (均匀平面波传播)" in grounding.text


def test_builder_with_injected_graph_and_registry(tmp_path):
    """graph/registry 注入隔离: 自定义图只暴露自定义概念。"""
    from core.learning.knowledge_graph import ConceptNode

    tiny_graph = KnowledgeGraph([
        ConceptNode(concept_id="tiny-c", name="微小概念"),
    ])
    tiny_registry = ExperimentRegistry(search_paths=[tmp_path])   # 空目录
    context = ContextBuilder(graph=tiny_graph, registry=tiny_registry).build()
    assert context.available_concepts == ("tiny-c",)
    assert context.available_experiments == ()
