# -*- coding: utf-8 -*-
"""中文分句器: 长文本 → 适合逐句合成的句子列表。

策略:
- 句末标点 (。！？!?；;…\n) 强制切分
- 单句超长 (默认 >55 字) 在句中标点 (，、,:：) 处二次切分 (TTS 单句过长易吞字)
- 过短碎片 (<min_merge 字) 并回前句, 保持韵律连续
"""

from __future__ import annotations

import re
from typing import List

SENT_END = re.compile(r"(?<=[。！？!?；;…\n])")
CLAUSE = re.compile(r"(?<=[，、,:：])")


def split_sentences(text: str, max_len: int = 55, min_merge: int = 6) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    raw = [s.strip() for s in SENT_END.split(text)]
    parts: List[str] = []
    for seg in raw:
        if not seg:
            continue
        if len(seg) > max_len:
            buf, chunk = [], ""
            for piece in CLAUSE.split(seg):
                if len(chunk) + len(piece) > max_len and chunk:
                    buf.append(chunk)
                    chunk = piece
                else:
                    chunk += piece
            if chunk:
                buf.append(chunk)
            parts.extend(buf)
        else:
            parts.append(seg)

    # 碎片合并: 太短的句子并回前一句 (避免 TTS 韵律破碎)
    merged: List[str] = []
    for p in parts:
        if merged and len(p) < min_merge:
            merged[-1] += p
        elif merged and len(merged[-1]) < min_merge:
            merged[-1] += p
        else:
            merged.append(p)
    return [p for p in (s.strip() for s in merged) if p]
