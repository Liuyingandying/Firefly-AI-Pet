# -*- coding: utf-8 -*-
"""TJULLM adapters — 工具处理器 + 默认注册表 + tool call 入口（M2.7）。

每个处理器 = 既有 Learning API 的薄投影（只读包装, 零核心改动）::

    TJULLM tool_call JSON ──execute_tool_call──▶ ToolRegistry.execute
                                                     │ 白名单 + 参数校验
                                                     ▼
                                 import_textbook   → extractor + curriculum_generator
                                 query_concept     → knowledge_graph（只读）
                                 run_experiment    → simulation.runner（只读调用）
                                 generate_learning_plan → knowledge_graph + recommendation

安全:
- 白名单即能力边界（registry 词表外名字结构上不可调用）;
- **文件越权防护**: import_textbook 的路径必须位于允许根内
  （默认 = 进程工作目录; 宿主可用 ``configure_allowed_roots`` 收紧）;
- 仿真实验的参数经 runner 封闭词表校验, 模板固定——无任意代码执行面。
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.learning.curriculum_generator import CurriculumGenerator
from core.learning.extractor import NativeExtractor
from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph
from core.learning.mastery_adapter.schema import MasterySignal
from core.learning.recommendation import RecommendationEngine
from core.learning.simulation.runner import SimulationRunner
from core.learning.tools.schema import (
    ERR_INVALID_ARGUMENTS,
    ERR_NOT_FOUND,
    ERR_PATH_DENIED,
    TOOL_GENERATE_LEARNING_PLAN,
    TOOL_IMPORT_TEXTBOOK,
    TOOL_QUERY_CONCEPT,
    TOOL_RUN_EXPERIMENT,
    ParsedToolCall,
    ToolResult,
    parse_tool_call,
)
from core.learning.tools.tool_registry import ToolRegistry

# ---------------------------------------------------------------------------
# 文件允许根（宿主可收紧; 默认 = 进程工作目录）
# ---------------------------------------------------------------------------

_ALLOWED_ROOTS: list[Path] = [Path.cwd().resolve()]


def configure_allowed_roots(roots: list[Path] | tuple[Path, ...]) -> None:
    """设置 import_textbook 允许的目录根（安全边界, 宿主/测试注入）。"""
    global _ALLOWED_ROOTS
    _ALLOWED_ROOTS = [Path(root).resolve() for root in roots]


def _path_outside_roots(path: Path) -> str | None:
    """路径若不在任何允许根内, 返回越权描述; 否则 None。"""
    try:
        resolved = path.resolve()
    except OSError as exc:
        return f"unresolvable path: {exc}"
    for root in _ALLOWED_ROOTS:
        if resolved.is_relative_to(root):
            return None
    return f"{resolved} is outside allowed roots"


# ---------------------------------------------------------------------------
# 处理器（签名 = schema parameters; 返回 ToolResult, 永不抛异常）
# ---------------------------------------------------------------------------

def _tool_import_textbook(path: str, course_id: str) -> ToolResult:
    textbook = Path(str(path))
    violation = _path_outside_roots(textbook)
    if violation is not None:
        return ToolResult(
            success=False, tool=TOOL_IMPORT_TEXTBOOK,
            error=f"{ERR_PATH_DENIED}: {violation}",
        )

    extraction = NativeExtractor().extract(textbook)
    if not extraction.success:
        return ToolResult(
            success=False, tool=TOOL_IMPORT_TEXTBOOK, error=extraction.error
        )

    generation = CurriculumGenerator().generate(
        extraction.document_structure, course_id=str(course_id).strip(),
    )
    if not generation.success:
        return ToolResult(
            success=False, tool=TOOL_IMPORT_TEXTBOOK, error=generation.error
        )

    summary = generation.to_dict()
    return ToolResult(
        success=True,
        tool=TOOL_IMPORT_TEXTBOOK,
        result={
            "document_title": extraction.document_structure.title,
            "course_id": str(course_id).strip(),
            "draft": summary["curriculum_draft"],
            "lessons": [s["lesson_path"] for s in summary["generated_sections"]],
            "section_count": len(summary["generated_sections"]),
            "warnings": summary["warnings"],
            "note": "draft generated; NOT confirmed/activated (course-side gate unchanged)",
        },
    )


def _tool_query_concept(concept_id: str) -> ToolResult:
    graph = KnowledgeGraph(DEFAULT_NODES)          # 只读查询, 每次新实例
    cid = str(concept_id).strip()
    node = graph.get_concept(cid)
    if node is None:
        return ToolResult(
            success=False, tool=TOOL_QUERY_CONCEPT,
            error=f"{ERR_NOT_FOUND}: concept {cid!r}",
        )
    return ToolResult(
        success=True,
        tool=TOOL_QUERY_CONCEPT,
        result={
            "concept": node.to_dict(),
            "learning_path": list(graph.find_learning_path(cid)),
            "related_experiments": list(node.related_experiments),
        },
    )


def _tool_run_experiment(experiment_id: str, params: dict | None = None) -> ToolResult:
    runner = SimulationRunner(timeout_s=30)
    out_dir = Path(tempfile.mkdtemp(prefix="firefly_tool_sim_"))
    result = runner.run_experiment(
        str(experiment_id).strip(),
        params_override=params if isinstance(params, dict) else None,
        out_dir=out_dir,
    )
    if not result.ok:
        return ToolResult(
            success=False, tool=TOOL_RUN_EXPERIMENT, error=result.reason
        )
    return ToolResult(
        success=True,
        tool=TOOL_RUN_EXPERIMENT,
        result={
            "experiment_id": result.experiment_id,
            "display_name": result.display_name,
            "status": result.status,
            "elapsed_ms": result.elapsed_ms,
            "artifacts": {
                "png": str(result.png_path),
                "gif": str(result.gif_path),
                "markdown": str(result.md_path),
            },
            "params": dict(result.params_payload),
        },
    )


def _neutral_signal(concept_id: str) -> MasterySignal:
    """冷启动中性信号（无掌握度数据时的规划基线, 强度 = 推荐阈值 0.6）。"""
    return MasterySignal(
        concept_id=concept_id,
        signal_type="basic",
        source="tjullm-tool",
        strength=0.6,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        evidence_id="",
    )


def _tool_generate_learning_plan(concept_id: str) -> ToolResult:
    graph = KnowledgeGraph(DEFAULT_NODES)
    cid = str(concept_id).strip()
    learning_path = graph.find_learning_path(cid)
    if not learning_path:
        return ToolResult(
            success=False, tool=TOOL_GENERATE_LEARNING_PLAN,
            error=f"{ERR_NOT_FOUND}: concept {cid!r} (unknown or cyclic prerequisites)",
        )

    engine = RecommendationEngine(graph)
    steps = []
    for step_concept in learning_path:
        recommendation = engine.recommend(_neutral_signal(step_concept))
        entry = recommendation.to_dict()
        entry["concept"] = step_concept
        steps.append(entry)

    return ToolResult(
        success=True,
        tool=TOOL_GENERATE_LEARNING_PLAN,
        result={
            "concept_id": cid,
            "learning_path": list(learning_path),
            "steps": steps,
            "note": "cold-start plan (neutral strength 0.6, no mastery data)",
        },
    )


# ---------------------------------------------------------------------------
# 默认注册表 + TJULLM tool call 入口
# ---------------------------------------------------------------------------

def build_default_registry() -> ToolRegistry:
    """组装白名单默认注册表（4 工具, 与 TOOL_SCHEMAS 词表一致）。"""
    registry = ToolRegistry()
    registry.register(TOOL_IMPORT_TEXTBOOK, _tool_import_textbook)
    registry.register(TOOL_QUERY_CONCEPT, _tool_query_concept)
    registry.register(TOOL_RUN_EXPERIMENT, _tool_run_experiment)
    registry.register(TOOL_GENERATE_LEARNING_PLAN, _tool_generate_learning_plan)
    return registry


_DEFAULT_REGISTRY: ToolRegistry | None = None


def _default_registry() -> ToolRegistry:
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = build_default_registry()
    return _DEFAULT_REGISTRY


def execute_tool_call(
    entry: dict | None,
    *,
    registry: ToolRegistry | None = None,
) -> ToolResult:
    """TJULLM/OpenAI ``tool_calls`` 条目 → 工具执行 → ToolResult。

    适配层唯一入口; 解析失败/白名单外/参数非法/处理器异常全部收敛为
    失败 ToolResult, 永不抛异常。
    """
    parsed, error = parse_tool_call(entry)
    if parsed is None:
        return ToolResult(
            success=False,
            tool=_entry_name(entry),
            error=error,
        )
    active_registry = registry if registry is not None else _default_registry()
    result = active_registry.execute(parsed.name, parsed.arguments)
    if not result.tool:
        result = ToolResult(
            success=result.success, tool=parsed.name,
            result=result.result, error=result.error,
        )
    return result


def _entry_name(entry: dict | None) -> str:
    """尽力提取工具名用于失败结果审计（解析失败时）。"""
    if isinstance(entry, dict) and isinstance(entry.get("function"), dict):
        return str(entry["function"].get("name", "") or "")
    return ""


__all__ = [
    "configure_allowed_roots",
    "build_default_registry",
    "execute_tool_call",
    "ParsedToolCall",
    "ToolResult",
    ERR_INVALID_ARGUMENTS,
    ERR_PATH_DENIED,
]
