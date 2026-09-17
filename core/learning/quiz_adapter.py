"""Quiz → Learning domain adapter (Phase 1D).

Direction of dependency: Learning depends on the existing quiz capability —
``core.video_study`` is NEVER imported by... rather, this module imports it,
and VideoStudy never imports Learning. The existing quiz functions stay
untouched; this adapter only converts their outputs into the frozen
``AssessmentEvidence`` contract.

Concept binding is deterministic (course-scoped normalized-name match).
LLM output never picks a concept, and ambient candidates never enter the
store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.learning.models import AssessmentEvidence, normalize_name
from core.learning.store import LearningStore

DEFAULT_QUIZ_CONFIDENCE = 0.8
DEFAULT_QUIZ_DIFFICULTY = "basic"
# Conservative default when the feedback shape cannot be parsed and no
# explicit score was supplied: recorded as partial, never promotes.
UNPARSED_FEEDBACK_DEFAULT_SCORE = 0.5


@dataclass
class QuizGrading:
    """One grading outcome, ready to become AssessmentEvidence.

    ``score`` may be supplied explicitly by the caller; otherwise it is
    derived deterministically from the frozen feedback section shape via
    :func:`derive_score_from_feedback`.
    """

    question: str
    answer: str
    feedback: str = ""
    score: float | None = None
    confidence: float = DEFAULT_QUIZ_CONFIDENCE
    difficulty: str = DEFAULT_QUIZ_DIFFICULTY
    question_type: str | None = None
    quiz_id: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class ConceptBinding:
    """Result of binding a quiz to a course concept.

    status:
      exact        — the requested name matched a stored concept
      unique       — the course has exactly one concept (auto-bound)
      needs_choice — several concepts; the user must pick one by name
      candidate    — no matching concept: stays a candidate, NEVER stored
    """

    status: str
    concept_id: str | None = None
    concept_name: str | None = None
    choices: tuple = ()  # ((concept_id, name), ...)


# ---------------------------------------------------------------------------
# score derivation (deterministic, no LLM at this layer)
# ---------------------------------------------------------------------------

_SECTION_CORRECT = "正确的部分"
_SECTION_MISSING = "缺失的部分"
_SECTION_WRONG = "错误理解"


def _section(feedback: str, marker: str, stop_markers: tuple[str, ...]) -> str:
    index = feedback.find(marker)
    if index < 0:
        return ""
    rest = feedback[index + len(marker):]
    for stop in stop_markers:
        stop_index = rest.find(stop)
        if stop_index >= 0:
            rest = rest[:stop_index]
    # Strip the enumerations the grading prompt uses ("1." "2." ...).
    return rest.strip(" ：:。\n\t 0123456789.、-")


def _has_content(section_text: str) -> bool:
    """A section counts only when it carries real content; the grading
    prompt's '无' / '没有' placeholders mean the section is empty."""
    text = (section_text or "").strip()
    if not text:
        return False
    return text not in ("无", "没有", "暂无", "无。", "没有。")


def derive_score_from_feedback(feedback: str) -> float | None:
    """Map the frozen three-section grading feedback to a normalized score.

    - 错误理解 section non-empty  -> 0.3 (misunderstanding present)
    - 缺失的部分 section non-empty -> 0.7 (incomplete but on track)
    - only 正确的部分             -> 0.95
    - shape not recognized        -> None (caller decides; default 0.5)
    """
    feedback = feedback or ""
    correct = _section(feedback, _SECTION_CORRECT, (_SECTION_MISSING, _SECTION_WRONG))
    missing = _section(feedback, _SECTION_MISSING, (_SECTION_WRONG,))
    wrong = _section(feedback, _SECTION_WRONG, ())
    if _has_content(wrong):
        return 0.3
    if _has_content(missing):
        return 0.7
    if _has_content(correct):
        return 0.95
    return None


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


