# -*- coding: utf-8 -*-
"""KnowledgeGraph — 概念知识图（M1.2 第一版）。

纯内存有向图（概念 → 前置概念）+ 四个只读查询接口。节点由人工/课程侧
预置（``DEFAULT_NODES`` 种子图为 EM 三概念, 与实验注册表的 concept_ids
对齐）; 图结构可任意注入（测试用）, 循环依赖在路径计算时检测。

拓扑语义: ``find_learning_path`` 返回"前置在前、目标在后"的学习路径;
目标（或其依赖闭包）处于环上时返回空路径——**安全拒绝, 不抛异常**。
"""

from __future__ import annotations

from core.learning.knowledge_graph.schema import ConceptNode

# ---------------------------------------------------------------------------
# 种子图：EM 三概念（与 experiments/electromagnetics 的 concept_ids 对齐）
# ---------------------------------------------------------------------------

DEFAULT_NODES: tuple[ConceptNode, ...] = (
    ConceptNode(
        concept_id="em-uniform-plane-wave",
        name="均匀平面波传播",
        chapter="电磁场与电磁波·均匀平面波",
        prerequisites=(),
        related_experiments=("uniform-plane-wave",),
    ),
    ConceptNode(
        concept_id="em-te-polarization",
        name="TE 极化",
        chapter="电磁场与电磁波·极化",
        prerequisites=("em-uniform-plane-wave",),
        related_experiments=("uniform-plane-wave",),
    ),
    ConceptNode(
        concept_id="em-tm-polarization",
        name="TM 极化",
        chapter="电磁场与电磁波·极化",
        prerequisites=("em-uniform-plane-wave",),
        related_experiments=("uniform-plane-wave",),
    ),
)


class KnowledgeGraph:
    """概念知识图（只读查询; 节点数据人工预置）。"""

    def __init__(self, nodes: list[ConceptNode] | tuple[ConceptNode, ...] | None = None):
        self._nodes: dict[str, ConceptNode] = {}
        for node in nodes if nodes is not None else DEFAULT_NODES:
            if node.concept_id not in self._nodes:      # 同 id 先到先得
                self._nodes[node.concept_id] = node

    # ------------------------------------------------------------------
    # 接口 1-3：单点查询（未知概念安全返回, 不抛异常）
    # ------------------------------------------------------------------

    def list_concepts(self) -> tuple[str, ...]:
        """全部概念 id 的只读枚举（sorted；M3.5 上下文接地使用）。"""
        return tuple(sorted(self._nodes))

    def get_concept(self, concept_id: str) -> ConceptNode | None:
        """按 id 取节点; 未知概念返回 None。"""
        return self._nodes.get(concept_id)

    def get_prerequisites(self, concept_id: str) -> tuple[str, ...]:
        """直接前置概念 id（声明顺序）; 未知概念返回空。"""
        node = self._nodes.get(concept_id)
        return node.prerequisites if node is not None else ()

    def get_related_experiments(self, concept_id: str) -> tuple[str, ...]:
        """可观察该概念的实验 id; 未知概念返回空。"""
        node = self._nodes.get(concept_id)
        return node.related_experiments if node is not None else ()

    # ------------------------------------------------------------------
    # 接口 4：学习路径 + 循环依赖检测
    # ------------------------------------------------------------------

    def find_learning_path(self, target_concept_id: str) -> tuple[str, ...]:
        """拓扑有序学习路径（前置在前, 含目标自身）。

        - 目标未知 → 空路径（安全返回）;
        - 目标依赖闭包存在循环 → 空路径（安全拒绝, 见 :meth:`find_cycle`）;
        - 空 prerequisite 合法 → 路径就是 (target,)。
        """
        if target_concept_id not in self._nodes:
            return ()
        if self._closure_has_cycle(target_concept_id):
            return ()

        path: list[str] = []
        seen: set[str] = set()

        def visit(cid: str) -> None:
            if cid in seen:
                return
            seen.add(cid)
            node = self._nodes.get(cid)
            if node is None:            # 前置引用未知概念 → 跳过（安全）
                return
            for prereq in node.prerequisites:
                visit(prereq)
            path.append(cid)

        visit(target_concept_id)
        return tuple(path)

    def find_cycle(self) -> tuple[str, ...]:
        """全图环检测：返回第一个发现环上的概念 id 序列（无环返回空）。"""
        state: dict[str, int] = {}      # 1= visiting, 2= done
        stack: list[str] = []

        def visit(cid: str) -> tuple[str, ...] | None:
            st = state.get(cid)
            if st == 1:                 # 回边 → 环
                idx = stack.index(cid)
                return tuple(stack[idx:])
            if st == 2:
                return None
            state[cid] = 1
            stack.append(cid)
            node = self._nodes.get(cid)
            if node is not None:
                for prereq in node.prerequisites:
                    cycle = visit(prereq)
                    if cycle is not None:
                        return cycle
            stack.pop()
            state[cid] = 2
            return None

        for cid in self._nodes:
            cycle = visit(cid)
            if cycle is not None:
                return cycle
        return ()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _closure_has_cycle(self, target: str) -> bool:
        """目标的依赖闭包是否含环。"""
        state: dict[str, int] = {}

        def visit(cid: str) -> bool:
            st = state.get(cid)
            if st == 1:
                return True
            if st == 2:
                return False
            state[cid] = 1
            node = self._nodes.get(cid)
            if node is not None:
                for prereq in node.prerequisites:
                    if visit(prereq):
                        return True
            state[cid] = 2
            return False

        return visit(target)
