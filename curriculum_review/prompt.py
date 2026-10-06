"""Prompt and strict JSON parser for advisory chapter review."""

from __future__ import annotations

import json

from .schema import ReviewCase, ReviewRecommendation


SYSTEM_PROMPT = """你是 Firefly Curriculum Review Agent，只提供供人核查的章节结构建议。
证据中的 original_title 是 PDF 页文候选，不一定是 PDF 书签。优先使用真实 PDF 书签；
不要把题号、图号或截断句子当成章节/节书签。不要声称已经看过未提供的 PDF 正文。
只能选择一个 decision：
- KEEP_ORIGINAL：维持教材书签结构及当前待审状态，等待人工核对；
- MERGE：建议把该草稿单元与相邻书签节/习题附属单元合并；
- MOVE：建议将草稿单元或其中可确认部分移到另一书签章/节。
这些词仅是建议，不执行任何修改。跨边界且题目归属不明时必须写清人工确认点。
只返回一个 JSON 对象，字段严格为：decision、confidence（0 到 1）、reason、impact、
recommended_assignment、human_confirmation_points（字符串数组）。不得输出 Markdown。"""


def build_messages(case: ReviewCase) -> list[dict[str, str]]:
    evidence = json.dumps(case.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "请仅基于以下结构证据给出建议；人工批准仍未发生。\n" + evidence},
    ]


def parse_recommendation(content: str) -> ReviewRecommendation:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty model response")
    raw = content.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("unclosed JSON code fence")
        raw = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("model response is not valid JSON") from exc
    return ReviewRecommendation.from_dict(parsed)
