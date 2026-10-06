# -*- coding: utf-8 -*-
"""NativeExtractor — Markdown/纯文本提取器（M2.1, M2.0 native 槽位落地）。

能力（人工预置启发式, 零外部依赖）:
1. Markdown 标题树: ``#``=章(level 1) … ``####``=level 4, 栈式建树;
2. 段落提取: 空行分界, 连续文本行合并为一段（代码围栏内原样保留）;
3. 公式识别: ``$$...$$``（单行/多行 display 块）与行内 ``$...$`` → LaTeX 源;
4. 图片引用: ``![alt](path)`` → ImageBlock（路径原样保留, 附件层负责解析）;
5. 错误即结果: 不存在/不支持格式/非法编码/空文档/IO 故障全部返回失败结果,
   **永不抛出**。

限制（v1, 显式声明）:
- 仅支持 UTF-8（±BOM）; 其他编码 = invalid_encoding 失败;
- 不产出 concept hints / page_range（md 无页概念）; 公式/图片不做语义识别。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from core.learning.extractor.schema import (
    ERR_DECODE,
    ERR_EMPTY,
    ERR_NOT_FOUND,
    ERR_NOT_SUPPORTED,
    ERR_READ,
    EXTRACTOR_NATIVE,
    DocumentExtractionResult,
    ExtractedDocument,
    ExtractedSection,
    FormulaBlock,
    ImageBlock,
    ParagraphBlock,
)

_SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DISPLAY_ONE_LINE_RE = re.compile(r"^\$\$(.+)\$\$$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_INLINE_FORMULA_RE = re.compile(r"\$[^$\n]+\$")

_SYNTHETIC_SECTION_TITLE = "全文"


class _Node:
    """解析期的可变大纲节点（final: freeze 成 ExtractedSection）。"""

    __slots__ = ("section_id", "title", "level", "position", "parent_id",
                 "paragraphs", "formulas", "images")

    def __init__(self, section_id, title, level, position, parent_id):
        self.section_id = section_id
        self.title = title
        self.level = level
        self.position = position
        self.parent_id = parent_id
        self.paragraphs: list[ParagraphBlock] = []
        self.formulas: list[FormulaBlock] = []
        self.images: list[ImageBlock] = []


def _section_id(level: int, title: str, occurrence: int) -> str:
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"sec:{level}:{occurrence}:{digest}"


class NativeExtractor:
    """Markdown/纯文本提取器（native 槽位, 零外部依赖, 永不抛出）。"""

    name = EXTRACTOR_NATIVE

    # -- 协议: 能力探测（只读, 无副作用; 按格式路由, 存在性由 extract 判定） --

    def can_extract(self, source: Path) -> bool:
        return Path(source).suffix.lower() in _SUPPORTED_SUFFIXES

    # -- 协议: 提取（永不抛出） ----------------------------------------------

    def extract(self, source: Path) -> DocumentExtractionResult:
        path = Path(source)

        if not path.exists():
            return self._fail(ERR_NOT_FOUND, str(path))
        if not path.is_file():
            return self._fail(ERR_READ, f"not a file: {path}")
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            return self._fail(ERR_NOT_SUPPORTED, f"{path.suffix!r}（支持: {sorted(_SUPPORTED_SUFFIXES)}）")

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return self._fail(ERR_READ, str(exc))

        # native v1 只支持 UTF-8（±BOM）; 其余编码显式失败而非静默乱码
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return self._fail(ERR_DECODE, f"{len(raw)} bytes not valid UTF-8")

        if not text.strip():
            return self._fail(ERR_EMPTY, "document has no content")

        warnings: list[str] = []
        doc = _parse(text, source_path=str(path), warnings=warnings)
        stats = {
            "bytes": len(raw),
            "sections": len(doc.sections),
            "paragraphs": len(doc.paragraphs),
            "formulas": len(doc.formulas),
            "images": len(doc.images),
            "warnings": len(doc.warnings),
        }
        return DocumentExtractionResult(
            success=True,
            extractor_name=self.name,
            document_structure=doc,
            stats=stats,
        )

    # -- 内部 ----------------------------------------------------------------

    @staticmethod
    def _fail(code: str, detail: str) -> DocumentExtractionResult:
        return DocumentExtractionResult(
            success=False,
            extractor_name=EXTRACTOR_NATIVE,
            error=f"{code}: {detail}",
        )


# ---------------------------------------------------------------------------
# 解析器（模块级纯函数, 只被 NativeExtractor.extract 调用）
# ---------------------------------------------------------------------------

def _parse(text: str, *, source_path: str, warnings: list[str]) -> ExtractedDocument:
    lines = text.splitlines()

    nodes: list[_Node] = []            # 文档顺序
    stack: list[_Node] = []            # 活跃祖先（level 递增）
    sibling_counts: dict[str | None, int] = {}
    occurrence: dict[tuple[int, str], int] = {}
    doc_title: str | None = None

    # 首个标题之前的正文（preamble）, finalize 时并入第一个节（或合成节）
    pre_paragraphs: list[ParagraphBlock] = []
    pre_formulas: list[FormulaBlock] = []
    pre_images: list[ImageBlock] = []

    para_buf: list[str] = []
    formula_buf: list[str] | None = None    # 多行 $$ 块
    fence_open = False

    def current_lists() -> tuple[list, list, list]:
        if stack:
            node = stack[-1]
            return node.paragraphs, node.formulas, node.images
        return pre_paragraphs, pre_formulas, pre_images

    def flush_paragraph() -> None:
        if not para_buf:
            return
        joined = " ".join(part.strip() for part in para_buf).strip()
        para_buf.clear()
        if joined:
            current_lists()[0].append(ParagraphBlock(text=joined))

    def add_formula(latex: str, display: bool) -> None:
        latex = latex.strip()
        if latex:
            current_lists()[1].append(
                FormulaBlock(latex=latex, display=display, section_id=_sid_of_stack())
            )

    def add_image(caption: str, ref: str) -> None:
        ref = ref.strip()
        if not ref:
            warnings.append("image reference with empty path skipped")
            return
        current_lists()[2].append(
            ImageBlock(path=ref, caption=caption.strip(), section_id=_sid_of_stack())
        )

    def _sid_of_stack() -> str:
        return stack[-1].section_id if stack else ""

    def open_heading(level: int, title: str) -> None:
        flush_paragraph()
        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        parent_key = parent.section_id if parent is not None else None
        position = sibling_counts.get(parent_key, 0)
        sibling_counts[parent_key] = position + 1

        occurrence[(level, title)] = occurrence.get((level, title), 0) + 1
        node = _Node(
            section_id=_section_id(level, title, occurrence[(level, title)]),
            title=title,
            level=level,
            position=position,
            parent_id=parent.section_id if parent is not None else None,
        )
        nodes.append(node)
        stack.append(node)

    for raw_line in lines:
        line = raw_line.rstrip("\n")

        # ---- 代码围栏: 开关切换, 围栏内全部按普通文本进段落 -----------------
        if _FENCE_RE.match(line):
            if formula_buf is not None:          # 围栏打断未闭合的 $$ 块
                warnings.append("unclosed $$ formula block closed by code fence")
                formula_buf = None
            flush_paragraph()
            fence_open = not fence_open
            continue

        if fence_open:
            para_buf.append(raw_line)
            continue

        # ---- 多行 $$ 块 ------------------------------------------------------
        if formula_buf is not None:
            if line.strip() == "$$":
                add_formula("\n".join(formula_buf), display=True)
                formula_buf = None
            else:
                formula_buf.append(line)
            continue

        # ---- 标题 -------------------------------------------------------------
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if doc_title is None and level == 1:
                doc_title = title
            open_heading(level, title)
            continue

        # ---- 单行 display 公式 -------------------------------------------------
        display_one = _DISPLAY_ONE_LINE_RE.match(line.strip())
        if display_one:
            flush_paragraph()
            add_formula(display_one.group(1), display=True)
            continue

        stripped = line.strip()
        if stripped == "$$":                     # 多行 display 块开始
            flush_paragraph()
            formula_buf = []
            continue

        if not stripped:
            flush_paragraph()
            continue

        # ---- 图片引用 ----------------------------------------------------------
        image_matches = list(_IMAGE_RE.finditer(stripped))
        if image_matches:
            flush_paragraph()
            for match in image_matches:
                add_image(match.group(1), match.group(2))
            remainder = _IMAGE_RE.sub("", stripped).strip()
            if remainder:                        # 图文同行: 图入 images, 文本仍进段落
                para_buf.append(remainder)
            continue

        # ---- 普通文本行（行内公式随段落保留原文, 同时提取 LaTeX） ----------------
        para_buf.append(stripped)
        for match in _INLINE_FORMULA_RE.finditer(stripped):
            add_formula(match.group(0)[1:-1], display=False)

    # ---- 收尾 ---------------------------------------------------------------

    if formula_buf is not None:
        warnings.append("unclosed $$ formula block at end of document")
    flush_paragraph()

    if nodes:
        # preamble 并入文档第一个节; 块的 section_id 重盖为首节 id
        import dataclasses

        first = nodes[0]
        first.paragraphs[:0] = pre_paragraphs
        first.formulas[:0] = [
            dataclasses.replace(f, section_id=first.section_id) for f in pre_formulas
        ]
        first.images[:0] = [
            dataclasses.replace(i, section_id=first.section_id) for i in pre_images
        ]
    else:
        import dataclasses

        first = _Node(
            section_id=_section_id(1, _SYNTHETIC_SECTION_TITLE, 1),
            title=_SYNTHETIC_SECTION_TITLE,
            level=1,
            position=0,
            parent_id=None,
        )
        first.paragraphs = pre_paragraphs
        first.formulas = [
            dataclasses.replace(f, section_id=first.section_id) for f in pre_formulas
        ]
        first.images = [
            dataclasses.replace(i, section_id=first.section_id) for i in pre_images
        ]
        nodes.append(first)

    if doc_title is None:
        doc_title = Path(source_path).stem or _SYNTHETIC_SECTION_TITLE

    sections = tuple(
        ExtractedSection(
            section_id=node.section_id,
            title=node.title,
            level=node.level,
            position=node.position,
            parent_id=node.parent_id,
            paragraphs=tuple(node.paragraphs),
            formulas=tuple(node.formulas),
            images=tuple(node.images),
        )
        for node in nodes
    )
    return ExtractedDocument(
        title=doc_title,
        source_path=source_path,
        sections=sections,
        warnings=tuple(warnings),
    )
