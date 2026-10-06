# -*- coding: utf-8 -*-
"""LearningAgentRuntime — Agent 执行循环（M3.0）。

规划 → 执行 → 收集 → 解释 → 回复；失败即结构化结果, 永不异常退出::

    AgentTask ──▶ planner.plan ──▶ 逐 PlannedStep：
        tool=None  → 解释层组装（M0.5 单一解释源 + 知识图/推荐）
        tool≠None  → selector 白名单 → ToolRegistry.execute（M2.7 安全层）
    ──▶ AgentRunResult（markdown 回复 + 可审计 steps）

安全: 用户输入只进规则匹配与工具参数（结构化数据）, 永不进代码路径;
三重白名单拦截（selector → registry → 词表常量）。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from core.learning.agent.planner import plan
from core.learning.agent.schema import (
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_RUNNING,
    AgentRunResult,
    AgentStep,
    AgentTask,
)
from core.learning.agent.tool_selector import ToolDeniedError, assert_allowed
from core.learning.explanation import SimulationExplainer
from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.simulation.runner import SimulationResult
from core.learning.tools.adapters import execute_tool_call
from core.learning.tools.tool_registry import ToolRegistry


class LearningAgentRuntime:
    """学习智能体运行时（规则规划 + 白名单工具 + 解释层组装）。"""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        graph: KnowledgeGraph | None = None,
        model=None,
        context_provider=None,
    ):
        self._registry = registry if registry is not None else _default_registry()
        self._graph = graph if graph is not None else KnowledgeGraph(DEFAULT_NODES)
        # M3.1: 可选模型后端（LearningModel 协议）。None = 保持 M3.0 规则规划。
        self._model = model
        # M3.5: 可选接地提供方（零参 callable / 带 .build() 的 ContextBuilder）。
        # None = 不注入接地（现状不变）。
        self._context_provider = context_provider

    def run(self, task: AgentTask) -> AgentRunResult:
        """执行一次学习任务；任何失败都以结构化 AgentRunResult 返回。"""
        if not isinstance(task, AgentTask):
            return self._fail(None, "invalid_input", "task is not an AgentTask")
        if not str(task.user_query or "").strip():
            return self._fail(task, "empty_query", "user_query is empty")

        running = dataclasses.replace(task, status=TASK_RUNNING)

        # ---- M3.1: 模型通道（可选）。None → 规则规划路径原样执行 --------------
        if self._model is not None:
            return self._run_with_model(running)

        steps: list[AgentStep] = []
        tools_used: list[str] = []
        warnings: list[str] = []

        concept_payloads: list[dict] = []
        experiment_payload: dict | None = None
        plan_payload: dict | None = None
        import_payload: dict | None = None

        try:
            planned_steps = plan(running)
            steps.append(AgentStep(
                step_index=0,
                thought_type="plan",
                action=f"规则规划：{len(planned_steps)} 个步骤",
                tool="",
                result={"steps": [s.action for s in planned_steps]},
            ))

            for index, planned in enumerate(planned_steps, start=1):
                # ---- explain 终步（tool=None）：解释层组装 -------------------
                if planned.tool is None:
                    response = self._compose_response(
                        planned.arguments.get("focus_concepts", []),
                        concept_payloads, experiment_payload, plan_payload, import_payload,
                    )
                    steps.append(AgentStep(
                        step_index=index,
                        thought_type="explain",
                        action=planned.action,
                        tool="",
                        result={"response_chars": len(response)},
                    ))
                    return AgentRunResult(
                        success=True,
                        task=dataclasses.replace(running, status=TASK_COMPLETED),
                        steps=tuple(steps),
                        response=response,
                        tools_used=tuple(tools_used),
                        warnings=tuple(warnings),
                    )

                # ---- 工具步：白名单 → 注册表执行 ------------------------------
                assert_allowed(planned.tool)                       # ToolDeniedError → 兜底
                entry = {
                    "id": f"agent-step-{index}",
                    "type": "function",
                    "function": {
                        "name": planned.tool,
                        "arguments": json.dumps(planned.arguments, ensure_ascii=False),
                    },
                }
                tool_result = execute_tool_call(entry, registry=self._registry)
                steps.append(AgentStep(
                    step_index=index,
                    thought_type="act",
                    action=planned.action,
                    tool=planned.tool,
                    result={
                        "success": tool_result.success,
                        "error": tool_result.error,
                        "result": dict(tool_result.result),
                    },
                ))

                if not tool_result.success:
                    # 结构化失败：已记录的 trace 保留, 状态置 failed
                    return AgentRunResult(
                        success=False,
                        task=dataclasses.replace(running, status=TASK_FAILED),
                        steps=tuple(steps),
                        response="",
                        tools_used=tuple(tools_used),
                        warnings=tuple(warnings),
                        error=f"tool_failed: {tool_result.error}",
                    )

                tools_used.append(planned.tool)
                payload = dict(tool_result.result)
                if planned.tool == "query_concept":
                    concept_payloads.append(payload)
                elif planned.tool == "run_experiment":
                    experiment_payload = payload
                elif planned.tool == "generate_learning_plan":
                    plan_payload = payload
                elif planned.tool == "import_textbook":
                    import_payload = payload

            # 理论不可达（planner 恒以 explain 终步收尾）, 防御性兜底
            return self._fail(running, "planner_error", "plan ended without explain step")

        except ToolDeniedError as exc:
            steps.append(AgentStep(
                step_index=len(steps) + 1, thought_type="observe",
                action=f"工具被白名单拒绝：{exc}", tool="",
                result={"denied": True},
            ))
            warnings.append(f"tool_denied: {exc}")
            return AgentRunResult(
                success=False,
                task=dataclasses.replace(running, status=TASK_FAILED),
                steps=tuple(steps),
                response="",
                tools_used=tuple(tools_used),
                warnings=tuple(warnings),
                error=f"tool_denied: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - 错误即结果
            return self._fail(
                dataclasses.replace(running, status=TASK_FAILED),
                "execution_failed", f"{type(exc).__name__}: {exc}",
                steps=tuple(steps), tools_used=tuple(tools_used), warnings=warnings,
            )

    # ------------------------------------------------------------------
    # M3.1: 模型通道（可选入口, LearningModel 协议）
    # ------------------------------------------------------------------

    def _run_with_model(self, running: AgentTask) -> AgentRunResult:
        """模型规划通道：request → model.generate → 白名单执行 → 解释组装。"""
        from core.learning.agent.model.schema import ModelRequest
        from core.learning.tools.schema import all_tool_schemas

        steps: list[AgentStep] = []
        tools_used: list[str] = []
        warnings: list[str] = []
        concept_payloads: list[dict] = []
        experiment_payload: dict | None = None
        plan_payload: dict | None = None
        import_payload: dict | None = None

        try:
            model = self._model
            if not hasattr(model, "generate"):
                return self._fail(
                    dataclasses.replace(running, status=TASK_FAILED),
                    "invalid_input", "model does not implement generate()",
                    steps=tuple(steps),
                )

            steps.append(AgentStep(
                step_index=1,
                thought_type="plan",
                action=f"模型规划（{getattr(model, 'name', type(model).__name__)}）",
                tool="",
                result={"model": getattr(model, "name", type(model).__name__)},
            ))

            request = ModelRequest.from_task(running, tools=tuple(all_tool_schemas()))
            if self._context_provider is not None:
                request = self._ground_request(request)          # M3.5 接地注入
            response = model.generate(request)
            steps.append(AgentStep(
                step_index=2,
                thought_type="observe",
                action="模型返回",
                tool="",
                result={
                    "finish_reason": response.finish_reason,
                    "tool_call_count": len(response.tool_calls),
                    "content_chars": len(response.content),
                },
            ))

            # ---- 空回复安全：无调用无内容 → 警告 + 回退文案（不失败） -----------
            if not response.tool_calls:
                content = response.content.strip()
                if not content:
                    warnings.append("model returned empty response")
                    content = (
                        "（模型未返回有效内容）请换个问法，例如："
                        "帮我理解均匀平面波中的TE和TM模式。"
                    )
                return AgentRunResult(
                    success=True,
                    task=dataclasses.replace(running, status=TASK_COMPLETED),
                    steps=tuple(steps),
                    response=content,
                    tools_used=(),
                    warnings=tuple(warnings),
                )

            # ---- 逐条执行模型提议（白名单 → 注册表, 三层安全原样） --------------
            for index, call in enumerate(response.tool_calls, start=3):
                action_text = f"执行模型提议：{call.name}"
                try:
                    assert_allowed(call.name)
                except ToolDeniedError as exc:
                    steps.append(AgentStep(
                        step_index=index,
                        thought_type="observe",
                        action=f"{action_text}（被白名单拒绝）",
                        tool=call.name,
                        result={"denied": True, "detail": str(exc)},
                    ))
                    warnings.append(f"tool_denied: {exc}")
                    continue

                entry = {
                    "id": f"agent-model-step-{index}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                tool_result = execute_tool_call(entry, registry=self._registry)
                steps.append(AgentStep(
                    step_index=index,
                    thought_type="act",
                    action=action_text,
                    tool=call.name,
                    result={
                        "success": tool_result.success,
                        "error": tool_result.error,
                        "result": dict(tool_result.result),
                    },
                ))

                if not tool_result.success:
                    return AgentRunResult(
                        success=False,
                        task=dataclasses.replace(running, status=TASK_FAILED),
                        steps=tuple(steps),
                        response="",
                        tools_used=tuple(tools_used),
                        warnings=tuple(warnings),
                        error=f"tool_failed: {tool_result.error}",
                    )

                tools_used.append(call.name)
                payload = dict(tool_result.result)
                if call.name == "query_concept":
                    concept_payloads.append(payload)
                elif call.name == "run_experiment":
                    experiment_payload = payload
                elif call.name == "generate_learning_plan":
                    plan_payload = payload
                elif call.name == "import_textbook":
                    import_payload = payload

            focus = [
                p.get("concept", {}).get("concept_id", "")
                for p in concept_payloads
                if p.get("concept")
            ]
            response_md = self._compose_response(
                focus, concept_payloads, experiment_payload, plan_payload, import_payload,
            )
            steps.append(AgentStep(
                step_index=len(steps) + 1,
                thought_type="explain",
                action="综合解释并给出学习建议（模型工具结果）",
                tool="",
                result={"response_chars": len(response_md)},
            ))
            return AgentRunResult(
                success=True,
                task=dataclasses.replace(running, status=TASK_COMPLETED),
                steps=tuple(steps),
                response=response_md,
                tools_used=tuple(tools_used),
                warnings=tuple(warnings),
            )
        except Exception as exc:  # noqa: BLE001 - 错误即结果
            return self._fail(
                dataclasses.replace(running, status=TASK_FAILED),
                "execution_failed", f"{type(exc).__name__}: {exc}",
                steps=tuple(steps), tools_used=tuple(tools_used), warnings=warnings,
            )

    # ------------------------------------------------------------------
    # M3.5: 接地注入（provider 归一 + system 消息合并; 失败静默降级）
    # ------------------------------------------------------------------

    def _ground_request(self, request):
        """把 context_provider 产出的接地文本合并进首条 system 消息。"""
        from core.learning.agent.context.builder import ContextBuilder
        from core.learning.agent.context.schema import ContextGrounding, LearningContext

        provider = self._context_provider
        try:
            outcome = provider() if callable(provider) else provider.build()
        except Exception:  # noqa: BLE001 - 接地失败静默降级, 不阻断任务
            return request

        try:
            if isinstance(outcome, ContextGrounding):
                grounding = outcome.text
            elif isinstance(outcome, LearningContext):
                grounding = ContextBuilder(
                    graph=self._graph
                ).render_grounding(outcome).text
            elif isinstance(outcome, str):
                grounding = outcome
            else:
                return request
        except Exception:  # noqa: BLE001
            return request

        grounding = grounding.strip()
        if not grounding:
            return request

        messages = list(request.messages)
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            merged = dict(messages[0])
            merged["content"] = f"{messages[0].get('content', '')}\n\n{grounding}"
            messages[0] = merged
        else:
            messages.insert(0, {"role": "system", "content": grounding})
        return dataclasses.replace(request, messages=tuple(messages))

    # ------------------------------------------------------------------
    # 解释与组装（复用 M0.5 单一解释源 + M1.2 知识图）
    # ------------------------------------------------------------------

    def _compose_response(
        self,
        focus_concepts: list,
        concept_payloads: list[dict],
        experiment_payload: dict | None,
        plan_payload: dict | None,
        import_payload: dict | None,
    ) -> str:
        lines: list[str] = ["# 学习助手回复", ""]

        # ---- 概念解释 ---------------------------------------------------------
        lines.append("## 概念解释")
        if not concept_payloads:
            lines.append("（本次未查询概念）")
        for payload in concept_payloads:
            concept = payload.get("concept", {})
            lines.append(f"### {concept.get('name', '?')}")
            lines.append(f"- 章节：{concept.get('chapter', '—')}")
            prerequisites = concept.get("prerequisites", ()) or ()
            lines.append(
                f"- 前置概念：{('、'.join(prerequisites)) if prerequisites else '（无——入门概念）'}"
            )
            experiments = payload.get("related_experiments", ()) or ()
            lines.append(
                f"- 关联实验：{('、'.join(experiments)) if experiments else '（暂无）'}"
            )
            learning_path = payload.get("learning_path", ()) or ()
            if learning_path:
                lines.append(f"- 建议学习路径：{' → '.join(learning_path)}")
        lines.append("")

        # ---- 仿真实验 + Explanation Layer -------------------------------------
        if experiment_payload is not None:
            lines.append("## 仿真实验结果")
            artifacts = experiment_payload.get("artifacts", {})
            lines.append(
                f"- 状态：{experiment_payload.get('status', '—')} · "
                f"耗时 {experiment_payload.get('elapsed_ms', 0)} ms"
            )
            if artifacts.get("png"):
                lines.append(f"- 电场分布图：{artifacts['png']}")
            if artifacts.get("gif"):
                lines.append(f"- 传播动画：{artifacts['gif']}")
            if artifacts.get("markdown"):
                lines.append(f"- 实验说明：{artifacts['markdown']}")

            explanation = self._explain_experiment(experiment_payload)
            if explanation is not None:
                lines.append("")
                lines.append("### 结果解读（Explanation Layer）")
                lines.append(explanation["summary"])
                for point in explanation["key_points"]:
                    lines.append(f"- {point}")
            lines.append("")

        # ---- 教材导入（若发生） ------------------------------------------------
        if import_payload is not None:
            lines.append("## 教材导入")
            lines.append(
                f"- 课程 {import_payload.get('course_id', '—')}："
                f"{import_payload.get('document_title', '—')}（"
                f"{import_payload.get('section_count', 0)} 个教学节）"
            )
            lines.append("")

        # ---- 学习计划（若请求） ------------------------------------------------
        if plan_payload is not None:
            lines.append("## 学习计划")
            for step in plan_payload.get("steps", []):
                lines.append(f"- `{step.get('concept', '—')}` → {step.get('action', '—')}")
            lines.append("")

        # ---- 下一步学习建议 ----------------------------------------------------
        lines.append("## 下一步学习建议")
        suggestions: list[str] = []
        if experiment_payload is not None:
            explanation = self._explain_experiment(experiment_payload)
            if explanation is not None:
                suggestions.extend(explanation["suggestions"])
        for payload in concept_payloads:
            path = payload.get("learning_path", []) or []
            if len(path) >= 2:
                suggestions.append(f"按路径推进：{' → '.join(path)}")
        if not suggestions:
            suggestions.append("先从「均匀平面波传播」开始，逐步过渡到 TE/TM 极化。")
        for suggestion in suggestions:
            lines.append(f"- {suggestion}")

        return "\n".join(lines)

    def _explain_experiment(self, payload: dict) -> dict | None:
        """从实验工具结果重建 SimulationResult（只读构造）→ M0.5 解释层。"""
        try:
            artifacts = payload.get("artifacts", {}) or {}
            sim_result = SimulationResult(
                ok=True,
                status=str(payload.get("status", "completed")),
                experiment_id=str(payload.get("experiment_id", "")),
                display_name=str(payload.get("display_name", "")),
                out_dir=str(Path(artifacts.get("png", "x")).parent),
                png_path=artifacts.get("png", ""),
                gif_path=artifacts.get("gif", ""),
                md_path=artifacts.get("markdown", ""),
                elapsed_ms=int(payload.get("elapsed_ms", 0)),
                params_payload=dict(payload.get("params", {}) or {}),
            )
            response = SimulationExplainer().explain_result(sim_result)
            if response.ok:
                return {
                    "summary": response.summary,
                    "key_points": tuple(response.key_points),
                    "suggestions": tuple(response.suggestions),
                }
        except Exception:  # noqa: BLE001 - 解释失败不阻断回复组装
            pass
        return None

    @staticmethod
    def _fail(
        task: AgentTask | None,
        code: str,
        detail: str,
        *,
        steps: tuple = (),
        tools_used: tuple = (),
        warnings: list | None = None,
    ) -> AgentRunResult:
        status = TASK_FAILED
        task = dataclasses.replace(task, status=status) if task is not None else task
        return AgentRunResult(
            success=False,
            task=task,
            steps=tuple(steps),
            tools_used=tuple(tools_used),
            warnings=tuple(warnings or ()),
            error=f"{code}: {detail}",
        )


def _default_registry() -> ToolRegistry:
    from core.learning.tools.adapters import build_default_registry

    return build_default_registry()
