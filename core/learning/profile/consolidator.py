# -*- coding: utf-8 -*-
"""LearningMemoryConsolidator — 错误模式聚合（M4.9 阶段 3）。

把学习事件中的错误类型聚合为长期弱点描述:
- concept_confusion（≥1 次）→ "概念关系理解不足"
- wrong_formula（≥1 次）→ "公式应用错误"
- 同一概念反复出现同一错误 → "反复出现：{描述}"

不保存具体答案内容（只聚合模式, 零原始答案留存）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: error_type → 人读弱点描述（封闭映射）
_ERROR_TYPE_DESCRIPTIONS: dict[str, str] = {
    "concept_confusion": "概念关系理解不足",
    "wrong_formula": "公式应用错误",
    "missing_concept": "关键概念缺失",
}


@dataclass(frozen=True)
class ConsolidatedWeakness:
    """一条聚合后的长期弱点。"""

    pattern: str            # 人读弱点描述（如 "概念关系理解不足"）
    concept_id: str         # 关联概念
    occurrences: int        # 出现次数
    last_seen: str = ""     # 最近一次时间戳

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "concept_id": self.concept_id,
            "occurrences": self.occurrences,
            "last_seen": self.last_seen,
        }


class LearningMemoryConsolidator:
    """错误模式聚合器（纯映射, 零 LLM）。"""

    @staticmethod
    def consolidate(error_events: list[dict]) -> list[ConsolidatedWeakness]:
        """把错误事件列表聚合为长期弱点描述。

        error_events 每条: {"concept_id": …, "error_type": …, "at": …}
        按出现次数降序; 不保存具体答案内容。
        """
        if not error_events:
            return []
        counter: dict[tuple[str, str], dict] = {}
        for event in error_events:
            if not isinstance(event, dict):
                continue
            etype = str(event.get("error_type", "")).strip()
            cid = str(event.get("concept_id", "")).strip()
            at = str(event.get("at", "") or event.get("timestamp", "")).strip()
            if not etype:
                continue
            key = (etype, cid)
            if key not in counter:
                counter[key] = {"error_type": etype, "concept_id": cid,
                                "count": 0, "last_at": at}
            counter[key]["count"] += 1
            if at > counter[key]["last_at"]:
                counter[key]["last_at"] = at

        results: list[ConsolidatedWeakness] = []
        for (etype, cid), info in sorted(
            counter.items(), key=lambda x: (-x[1]["count"], x[0])
        ):
            desc = _ERROR_TYPE_DESCRIPTIONS.get(etype, etype)
            suffix = f"（反复出现 {info['count']} 次）" if info["count"] >= 2 else ""
            results.append(ConsolidatedWeakness(
                pattern=f"{desc}{suffix}",
                concept_id=cid,
                occurrences=info["count"],
                last_seen=info["last_at"],
            ))
        return results
