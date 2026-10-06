# -*- coding: utf-8 -*-
"""TJULLMLearningBrain — 真实 TJULLM 决策大脑（M4.8 阶段 2）。

流程::

    LearnerState + LearnerProfile
      → build_brain_messages（system 规则 + 画像 + 接地 + 用户输入）
      → TJULLMModel.generate()
      → _extract_json（content → SkillDecision JSON）
      → SkillDecisionValidator（action 白名单/概念词表/置信度）
      → SkillDecision

失败降级: 任何异常/校验失败 → 调用 ``rule_fallback(learner_state)`` 返回
规则决策（M4.7 原规则, 保证学习流程不中断）。
TJULLM 只**选择**动作, 不执行工具——执行仍经 ToolRegistry 白名单。
"""

from __future__ import annotations

import dataclasses
import json

from core.learning.agent.model.schema import ModelRequest
from core.learning.knowledge_graph import KnowledgeGraph
from core.learning.skill.brain.prompt import build_brain_messages
from core.learning.skill.decision import SkillDecisionValidator
from core.learning.skill.schema import LearnerState, SkillDecision


def _extract_json(content: str) -> dict | None:
    text = str(content or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


class TJULLMLearningBrain:
    """TJULLM 学习决策大脑（模型决策 + 规则回退）。"""

    def __init__(
        self,
        *,
        model,                                 # TJULLMModel（M3.3）或任意 LearningModel
        graph: KnowledgeGraph | None = None,
        rule_fallback=None,                    # callable(learner_state) -> (SkillDecision, done)
    ):
        self._model = model
        self._graph = graph if graph is not None else KnowledgeGraph()
        self._rule_fallback = rule_fallback    # callable or None

    def decide(
        self,
        learner_state: LearnerState,
        goal: str,
        *,
        grounding_text: str = "",
        user_query: str = "",
        profile=None,                          # M4.9 LearnerProfile（可选，增强画像）
    ) -> tuple[SkillDecision, bool]:
        """返回 (SkillDecision, done)。任何失败 → 规则回退（不抛异常）。"""
        profile_text = self._render_profile(learner_state)
        if profile is not None:
            try:
                profile_block = profile.render_for_brain()
                if profile_block.strip():
                    profile_text = f"{profile_text}\n\n{profile_block}"
            except Exception:  # noqa: BLE001 - 画像渲染失败静默降级
                pass
        messages = build_brain_messages(
            profile_text=profile_text,
            grounding_text=grounding_text,
            goal=goal,
            user_query=user_query,
        )
        request = ModelRequest(messages=messages)

        try:
            response = self._model.generate(request)
        except Exception as exc:  # noqa: BLE001 - TJULLM 故障降级
            return self._fallback(learner_state, f"brain model error: {exc}")

        payload = _extract_json(response.content)
        if payload is None:
            return self._fallback(learner_state, "brain returned no parsable JSON")

        from core.learning.skill.decision import SkillDecisionValidator

        check = SkillDecisionValidator().validate(
            payload, available_concepts=self._graph.list_concepts()
        )
        if not check.ok or check.decision is None:
            return self._fallback(learner_state, f"brain rejected: {check.error}")

        done_flag = bool(payload.get("done"))
        return check.decision, done_flag

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _fallback(self, learner_state: LearnerState, reason: str):
        """规则回退: 决策由 rule_fallback 产出; 不可用时安全 cold_start。"""
        if callable(self._rule_fallback):
            decision, done = self._rule_fallback(learner_state)
            return decision, done
        return (
            SkillDecision(
                action="cold_start_probe",
                target_concept="em-uniform-plane-wave",
                reason=f"[brain degraded: {reason}] cold_start: 用入门概念探底",
                confidence=1.0,
            ),
            True,
        )

    @staticmethod
    def _render_profile(learner_state: LearnerState) -> str:
        d = learner_state.to_dict()
        lines = ["[Learner State]"]
        lines.append(f"cold_start: {d.get('cold_start', True)}")
        mastery = d.get("concept_mastery", {})
        if mastery:
            lines.append(f"mastery: {', '.join(f'{k}={v}' for k, v in mastery.items())}")
        for key in ("weak_concepts", "review_due", "recent_errors",
                     "active_projects", "learning_goals", "unfinished_sessions"):
            value = d.get(key, [])
            if value:
                lines.append(f"{key}: {', '.join(str(v) for v in value)}")
        return "\n".join(lines)
