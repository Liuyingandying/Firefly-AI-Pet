"""评估器：把作答折算成结构化证据（Evidence）。

PoC 提供确定性判定（字符串归一化比对）保证离线可跑；
线上产品应把判定委托给 Firefly Agent 的 LLM（judge 适配点，同一契约）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .state import Outcome, now_iso

# 判定器签名：输入（用户答案, 期望答案）-> (outcome, misconceptions)
Judge = Callable[[str, str], tuple[Outcome, list[str]]]


@dataclass
class Evidence:
    """一次作答的证据记录（将被 StateEngine 折算进知识状态）。"""

    node_id: str
    outcome: Outcome
    detail: str = ""
    misconceptions: list[str] = field(default_factory=list)
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "outcome": self.outcome,
            "detail": self.detail,
            "misconceptions": list(self.misconceptions),
            "at": self.at,
        }


def _normalize(text: str) -> str:
    """归一化：去首尾空白、去标点、统一小写（中文不受影响）。"""
    text = text.strip().lower()
    return re.sub(r"[\s，。；：、,.!?；!？()（）'\"“”‘’\-_/]+", "", text)


def deterministic_judge(user_answer: str, expected: str) -> tuple[Outcome, list[str]]:
    """确定性判定：精确匹配=正确；包含关键子串=部分；否则错误。"""
    u, e = _normalize(user_answer), _normalize(expected)
    if not u:
        return "incorrect", ["未作答"]
    if u == e:
        return "correct", []
    # 部分：期望答案中的核心词出现
    core_words = [w for w in re.split(r"[\s，。；：、,]+", expected.strip()) if len(w) >= 2]
    if any(w in u for w in core_words):
        return "partial", []
    return "incorrect", [f"对 {expected[:20]} 的理解有偏差"]


class Evaluator:
    """评估器：作答 -> Evidence。"""

    def __init__(self, judge: Judge | None = None):
        self.judge: Judge = judge or deterministic_judge

    def evaluate(
        self,
        node_id: str,
        user_answer: str,
        expected: str,
        detail: str = "",
    ) -> Evidence:
        """评估一次作答，产出证据（供 LearningMemory.apply_evidence 使用）。"""
        outcome, misconceptions = self.judge(user_answer, expected)
        return Evidence(
            node_id=node_id,
            outcome=outcome,
            detail=detail or f"作答: {user_answer[:60]}",
            misconceptions=misconceptions,
        )
