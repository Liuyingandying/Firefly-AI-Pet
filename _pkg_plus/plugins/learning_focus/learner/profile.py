"""学习者画像：LearnerProfile 定义与 JSON 序列化。

画像即"学习记忆"的权威状态（参考 Inno Agent L1 模式，自研实现）：
- goals：学习目标列表
- knowledge：node_id -> KnowledgeState（知识状态，权威）
- misconceptions：误解记录（门控"先讲后考"）

掌握度唯一权威来源是本画像；知识图谱上的掌握度只是渲染快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .state import KnowledgeState, Misconception, now_iso


@dataclass
class LearnerProfile:
    learner_id: str
    display_name: str = ""
    goals: list[str] = field(default_factory=list)
    knowledge: dict[str, KnowledgeState] = field(default_factory=dict)
    misconceptions: list[Misconception] = field(default_factory=list)
    preferences: dict = field(default_factory=dict)
    updated_at: str = field(default_factory=now_iso)
    version: int = 1

    # ---- 查询 ----

    def get_state(self, node_id: str) -> KnowledgeState:
        """取节点状态；不存在则返回 unseen 默认态（不写入）。"""
        return self.knowledge.get(node_id) or KnowledgeState(node_id=node_id)

    def unresolved_misconceptions(self, node_id: str | None = None) -> list[Misconception]:
        items = [m for m in self.misconceptions if m.resolved_at is None]
        if node_id is not None:
            items = [m for m in items if m.node_id == node_id]
        return items

    def has_unresolved_misconception(self, node_id: str) -> bool:
        return any(
            m.node_id == node_id and m.resolved_at is None for m in self.misconceptions
        )

    # ---- 变更 ----

    def set_state(self, state: KnowledgeState) -> None:
        self.knowledge[state.node_id] = state
        self.updated_at = now_iso()

    def add_misconception(self, node_id: str, description: str, source: str = "evaluation") -> bool:
        """新增误解（同节点同描述未解决时不重复添加）。返回是否新增。"""
        if self.has_unresolved_misconception(node_id):
            for m in self.misconceptions:
                if m.node_id == node_id and m.resolved_at is None and m.description == description:
                    return False
        self.misconceptions.append(Misconception(node_id=node_id, description=description, source=source))
        self.updated_at = now_iso()
        return True

    def resolve_misconceptions(self, node_id: str, node_name: str | None = None) -> int:
        """标记某节点未解决误解为已解决。

        node_id 精确匹配；node_name 用于跨节点闭环——
        若误解描述中提及该节点名（如"无法理解零极点"挂在传递函数上），一并解决。
        返回解决条数。
        """
        resolved = 0
        for m in self.misconceptions:
            if m.resolved_at is None and (
                m.node_id == node_id
                or (node_name and node_name in m.description)
            ):
                m.resolved_at = now_iso()
                resolved += 1
        if resolved:
            self.updated_at = now_iso()
        return resolved

    # ---- 序列化 ----

    def to_dict(self) -> dict:
        return {
            "learner_id": self.learner_id,
            "display_name": self.display_name,
            "goals": list(self.goals),
            "knowledge": {k: v.to_dict() for k, v in self.knowledge.items()},
            "misconceptions": [m.to_dict() for m in self.misconceptions],
            "preferences": dict(self.preferences),
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LearnerProfile":
        return cls(
            learner_id=data["learner_id"],
            display_name=data.get("display_name", ""),
            goals=data.get("goals", []),
            knowledge={
                k: KnowledgeState.from_dict(v) for k, v in data.get("knowledge", {}).items()
            },
            misconceptions=[
                Misconception.from_dict(m) for m in data.get("misconceptions", [])
            ],
            preferences=data.get("preferences", {}),
            updated_at=data.get("updated_at", now_iso()),
            version=data.get("version", 1),
        )