def quiz_grading_to_evidence(
    grading: QuizGrading,
    concept_id: str,
    session_id: str | None = None,
) -> AssessmentEvidence:
    """Frozen mapping: QuizGrading -> AssessmentEvidence."""
    score = grading.score
    score_source = "explicit"
    if score is None:
        score = derive_score_from_feedback(grading.feedback)
        score_source = "feedback_sections"
    if score is None:
        score = UNPARSED_FEEDBACK_DEFAULT_SCORE
        score_source = "unparsed_default"
    return AssessmentEvidence(
        concept_id=concept_id,
        source="quiz",
        score=float(score),
        confidence=float(grading.confidence),
        difficulty=grading.difficulty,
        created_at=grading.created_at or "",
        session_id=session_id,
        question_type=grading.question_type,
        metadata={
            "quiz_id": grading.quiz_id,
            "question": grading.question,
            "answer": grading.answer,
            "difficulty": grading.difficulty,
            "score_source": score_source,
            "feedback": grading.feedback,
        },
    )


# ---------------------------------------------------------------------------
# concept binding (deterministic; course-scoped)
# ---------------------------------------------------------------------------


def bind_quiz_to_concept(
    store: LearningStore,
    course_id: str,
    requested_name: str | None = None,
) -> ConceptBinding:
    """Bind a quiz to a concept of the course — deterministic only.

    - requested name given: exact normalized match -> "exact"; no match ->
      "candidate" (NEVER auto-create, NEVER fuzzy-merge).
    - no name: one concept -> "unique" (auto-bind); several -> "needs_choice"
      (user picks); none -> "candidate".
    """
    concepts = store.list_concepts(course_id)
    if requested_name:
        normalized = normalize_name(requested_name)
        for concept in concepts:
            if concept.normalized_name == normalized or normalized in {
                normalize_name(alias) for alias in concept.aliases
            }:
                return ConceptBinding("exact", concept.id, concept.canonical_name)
        return ConceptBinding("candidate")
    if len(concepts) == 1:
        return ConceptBinding("unique", concepts[0].id, concepts[0].canonical_name)
    if len(concepts) > 1:
        return ConceptBinding(
            "needs_choice",
            choices=tuple((c.id, c.canonical_name) for c in concepts),
        )
    return ConceptBinding("candidate")


# ---------------------------------------------------------------------------
# course-grounded generation / grading (existing router, no new provider)
# ---------------------------------------------------------------------------


def generate_concept_question(concept_name: str, course_name: str) -> str:
    """One concept-grounded comprehension question via the existing
    ai_router chain. Mirrors VideoStudy's generation shape for courses."""
    from core.ai_router import chat as ai_chat

    prompt = (
        f"课程：{course_name}\n当前知识点：{concept_name}\n\n"
        "请出 1 道检验学习者是否理解这个知识点的练习题：\n"
        "1. 问题要具体、围绕该知识点；\n"
        "2. 只出问题，不要给出答案；\n"
        "3. 用中文，像朋友聊天一样提问。"
    )
    completion = ai_chat(
        [{"role": "system", "content": "你是 Firefly 的学习陪伴助手，负责出题。"},
         {"role": "user", "content": prompt}],
    )
    choices = completion.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    return str(message.get("content") or "").strip()


def grade_concept_answer(concept_name: str, course_name: str,
                         question: str, answer: str) -> QuizGrading:
    """Grade via the existing ai_router chain using the frozen three-section
    feedback shape（正确的部分 / 缺失的部分 / 错误理解），so the score can be
    derived deterministically."""
    from core.ai_router import chat as ai_chat

    prompt = (
        f"课程：{course_name}\n当前知识点：{concept_name}\n\n"
        f"你出的题目：\n{question}\n\n"
        f"学习者的回答：\n{answer}\n\n"
        "请判卷并按以下固定结构回复：\n"
        "正确的部分：（答对的要点；没有就写 无）\n"
        "缺失的部分：（应答到但没答到的要点；没有就写 无）\n"
        "错误理解：（理解错误之处；没有就写 无）\n"
        "最后用一句轻松鼓励的话收尾。不要编造。"
    )
    completion = ai_chat(
        [{"role": "system", "content": "你是 Firefly 的学习陪伴助手，正在给学习者判卷。"},
         {"role": "user", "content": prompt}],
    )
    choices = completion.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    feedback = str(message.get("content") or "").strip()
    return QuizGrading(
        question=question, answer=answer, feedback=feedback,
        confidence=DEFAULT_QUIZ_CONFIDENCE, difficulty=DEFAULT_QUIZ_DIFFICULTY,
    )
