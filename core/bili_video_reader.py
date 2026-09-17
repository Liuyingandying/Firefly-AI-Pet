"""Minimal Bilibili video reading entry for Firefly.

Pipeline: Bilibili URL / BV id -> BiliInsight service (metadata + transcript,
subprocess JSONL, see core.bili_insight_client) -> core.ai_router summary ->
natural-language answer.

Deliberately minimal for the MVP: no Extension API, no Memory, no UI wiring
yet. The callable entry is :func:`analyze_bilibili_video`; a CLI is provided
for manual verification:

    python -m core.bili_video_reader <url-or-bvid> [question...]
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any

from core.bili_insight_client import BiliInsightClient, BiliServiceError

_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# Keep the summary prompt bounded; the middle of a very long transcript is
# dropped first, mirroring the dogfood harness behaviour.
_MAX_TRANSCRIPT_CHARS = 12_000

_SUMMARY_SYSTEM_PROMPT = (
    "你是 Firefly AI 宠物的视频阅读助手。请严格基于给定的视频信息和转录文本作答，"
    "不要编造转录中没有的内容；材料不足以回答的部分要明确说明无法确定。"
)


@dataclass(frozen=True)
class BiliVideoAnalysis:
    """One completed video reading: facts from the service plus the AI answer."""

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "url": self.url,
            "title": self.title,
            "owner": self.owner,
            "duration_formatted": self.duration_formatted,
            "segment_count": self.segment_count,
            "transcript_chars": self.transcript_chars,
            "summary": self.summary,
            "ai_provider": self.ai_provider,
            "tags": list(self.tags),
        }


def extract_bilibili_video_id(text: str) -> str | None:
    """Return the first BV id inside a URL or bare id string, else None."""
    if not text:
        return None
    match = _BV_RE.search(text)
    return match.group() if match else None


def _format_transcript(segments: list[dict], max_chars: int = _MAX_TRANSCRIPT_CHARS) -> str:
    """Render timestamped "[M:SS] text" lines; middle-out truncate when huge."""
    lines = []
    for seg in segments:
        start = float(seg.get("start") or 0)
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{int(start) // 60}:{int(start) % 60:02d}] {text}")
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    half = max_chars // 2
    return joined[:half] + "\n…[中部转录省略]…\n" + joined[-half:]


def _build_summary_question(question: str | None) -> str:
    if question and question.strip():
        return question.strip()
    return (
        "请总结这个视频：1) 一句话说明视频主题；2) 分条列出核心内容/论点"
        "（重要条目标注出现的时间段）；3) 结论或收尾方式。"
    )


def analyze_bilibili_video(
    source: str,
    question: str | None = None,
    *,
    client: BiliInsightClient | None = None,
) -> BiliVideoAnalysis:
    """Fetch metadata + transcript through BiliInsight, summarize via AI Router.

    Raises BiliServiceError on service failure; provider failures from
    core.ai_router propagate unchanged.
    """
    bvid = extract_bilibili_video_id(source)
    if not bvid:
        raise BiliServiceError("invalid_args", f"no BV id found in: {source!r}", "metadata")
    service = client or BiliInsightClient()

    meta = service.metadata(bvid)
    transcript = service.transcribe(bvid)
    segments = transcript.get("segments") or []
    transcript_text = _format_transcript(segments)

    from core.ai_router import chat as ai_chat  # deferred: keeps unit tests provider-free

    prompt = (
        f"视频标题：{meta.get('title', '')}\n"
        f"UP主：{meta.get('owner', '')}\n"
        f"时长：{meta.get('duration_formatted', '')}\n\n"
        f"语音转录（带时间戳）：\n{transcript_text}\n\n"
        f"{_build_summary_question(question)}"
    )
    completion = ai_chat(
        [
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    choices = completion.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    summary = str(message.get("content") or "").strip()

    return BiliVideoAnalysis(
        bvid=bvid,
        url=f"https://www.bilibili.com/video/{bvid}",
        title=str(meta.get("title") or ""),
        owner=str(meta.get("owner") or ""),
        duration_formatted=str(meta.get("duration_formatted") or ""),
        duration_s=float(meta.get("duration") or 0.0),
        segment_count=int(transcript.get("segment_count") or len(segments)),
        transcript_chars=int(transcript.get("chars") or len(transcript_text)),
        summary=summary,
        ai_provider=str(completion.get("provider") or ""),
        tags=list(meta.get("tags") or []),
        segments=tuple((float(s.get("start") or 0.0), str(s.get("text") or ""))
                       for s in segments),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m core.bili_video_reader <url-or-bvid> [question...]")
        return 2
    source = args[0]
    question = " ".join(args[1:]).strip() or None
    try:
        analysis = analyze_bilibili_video(source, question)
    except BiliServiceError as exc:
        print(f"BiliInsight 调用失败: {exc}", file=sys.stderr)
        return 1
    print(
        f"视频: {analysis.title}\n"
        f"UP主: {analysis.owner}  时长: {analysis.duration_formatted}\n"
        f"转录: {analysis.segment_count} 段 / {analysis.transcript_chars} 字"
        f"（引擎: BiliInsight service / faster-whisper）\n"
        f"链接: {analysis.url}\n"
    )
    print(analysis.summary)
    if analysis.ai_provider:
        print(f"\n[AI Router provider: {analysis.ai_provider}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
