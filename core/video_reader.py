"""Video reading service layer for the Firefly chat entry.

Wraps :mod:`core.bili_video_reader` (BiliInsight JSONL subprocess client +
AI Router summary) behind a chat-friendly surface:

    detect_bilibili_reference(text) -> BV id | None
    analyze_video_message(text)     -> VideoReadingResult (natural-language
                                       summary already produced through the
                                       existing core.ai_router providers)

No Extension API, no Memory, no new model: the summary leg is the existing
``core.ai_router.chat`` fallback chain (tju -> zhipu -> deepseek).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.bili_insight_client import BiliServiceError
from core.bili_video_reader import analyze_bilibili_video, extract_bilibili_video_id

_URL_RE = re.compile(r"https?://\S+")

# --- summary markdown -> chat text conversion (presentation only) ----------
# The AI summary arrives as markdown (bold headings, bullets). Chat replies
# should read like a companion, not a report: heading lines become ①②③
# numbered lines, bullets keep "- ", and markdown markers are stripped
# without removing any words.

_MD_HEADING_HASH_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")
_MD_HEADING_BOLD_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*[:：]?\s*$")
_MD_INLINE_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_BULLET_RE = re.compile(r"^\s*[*•·]\s+(.*)$")
_MD_CODE_RE = re.compile(r"`([^`]*)`")
_CIRCLED_DIGITS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮"

# Trigger phrases that carry no question intent; when the message is nothing
# but URL + trigger, the default "summarize this video" prompt is used.
_TRIGGER_PHRASES = (
    "看看这个视频", "看一下这个视频", "看这个视频", "帮我看看这个视频",
    "分析这个视频", "分析一下这个视频", "总结这个视频", "总结一下这个视频",
    "看看这个b站视频", "看这个b站视频", "分析这个b站视频",
    "看看视频", "看下这个视频", "读一下这个视频", "解读这个视频",
    "看看", "看一下", "分析一下", "总结一下",
)

# Short trigger tokens stripped only from the head of the remaining text
# (repeatedly, e.g. "帮我分析一下 …"), never from the middle of a sentence.
_HEAD_TRIGGER_TOKENS = (
    "帮我", "总结一下", "总结", "分析一下", "分析", "解读", "读一下",
    "看一下", "看看", "看下", "看",
)

_MIN_QUESTION_CHARS = 4


@dataclass(frozen=True)
class VideoReadingResult:
    """Structured outcome of one video reading."""

    bvid: str
    url: str
    title: str
    owner: str
    duration_formatted: str
    segment_count: int
    transcript_chars: int
    summary: str
    ai_provider: str = ""
    tags: list[str] = field(default_factory=list)
    segments: tuple = ()  # ((start, text), ...) raw transcript, in-RAM only
    duration_s: float = 0.0

    def to_answer(self) -> str:
        """Render the chat reply: friendly header, chat-style body, light outro."""
        header = (
            f"📺 我看完啦\n\n"
            f"《{self.title}》\n"
            f"UP主：{self.owner}\n"
            f"时长：{self.duration_formatted}"
        )
        body = _summary_to_chat_text(self.summary)
        outro = f"🔗 {self.url}\n想再深入了解的话，我也可以帮你看某个时间点的画面～"
        return f"{header}\n\n{body}\n\n{outro}"


def _strip_inline_markers(text: str) -> str:
    """Remove bold/backtick markers from one fragment, keeping every word."""
    text = _MD_INLINE_BOLD_RE.sub(r"\1", text)
    text = _MD_CODE_RE.sub(r"\1", text)
    return text.strip()


def _summary_to_chat_text(summary: str) -> str:
    """Convert the AI summary markdown into chat-style plain text.

    Conservative line-by-line rewrite: heading lines (``**1) X**``, ``### X``)
    become circled-number lines, bullets keep a plain ``- ``, and inline
    bold/backtick markers are stripped. No words are ever added or removed,
    so the AI's facts pass through untouched.
    """
    if not summary or not summary.strip():
        return "（这次没来得及写总结，再让我看一遍试试？）"
    lines_out: list[str] = []
    heading_index = 0
    for raw in summary.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if lines_out and lines_out[-1] != "":
                lines_out.append("")
            continue

        heading_text: str | None = None
        m = _MD_HEADING_BOLD_RE.match(line)
        if m:
            heading_text = m.group(1)
        else:
            m = _MD_HEADING_HASH_RE.match(line)
            if m:
                heading_text = m.group(1)
        if heading_text is not None:
            heading_text = _strip_inline_markers(heading_text)
            # Drop a leading "1)" / "2." marker; numbering is re-applied.
            heading_text = re.sub(r"^\d+\s*[)、.)]\s*", "", heading_text)
            heading_index += 1
            mark = (_CIRCLED_DIGITS[heading_index - 1]
                    if heading_index <= len(_CIRCLED_DIGITS) else f"{heading_index}.")
            lines_out.append(f"{mark} {heading_text}")
            continue

        m = _MD_BULLET_RE.match(line)
        if m:
            lines_out.append(f"- {_strip_inline_markers(m.group(1))}")
            continue

        lines_out.append(_strip_inline_markers(stripped))
    return "\n".join(lines_out).strip()


def detect_bilibili_reference(text: str) -> str | None:
    """Return the BV id when the message references a Bilibili video."""
    return extract_bilibili_video_id(text or "")


def extract_video_question(text: str, bvid: str) -> str | None:
    """Derive the user's question by stripping URL and trigger phrases.

    Returns None when nothing meaningful remains, so the caller falls back to
    the default "summarize this video" instruction.
    """
    cleaned = _URL_RE.sub(" ", text or "")
    cleaned = cleaned.replace(bvid, " ")
    for phrase in _TRIGGER_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    cleaned = cleaned.strip()
    changed = True
    while changed:  # head-only trigger tokens
        changed = False
        for token in _HEAD_TRIGGER_TOKENS:
            if cleaned.startswith(token):
                cleaned = cleaned[len(token):].lstrip(" ，,")
                changed = True
    cleaned = re.sub(r"[\s，。！？、,!?~～]+$", "", cleaned.strip())
    if len(cleaned) < _MIN_QUESTION_CHARS:
        return None
    return cleaned


def analyze(
    url_or_text: str,
    question: str | None = None,
    *,
    client=None,
) -> VideoReadingResult:
    """Read one Bilibili video: metadata + transcript + AI Router summary."""
    analysis = analyze_bilibili_video(url_or_text, question, client=client)
    return VideoReadingResult(
        bvid=analysis.bvid,
        url=analysis.url,
        title=analysis.title,
        owner=analysis.owner,
        duration_formatted=analysis.duration_formatted,
        segment_count=analysis.segment_count,
        transcript_chars=analysis.transcript_chars,
        summary=analysis.summary,
        ai_provider=analysis.ai_provider,
        tags=list(analysis.tags),
        segments=tuple(analysis.segments),
        duration_s=analysis.duration_s,
    )


def analyze_video_message(text: str, *, client=None) -> VideoReadingResult:
    """Chat-entry wrapper: detect the reference in the message and analyze it."""
    bvid = detect_bilibili_reference(text)
    if not bvid:
        raise BiliServiceError("invalid_args", "message contains no Bilibili video reference",
                               "metadata")
    return analyze(text, extract_video_question(text, bvid), client=client)


def video_reading_failure_reply(exc: Exception) -> str:
    """Convert a reading failure into one user-facing sentence."""
    if isinstance(exc, BiliServiceError):
        reasons = {
            "env_dependency": "视频阅读服务未安装或缺少依赖",
            "timeout": "视频处理超时，稍后再试",
            "bilibili_download": "B站视频获取失败（视频可能不存在或网络受限）",
            "asr": "语音转文字失败",
            "model": "本地语音模型加载失败",
            "invalid_args": "没有识别到有效的 B站视频链接",
        }
        reason = reasons.get(exc.kind, "视频阅读服务出错")
        return f"抱歉，{reason}。（{exc.kind}）"
    if getattr(exc, "failures", None) is not None or "all AI providers failed" in str(exc):
        return "抱歉，视频已读取，但 AI 总结服务暂时全部不可用，请稍后再试。"
    return f"抱歉，视频阅读失败：{type(exc).__name__}。"


# ---------------------------------------------------------------------------
# Session video context: "刚才那个视频里面 …" follow-up support.
# Purely in-RAM for the current conversation — never persisted, no Memory,
# no database. Injected through the existing runtime ``turn_context`` channel
# (the same one Screen Vision uses), so the ordinary chat path is untouched
# when there is no video to refer back to.
# ---------------------------------------------------------------------------

_FOLLOWUP_MARKERS = (
    "刚才", "那个视频", "这个视频", "视频里面", "里面那个",
    "这视频", "上个视频", "刚才那个",
)

_MAX_CONTEXT_TRANSCRIPT_CHARS = 8_000


@dataclass(frozen=True)
class SessionVideoContext:
    """The most recently read video of the current conversation session."""

    bvid: str
    url: str
    title: str
    owner: str
    summary: str
    segments: tuple = ()  # ((start, text), ...)
    transcript_chars: int = 0
    duration_s: float = 0.0

    @classmethod
    def from_result(cls, result: VideoReadingResult) -> "SessionVideoContext":
        return cls(
            bvid=result.bvid,
            url=result.url,
            title=result.title,
            owner=result.owner,
            summary=result.summary,
            segments=tuple(result.segments),
            transcript_chars=result.transcript_chars,
            duration_s=result.duration_s,
        )

    def to_context_block(self) -> str:
        """Render the turn_context system block (transcript capped)."""
        from core.bili_video_reader import _format_transcript

        segments = [{"start": start, "text": text} for start, text in self.segments]
        transcript = _format_transcript(segments, _MAX_CONTEXT_TRANSCRIPT_CHARS)
        return (
            "[当前会话视频上下文——这是你真实读取过的内容]\n"
            "你（流萤）刚才通过视频阅读功能真实分析过下面这个B站视频：提取了完整语音转录，"
            "并亲自写出了总结。用户后续消息里的\"刚才/那个视频\"就是指它。"
            "文字转录就是你\"看\"到的东西——回答视频相关追问时必须基于下面的转录与总结，"
            "不要再说你\"没有看过\"或\"没能看到\"这个视频。\n"
            f"标题：{self.title}（UP主：{self.owner}，{self.bvid}）\n\n"
            f"你写的总结：\n{self.summary}\n\n"
            f"你提取的语音转录（节选）：\n{transcript}\n\n"
            "转录与总结都没有的内容，才明确说明无法确定，不要编造。"
        )


def is_video_followup(text: str) -> bool:
    """True when the message refers back to a previously read video."""
    if not text:
        return False
    return any(marker in text for marker in _FOLLOWUP_MARKERS)


__all__ = [
    "VideoReadingResult",
    "SessionVideoContext",
    "detect_bilibili_reference",
    "extract_video_question",
    "is_video_followup",
    "analyze",
    "analyze_video_message",
    "video_reading_failure_reply",
]
