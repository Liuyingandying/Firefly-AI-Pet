# -*- coding: utf-8 -*-
"""QuestionGenerator — ConceptNode → LearningQuestion（M4.1）。

确定性模板出题（零 LLM、零随机）::

    KnowledgeGraph.get_concept(concept_id) → ConceptNode
      → 题库（_QUESTION_BANK, 人工预置封闭词表）→ LearningQuestion × N

生成规则:
- 未入库但存在于知识图的概念 → **通用回退题**（简述核心要点, 关键词 =
  概念名）——任何图内概念都有题;
- 不在知识图 → 结构化失败（unknown_concept, 禁止异常）;
- question_id 内容哈希（concept_id|type|index）——同一概念重复生成
  完全一致（确定性验收）。

任务书概念 id 对照（te-mode/tm-mode 为任务书别名, 本图实际 id 为
em-te-polarization / em-tm-polarization——三个种子概念全覆盖）。
"""

from __future__ import annotations

import hashlib

from core.learning.question.schema import (
    DIFFICULTY_EASY,
    DIFFICULTY_MEDIUM,
    ERR_INVALID_INPUT,
    ERR_UNKNOWN_CONCEPT,
    QUESTION_CALCULATION,
    QUESTION_CLOSED_CHOICE,
    QUESTION_SHORT_ANSWER,
    EvaluationRule,
    LearningQuestion,
    QuestionResult,
)
from core.learning.knowledge_graph import DEFAULT_NODES, KnowledgeGraph


class _Template:
    """题库条目（人工预置; frozen 语义由约定保证）。"""

    __slots__ = ("question_type", "difficulty", "question_text",
                 "expected_answer", "keywords")

    def __init__(self, question_type: str, difficulty: str,
                 question_text: str, expected_answer: str, keywords: tuple[str, ...]):
        self.question_type = question_type
        self.difficulty = difficulty
        self.question_text = question_text
        self.expected_answer = expected_answer
        self.keywords = keywords


#: 人工预置题库（concept_id → 模板列表; 与 M1.2 种子图概念对齐）
_QUESTION_BANK: dict[str, tuple[_Template, ...]] = {
    "em-uniform-plane-wave": (
        _Template(
            QUESTION_SHORT_ANSWER, DIFFICULTY_EASY,
            "均匀平面波中，电场E、磁场H和传播方向k之间有什么关系？",
            "三者相互正交",
            ("正交", "垂直"),
        ),
        _Template(
            QUESTION_CALCULATION, DIFFICULTY_MEDIUM,
            "频率 f = 2.4 GHz 的均匀平面波在真空中传播，波长 λ 是多少毫米？"
            "（公式 λ = c/(n·f)，c = 3×10⁸ m/s，答案保留一位小数）",
            "约 124.9 mm",
            ("124.9", "125"),
        ),
        _Template(
            QUESTION_CLOSED_CHOICE, DIFFICULTY_EASY,
            "均匀平面波是横波还是纵波？（A）横波 （B）纵波",
            "横波（A）",
            ("横波", "A"),
        ),
    ),
    "em-te-polarization": (
        _Template(
            QUESTION_SHORT_ANSWER, DIFFICULTY_EASY,
            "TE 极化（横电波）中，电场方向与入射面之间是什么关系？",
            "电场垂直于入射面",
            ("垂直", "正交"),
        ),
        _Template(
            QUESTION_CLOSED_CHOICE, DIFFICULTY_MEDIUM,
            "TE 极化中是否存在平行于入射面的电场分量？（A）存在 （B）不存在",
            "不存在（B）",
            ("不存在", "没有", "B"),
        ),
    ),
    "em-tm-polarization": (
        _Template(
            QUESTION_SHORT_ANSWER, DIFFICULTY_EASY,
            "TM 极化（横磁波）中，磁场方向与入射面之间是什么关系？",
            "磁场垂直于入射面",
            ("垂直", "正交"),
        ),
        _Template(
            QUESTION_CALCULATION, DIFFICULTY_MEDIUM,
            "线极化角 ψ = 0° 时，TM 分析中 Ey 分量的相对幅值是多少（相对 E0）？",
            "0（零）",
            ("0", "零"),
        ),
    ),
}


class QuestionGenerator:
    """学习问题生成器（模板规则, 确定性, 零 LLM）。"""

    def __init__(self, *, graph: KnowledgeGraph | None = None):
        self._graph = graph if graph is not None else KnowledgeGraph(DEFAULT_NODES)

    def generate(self, concept_id: str) -> QuestionResult:
        """为一个概念生成题目; 未知/非法输入返回结构化失败, 永不抛异常。"""
        cid = concept_id if isinstance(concept_id, str) else ""
        cid = cid.strip()
        if not cid:
            return QuestionResult(
                success=False,
                error=f"{ERR_INVALID_INPUT}: concept_id must be a non-empty string",
            )

        node = self._graph.get_concept(cid)
        if node is None:
            return QuestionResult(
                success=False,
                error=f"{ERR_UNKNOWN_CONCEPT}: concept {cid!r} is not in the knowledge graph",
            )

        templates = _QUESTION_BANK.get(cid, ())
        if not templates:
            # 通用回退题: 图内存在但未入库的概念（简述核心要点, 关键词=概念名）
            templates = (_Template(
                QUESTION_SHORT_ANSWER, DIFFICULTY_EASY,
                f"请简述「{node.name}」的核心要点（章节：{node.chapter}）。",
                node.name,
                (node.name,),
            ),)

        questions = tuple(
            LearningQuestion(
                question_id=_question_id(cid, template.question_type, index),
                concept_id=cid,
                question_type=template.question_type,
                difficulty=template.difficulty,
                question_text=template.question_text,
                expected_answer=template.expected_answer,
                evaluation_rule=EvaluationRule(
                    strategy="keyword_any", keywords=template.keywords
                ),
            )
            for index, template in enumerate(templates)
        )
        return QuestionResult(success=True, questions=questions)

    def generate_all(self, concept_ids) -> tuple[QuestionResult, ...]:
        """批量生成（保持输入顺序, 逐条结构化失败, 永不抛异常）。"""
        if concept_ids is None:
            return ()
        if isinstance(concept_ids, str):
            concept_ids = (concept_ids,)
        return tuple(self.generate(cid) for cid in concept_ids or ())


def _question_id(concept_id: str, question_type: str, index: int) -> str:
    digest = hashlib.sha1(
        f"{concept_id}|{question_type}|{index}".encode("utf-8")
    ).hexdigest()[:10]
    return f"q-{digest}"
