# -*- coding: utf-8 -*-
"""ConceptRefiner — ConceptCandidate → ConceptProposal（M2.5）。

确定性概念细化（零 LLM——子话题拆分由调用方显式提供, 未来可接模型后端）::

    ConceptCandidate(M2.4) ──细化──▶ ConceptProposal × (1 + N 子话题)
                              │
                              └─▶ 审阅/确认流程（仍是 proposal, 规则 5）

五条规则（M2.5 验收口径）:
1. **章节标题作为 parent**: parent_concept = candidate.chapter;
   章级候选（name == chapter）无自指父级 → 空串（顶层概念）;
2. **保留原始 candidate 溯源**: source_candidate_id / source_level /
   source_chapter 三字段 + concept_id 沿用候选 id（可回链）;
3. **不删除原概念**: base 提案恒在; 子话题是**增量**细化, 源候选原样回显;
4. **hierarchy refinement**: subtopics 显式拆分 → 子提案
   （parent_concept = 概念自身, prerequisites = 概念自身 id）;
5. **所有输出仍为 proposal**: 零 ConceptNode 构造、零图交互。

置信度传递（不重算）: base 与全部子提案 confidence == candidate.confidence。
"""

from __future__ import annotations

import hashlib

from core.learning.concept_refinement.schema import (
    ERR_INVALID_INPUT,
    ConceptProposal,
    ConceptRefinementResult,
)
from core.learning.knowledge_import.schema import ConceptCandidate

_SUBTOPIC_SUFFIX_LEN = 6


class ConceptRefiner:
    """概念细化器（纯映射 + 显式子话题拆分, 零数值计算）。"""

    def refine(
        self,
        candidate: ConceptCandidate,
        *,
        subtopics: list[str] | tuple[str, ...] = (),
    ) -> ConceptRefinementResult:
        """细化一条候选; 非法输入返回失败结果, 不抛异常。"""
        # ---- 空输入 / 非法输入（错误即结果） ---------------------------------
        if not isinstance(candidate, ConceptCandidate):
            return ConceptRefinementResult(
                success=False,
                error=f"{ERR_INVALID_INPUT}: candidate is not a ConceptCandidate",
            )

        warnings: list[str] = []
        base = self._base_proposal(candidate)

        # ---- 规则 4: hierarchy refinement（显式子话题拆分） -------------------
        proposals: list[ConceptProposal] = [base]
        seen: set[str] = {base.name}
        for raw in subtopics or ():
            if raw is None:                      # 显式 None = 空（防 str(None)）
                warnings.append("empty subtopic skipped")
                continue
            topic = str(raw).strip()
            if not topic:
                warnings.append("empty subtopic skipped")
                continue
            if topic in seen:
                warnings.append(f"duplicate subtopic skipped: {topic}")
                continue
            seen.add(topic)
            proposals.append(self._subtopic_proposal(candidate, base, topic))

        return ConceptRefinementResult(
            success=True,
            proposals=tuple(proposals),
            source_candidate=candidate,          # 规则 3: 原概念原样回显
            warnings=tuple(warnings),
        )

    def refine_all(
        self,
        candidates: list[ConceptCandidate] | tuple[ConceptCandidate, ...],
        *,
        subtopics_by_id: dict[str, list[str]] | None = None,
    ) -> tuple[ConceptRefinementResult, ...]:
        """批量细化（子话题按候选 id 注入; 逐条安全返回, 永不抛异常）。"""
        subtopics_by_id = subtopics_by_id or {}
        return tuple(
            self.refine(candidate, subtopics=subtopics_by_id.get(candidate.concept_id, ()))
            for candidate in candidates or ()
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _base_proposal(candidate: ConceptCandidate) -> ConceptProposal:
        # ---- 规则 1: 章节标题作为 parent（章级候选无自指父级） ----------------
        parent_concept = "" if candidate.name == candidate.chapter else candidate.chapter
        description = (
            f"「{candidate.name}」的教学概念提案"
            f"（所属章：{candidate.chapter}；来源层级 L{candidate.level}）"
        )
        return ConceptProposal(
            concept_id=candidate.concept_id,     # 规则 2: 沿用候选 id（可回链）
            name=candidate.name,
            description=description,
            parent_concept=parent_concept,
            prerequisites=tuple(candidate.prerequisites),
            related_experiments=tuple(candidate.related_experiments),
            confidence=candidate.confidence,     # 置信度传递, 不重算
            source_candidate_id=candidate.concept_id,
            source_level=candidate.level,
            source_chapter=candidate.chapter,
            refinement_kind="base",
        )

    @staticmethod
    def _subtopic_proposal(
        candidate: ConceptCandidate,
        base: ConceptProposal,
        topic: str,
    ) -> ConceptProposal:
        digest = hashlib.sha1(topic.encode("utf-8")).hexdigest()[:_SUBTOPIC_SUFFIX_LEN]
        return ConceptProposal(
            concept_id=f"{base.concept_id}:sub-{digest}",
            name=topic,
            description=(
                f"「{base.name}」的细化子概念：{topic}"
                f"（所属章：{candidate.chapter}）"
            ),
            parent_concept=base.name,            # 层级细化: 概念自身为父
            prerequisites=(base.concept_id,),    # 先学父概念
            related_experiments=tuple(candidate.related_experiments),  # 实验继承
            confidence=candidate.confidence,     # 置信度传递, 不重算
            source_candidate_id=candidate.concept_id,
            source_level=candidate.level,
            source_chapter=candidate.chapter,
            refinement_kind="subtopic",
        )
