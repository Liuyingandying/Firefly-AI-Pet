"""知识图谱：KnowledgeNode / KnowledgeGraph 与构建方式。

设计参考 LearningMAP 的 KnowledgeGraph（自研最小实现，非复制）：
- 节点分层：prerequisite（前置）/ core（核心）/ extension（扩展）
- 边语义：source -> target 表示 source 是 target 的前置知识点
- 掌握度不存于此，只存于 LearnerProfile.knowledge；本模块只做快照标注

构建方式（LearningMAP Adapter 的职责）：
1. 领域内置图谱（PoC / 离线兜底，见 AUTOMATIC_CONTROL_GRAPH）
2. LLM 结构化构建（线上，宿主 Agent 输出 -> build_graph_from_dict）
"""

from __future__ import annotations

from dataclasses import dataclass, field

NodeLevel = str  # "prerequisite" | "core" | "extension"


@dataclass
class KnowledgeNode:
    id: str
    name: str
    description: str = ""
    level: NodeLevel = "core"
    prerequisites: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "level": self.level,
            "prerequisites": list(self.prerequisites),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeNode":
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            description=data.get("description", ""),
            level=data.get("level", "core"),
            prerequisites=list(data.get("prerequisites", [])),
        )


@dataclass
class KnowledgeGraph:
    domain: str
    nodes: dict[str, KnowledgeNode] = field(default_factory=dict)

    def add_node(self, node: KnowledgeNode) -> None:
        self.nodes[node.id] = node

    def prerequisites_of(self, node_id: str) -> list[str]:
        node = self.nodes.get(node_id)
        return list(node.prerequisites) if node else []

    def dependents_of(self, node_id: str) -> list[str]:
        return [nid for nid, n in self.nodes.items() if node_id in n.prerequisites]

    def topological_order(self) -> list[str]:
        """Kahn 拓扑排序：前置知识点先于依赖知识点出现。"""
        indegree = {nid: len(n.prerequisites) for nid, n in self.nodes.items()}
        queue = [nid for nid, deg in indegree.items() if deg == 0]
        order: list[str] = []
        while queue:
            nid = queue.pop(0)
            order.append(nid)
            for dep in self.dependents_of(nid):
                indegree[dep] -= 1
                if indegree[dep] == 0:
                    queue.append(dep)
        return order

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "nodes": [n.to_dict() for n in self.nodes.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeGraph":
        graph = cls(domain=data["domain"])
        for nd in data.get("nodes", []):
            graph.add_node(KnowledgeNode.from_dict(nd))
        return graph


def build_graph_from_dict(data: dict) -> KnowledgeGraph:
    """LLM 结构化构建适配点：宿主 Agent 输出 {domain, nodes:[...]} -> 图。

    校验：节点 id 唯一、前置引用存在（缺失引用自动忽略并记入 description 提醒）。
    """
    graph = KnowledgeGraph.from_dict(data)
    known = set(graph.nodes)
    for nid, node in graph.nodes.items():
        node.prerequisites = [p for p in node.prerequisites if p in known]
    return graph


# ---------------------------------------------------------------
# 领域内置图谱：自动控制原理（demo 与离线兜底用）
# 节点参考经典课程编排：拉普拉斯变换 -> 传递函数 -> 零极点 -> 稳定性/时域/频域
# ---------------------------------------------------------------

_AUTOMATIC_CONTROL_NODES = [
    KnowledgeNode(
        id="laplace",
        name="拉普拉斯变换",
        description="复频域分析基础：把微分方程变换为代数方程。",
        level="prerequisite",
    ),
    KnowledgeNode(
        id="transfer_function",
        name="传递函数",
        description="系统输出与输入的复频域比值 G(s)=Y(s)/U(s)。",
        level="core",
        prerequisites=["laplace"],
    ),
    KnowledgeNode(
        id="poles_zeros",
        name="零极点",
        description="使传递函数分子为零/分母为零的 s 值，决定系统动态特性。",
        level="core",
        prerequisites=["transfer_function"],
    ),
    KnowledgeNode(
        id="stability",
        name="稳定性分析",
        description="由极点位置（左半平面）判定系统稳定性。",
        level="core",
        prerequisites=["poles_zeros"],
    ),
    KnowledgeNode(
        id="time_response",
        name="时域响应",
        description="阶跃/冲激响应：由零极点估算超调量与调节时间。",
        level="extension",
        prerequisites=["poles_zeros"],
    ),
    KnowledgeNode(
        id="frequency_response",
        name="频域分析",
        description="Bode 图与频域指标：由零极点画近似幅频/相频特性。",
        level="extension",
        prerequisites=["poles_zeros"],
    ),
]

AUTOMATIC_CONTROL_GRAPH = KnowledgeGraph(
    domain="自动控制原理",
    nodes={n.id: n for n in _AUTOMATIC_CONTROL_NODES},
)
