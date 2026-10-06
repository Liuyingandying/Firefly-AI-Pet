# -*- coding: utf-8 -*-
"""Knowledge injector — 显式 confirm 门 → ConceptNode 构造（M2.4）。

规则 4/5 的落点::

    ConceptCandidate × N ──批准（approved_ids）──▶ ConceptNode × N（返回值）
                              │
                              └─▶ 调用方经 KnowledgeGraph(nodes=…) 构造注入
                                  （M1.2, 本包**永不持有/写入图实例**）

- 门是显式的: 未提供非空 ``approved_ids`` → confirmation_required 拒绝;
- 未批准的候选被跳过并记录; 前置指向未批准候选的边被丢弃（记 skipped）;
- 构造 ``ConceptNode`` ≠ 修改图——M1.2 ``KnowledgeGraph`` 无任何变异 API,
  注入是调用方的构造注入职责（不修改图核心）。
"""

from __future__ import annotations

from core.learning.knowledge_import.schema import (
    ERR_CONFIRMATION_REQUIRED,
    ERR_CYCLE,
    ERR_INVALID_INPUT,
    KnowledgeCandidateResult,
    KnowledgeInjectionResult,
)


def convert_approved(
    result: KnowledgeCandidateResult | None,
    *,
    approved_ids: set[str] | frozenset[str] | list[str] | tuple[str, ...],
    confirmed_by: str = "",
) -> KnowledgeInjectionResult:
    """把**已批准**的候选构造为 ConceptNode; 规则 5 的显式 confirm 门。

    ``confirmed_by`` 为审计字段（记录批准者）, 仅进 skipped/审计语义,
    不影响转换逻辑。永不抛异常; 永不触碰任何 KnowledgeGraph 实例。
    """
    if result is None or not isinstance(result, KnowledgeCandidateResult):
        return KnowledgeInjectionResult(
            success=False, error=f"{ERR_INVALID_INPUT}: result is not a candidate result"
        )
    if not result.success:
        return KnowledgeInjectionResult(
            success=False,
            error=f"{ERR_INVALID_INPUT}: source extraction failed ({result.error})",
        )
    approved = {str(a) for a in (approved_ids or ()) if str(a)}
    if not approved:
        # ---- 规则 5: 无显式批准 → 拒绝转换 ----------------------------------
        return KnowledgeInjectionResult(
            success=False,
            error=f"{ERR_CONFIRMATION_REQUIRED}: approved_ids is empty",
        )

    from core.learning.knowledge_graph.schema import ConceptNode  # 只构造, 不写图

    by_id = {c.concept_id: c for c in result.candidates}
    approved_set = approved & set(by_id)
    skipped: list[str] = [c.concept_id for c in result.candidates if c.concept_id not in approved_set]

    nodes = []
    dropped_edges: list[str] = []
    for candidate in result.candidates:
        if candidate.concept_id not in approved_set:
            continue
        # 前置边只保留指向已批准候选的（未批准前置 → 丢弃边, 节点保留）
        prerequisites = tuple(
            prereq for prereq in candidate.prerequisites if prereq in approved_set
        )
        dropped = len(candidate.prerequisites) - len(prerequisites)
        if dropped:
            dropped_edges.append(candidate.concept_id)
            skipped.append(f"{candidate.concept_id}:prerequisite-dropped")
        nodes.append(
            ConceptNode(
                concept_id=candidate.concept_id,
                name=candidate.name,
                chapter=candidate.chapter,
                prerequisites=prerequisites,
                related_experiments=candidate.related_experiments,
            )
        )

    # 父链前置构造上无环; 防御性闭环检测（若未来扩展非父链前置来源）
    cycle = _find_node_cycle(nodes)
    if cycle:
        return KnowledgeInjectionResult(
            success=False,
            error=f"{ERR_CYCLE}: {' -> '.join(cycle)}",
        )

    return KnowledgeInjectionResult(
        success=True,
        nodes=tuple(nodes),
        approved_count=len(nodes),
        skipped=tuple(skipped),
    )


def _find_node_cycle(nodes: list) -> list[str]:
    """ConceptNode 前置闭环检测（防御; 空表 = 无环）。"""
    by_id = {node.concept_id: node for node in nodes}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(cid: str) -> list[str] | None:
        status = state.get(cid)
        if status == 1:
            index = stack.index(cid)
            return stack[index:]
        if status == 2:
            return None
        node = by_id.get(cid)
        if node is None:
            return None
        state[cid] = 1
        stack.append(cid)
        for prereq in node.prerequisites:
            cycle = visit(prereq)
            if cycle is not None:
                return cycle
        stack.pop()
        state[cid] = 2
        return None

    for node_id in by_id:
        cycle = visit(node_id)
        if cycle is not None:
            return cycle
    return []
