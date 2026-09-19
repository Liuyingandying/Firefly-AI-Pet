"""学习记忆：学习者画像持久化 + 事件日志 + 证据折算入口。

设计参考 Inno Agent L1 模式（自研实现）：
- profile.json：权威画像快照（原子写：临时文件 + os.replace）
- events.jsonl：追加式事件日志（证据、误解、系统事件），可重放

后端适配：默认文件后端；接入 Firefly 时宿主提供 ExtensionMemory，
只要实现 ExtensionMemoryBackend 协议即可无缝切换。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from ..learner.evaluator import Evidence
from ..learner.profile import LearnerProfile
from ..learner.state import StateEngine, now_iso


class ExtensionMemoryBackend(Protocol):
    """Firefly 宿主 ExtensionMemory 的本地契约镜像（适配点）。"""

    def load(self, name: str) -> str | None: ...
    def save(self, name: str, content: str) -> None: ...
    def append(self, name: str, line: str) -> None: ...


class _FileBackend:
    """默认文件后端：data_dir 下的两个文件。"""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / name

    def load(self, name: str) -> str | None:
        path = self._path(name)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def save(self, name: str, content: str) -> None:
        path = self._path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def append(self, name: str, line: str) -> None:
        with self._path(name).open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")


class LearningMemory:
    """学习记忆门面：画像读写、事件日志、证据折算与落盘。"""

    PROFILE_NAME = "profile.json"
    EVENTS_NAME = "events.jsonl"

    def __init__(
        self,
        data_dir: str | Path,
        backend: ExtensionMemoryBackend | None = None,
        engine: StateEngine | None = None,
    ):
        self.backend = backend or _FileBackend(data_dir)
        self.engine = engine or StateEngine()
        self._profile: LearnerProfile | None = None

    # ---- 画像 ----

    def load_profile(self, learner_id: str = "default", create: bool = True) -> LearnerProfile:
        """加载画像；不存在且 create=True 时新建默认画像（不落盘）。"""
        raw = self.backend.load(self.PROFILE_NAME)
        if raw is not None:
            self._profile = LearnerProfile.from_dict(json.loads(raw))
        elif create:
            self._profile = LearnerProfile(learner_id=learner_id)
        return self._profile

    @property
    def profile(self) -> LearnerProfile:
        if self._profile is None:
            self.load_profile()
        return self._profile

    def save_profile(self) -> Path | None:
        """原子落盘画像；返回文件路径（后端不暴露路径时返回 None）。"""
        self.backend.save(self.PROFILE_NAME, json.dumps(self.profile.to_dict(), ensure_ascii=False, indent=2))
        if isinstance(self.backend, _FileBackend):
            return self.backend._path(self.PROFILE_NAME)
        return None

    # ---- 事件日志 ----

    def append_event(self, event: dict) -> None:
        event.setdefault("at", now_iso())
        self.backend.append(self.EVENTS_NAME, json.dumps(event, ensure_ascii=False))

    def load_events(self) -> list[dict]:
        raw = self.backend.load(self.EVENTS_NAME)
        if not raw:
            return []
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    # ---- 证据折算（学习闭环的"评估 -> 更新"入口）----

    def apply_evidence(self, evidence: Evidence, graph=None) -> dict:
        """把一条作答证据折算进画像并落盘。返回更新摘要。

        graph 可选：提供时用于解析节点显示名，实现跨节点误解闭环
        （误解描述"无法理解零极点"挂在传递函数上，答对零极点后一并解决）。
        """
        profile = self.profile
        state = profile.get_state(evidence.node_id)
        if state.node_id not in profile.knowledge:
            profile.knowledge[evidence.node_id] = state

        before = state.mastery
        self.engine.apply_evidence(state, evidence.outcome, evidence.at)
        profile.set_state(state)

        # 误解生命周期
        node_name = evidence.node_id
        if graph is not None:
            node = graph.nodes.get(evidence.node_id)
            if node is not None:
                node_name = node.name

        if evidence.outcome == "incorrect" and evidence.misconceptions:
            for desc in evidence.misconceptions:
                profile.add_misconception(evidence.node_id, desc, source="evaluation")
        elif evidence.outcome == "correct":
            # 答对目标节点：解决该节点误解 + 描述中提及该节点名的误解（跨节点闭环）
            resolved = profile.resolve_misconceptions(evidence.node_id, node_name=node_name)
            self.append_event(
                {
                    "type": "misconception_resolved",
                    "node_id": evidence.node_id,
                    "count": resolved,
                }
            )

        self.append_event(
            {
                "type": "evidence",
                "node_id": evidence.node_id,
                "outcome": evidence.outcome,
                "mastery_before": round(before, 3),
                "mastery_after": round(state.mastery, 3),
                "misconceptions": evidence.misconceptions,
            }
        )
        saved = self.save_profile()

        return {
            "node_id": evidence.node_id,
            "outcome": evidence.outcome,
            "mastery_before": round(before, 3),
            "mastery_after": round(state.mastery, 3),
            "state": state.to_dict(),
            "saved_to": str(saved) if saved else None,
        }

    # ---- 快照 ----

    def snapshot(self) -> dict:
        return {
            "learner": self.profile.to_dict(),
            "events": self.load_events(),
        }
