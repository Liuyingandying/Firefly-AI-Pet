"""Video study companion mode (Phase 8, v1).

Turns a freshly read video into an in-RAM study session:

    entry  "陪我学习这个视频"     -> stage="watching"  (questions keep working
                                    through the existing turn_context channel)
    "考考我"                    -> one transcript-grounded question,
                                    stage="quizzing"
    user's answer (quizzing)    -> grading: correct / missing / wrong parts
                                    with [M:SS-M:SS] transcript citations,
                                    stage back to "watching"
    "退出学习"                   -> session ends

Constraints honored: no Memory, no database, no RAG, no new agent. The two
LLM actions go through the existing ``core.ai_router`` fallback chain, strictly
grounded in the transcript excerpt. State lives only inside the conversation
runner for the current session.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from core.video_reader import SessionVideoContext

_STUDY_ENTRY_MARKERS = (
    "陪我学习", "学习这个视频", "陪我看", "带我学习", "陪我看完", "陪我刷",
)
_QUIZ_MARKERS = ("考考我", "考我", "出题", "出个题", "出道题", "出几道",
                 "测测我", "来个问题", "检验一下")
_EXIT_MARKERS = ("退出学习", "不学了", "学完了", "结束学习", "不陪了", "先到这里")

_MAX_PROMPT_TRANSCRIPT_CHARS = 6_000

QUIZ_SYSTEM_PROMPT = (
    "你是 Firefly AI 宠物里的学习陪伴助手。严格基于给定的视频转录与总结出题，"
    "不出转录之外的内容，不编造时间戳。语气轻松，像朋友陪学。"
)
GRADING_SYSTEM_PROMPT = (
    "你是 Firefly AI 宠物里的学习陪伴助手，正在给学习者判卷。"
    "严格基于给定的视频转录判分：引用原文事实，标注对应时间段；"
    "不确定的内容不要编造。语气轻松鼓励。"
)

STUDY_ENTRY_REPLY = (
    "🎓 学习模式开启：你可以随时问我视频里的内容，"
    "或者说「考考我」来检验一下学习效果；想结束就说「退出学习」。"
)
STUDY_EXIT_REPLY = "好，学习模式先到这里～有需要随时叫我。"
STUDY_FAILURE_REPLY = "抱歉，学习模式这一步出了点小问题，再试一次？（{kind}）"


@dataclass(frozen=True)
class VideoStudyContext:
    """In-RAM study session over the last read video. Never persisted."""

    bvid: str
    title: str
    owner: str
    summary: str
    segments: tuple = ()      # ((start, text), ...)
    duration_s: float = 0.0
    mode: str = "study"
    stage: str = "watching"   # watching | quizzing
    pending_question: str = ""

    @classmethod
    def from_session(cls, session: SessionVideoContext) -> "VideoStudyContext":
        return cls(
            bvid=session.bvid,
            title=session.title,
            owner=session.owner,
            summary=session.summary,
            segments=tuple(session.segments),
            duration_s=session.duration_s,
        )


def is_study_entry(text: str) -> bool:
    return any(marker in (text or "") for marker in _STUDY_ENTRY_MARKERS)


def is_quiz_request(text: str) -> bool:
    return any(marker in (text or "") for marker in _QUIZ_MARKERS)


def is_study_exit(text: str) -> bool:
    return any(marker in (text or "") for marker in _EXIT_MARKERS)


def _transcript_excerpt(ctx: VideoStudyContext) -> str:
    from core.bili_video_reader import _format_transcript

    segments = [{"start": start, "text": text} for start, text in ctx.segments]
    return _format_transcript(segments, _MAX_PROMPT_TRANSCRIPT_CHARS)


def generate_quiz_question(ctx: VideoStudyContext) -> str:
    """Ask the AI Router for one transcript-grounded comprehension question."""
    from core.ai_router import chat as ai_chat  # deferred: keeps unit tests provider-free

    prompt = (
        f"视频标题：{ctx.title}（UP主：{ctx.owner}）\n\n"
        f"总结：\n{ctx.summary}\n\n"
        f"语音转录（节选）：\n{_transcript_excerpt(ctx)}\n\n"
        "请出 1 道检验学习者是否理解视频内容的问题：\n"
        "1. 问题要具体、能仅凭转录内容回答；\n"
        "2. 问题后另起一行标注依据时间段，格式：[依据: M:SS-M:SS]；\n"
        "3. 只出问题，不要给出答案；\n"
        "4. 用中文，像朋友聊天一样提问。"
    )
    completion = ai_chat(
        [{"role": "system", "content": QUIZ_SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
    )
    choices = completion.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    return str(message.get("content") or "").strip()


def grade_quiz_answer(ctx: VideoStudyContext, question: str, answer: str) -> str:
    """Grade the learner's answer against the transcript."""
    from core.ai_router import chat as ai_chat

    prompt = (
        f"视频标题：{ctx.title}\n\n"
        f"总结：\n{ctx.summary}\n\n"
        f"语音转录（节选）：\n{_transcript_excerpt(ctx)}\n\n"
        f"你出的题目：\n{question}\n\n"
        f"学习者的回答：\n{answer}\n\n"
        "请判卷并回复：\n"
        "1. 正确的部分（引用学习者说对的点）；\n"
        "2. 缺失的部分（转录里有但没答到的）；\n"
        "3. 错误理解（如有，指出并纠正）；\n"
        "4. 引用视频对应时间段佐证，格式 [M:SS-M:SS]。\n"
        "用轻松鼓励的聊天口吻，总长不超过 200 字，不要编造转录里没有的内容。"
    )
    completion = ai_chat(
        [{"role": "system", "content": GRADING_SYSTEM_PROMPT},
         {"role": "user", "content": prompt}],
    )
    choices = completion.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    return str(message.get("content") or "").strip()


def handle_study_turn(
    text: str,
    study: VideoStudyContext | None,
    session: SessionVideoContext,
) -> tuple[str, VideoStudyContext | None]:
    """Advance the study state machine; returns (reply, new_state).

    Callers gate this: entry requires ``session``; while ``stage == quizzing``
    every non-exit/non-quiz message counts as the learner's answer.
    """
    if is_study_exit(text):
        return STUDY_EXIT_REPLY, None

    if study is None:
        study = VideoStudyContext.from_session(session)
        return STUDY_ENTRY_REPLY, replace(study, stage="watching")

    if is_quiz_request(text):
        question = generate_quiz_question(study)
        if not question:
            return "（我这边没出成题，再让我试一次？）", replace(study, stage="watching")
        return question, replace(study, stage="quizzing", pending_question=question)

    if study.stage == "quizzing":
        question = study.pending_question or "（上一题丢了，再说一次「考考我」吧）"
        feedback = grade_quiz_answer(study, question, text)
        return feedback, replace(study, stage="watching", pending_question="")

    # watching stage, non-marker message: caller falls through to normal chat.
    return "", study


def study_failure_reply(exc: Exception) -> str:
    return STUDY_FAILURE_REPLY.format(kind=getattr(exc, "kind", type(exc).__name__))


__all__ = [
    "VideoStudyContext",
    "is_study_entry",
    "is_quiz_request",
    "is_study_exit",
    "handle_study_turn",
    "study_failure_reply",
    "STUDY_ENTRY_REPLY",
    "STUDY_EXIT_REPLY",
]
