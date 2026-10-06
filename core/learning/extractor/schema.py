# -*- coding: utf-8 -*-
"""Extractor schema — 教材提取层的数据形状（M2.1, M2.0 §4 落地）。

设计约束（对齐 M2.0 架构文档）:
- 全部 frozen dataclass, JSON 可序列化（tuple→list 由 to_dict 转换）;
- 提取产物是**内容附件层**（段落/公式/图片）, 与课程侧的纯大纲
  ``DocumentStructure``（curriculum, 禁改）分工——大纲映射见 normalizer;
- 公式只存 LaTeX 源, 图片只存引用路径（附件层负责落盘/解析生命周期）;
- ``error`` 进数据形状: 失败是结果, 不抛异常穿越层边界。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 提取器名封闭词表（M2.0 §3.1）
# ---------------------------------------------------------------------------

EXTRACTOR_NATIVE = "native"

# ---------------------------------------------------------------------------
# 失败码封闭词表（error 字段前缀, 机器可读）
# ---------------------------------------------------------------------------

ERR_NOT_FOUND = "file_not_found"          # 路径不存在
ERR_NOT_SUPPORTED = "format_not_supported"  # 后缀不在能力表
ERR_DECODE = "invalid_encoding"           # 非 UTF-8（native v1 只支持 UTF-8±BOM）
ERR_EMPTY = "empty_document"              # 空文档/纯空白
ERR_READ = "read_error"                   # 其他 IO 故障（权限/目录等）


@dataclass(frozen=True)
class ParagraphBlock:
    """一个段落（阅读顺序的连续非空文本行, 已合并）。"""

    text: str

    def to_dict(self) -> dict:
        return {"text": self.text}


@dataclass(frozen=True)
class FormulaBlock:
    """一个公式（LaTeX 源, 只存源码不做渲染决策）。"""

    latex: str
    display: bool = True        # False = 行内公式
    section_id: str = ""

    def to_dict(self) -> dict:
        return {"latex": self.latex, "display": self.display, "section_id": self.section_id}


@dataclass(frozen=True)
class ImageBlock:
    """一个图片引用（路径原样保留, 附件层负责解析与落盘生命周期）。"""

    path: str
    caption: str = ""
    section_id: str = ""

    def to_dict(self) -> dict:
        return {"path": self.path, "caption": self.caption, "section_id": self.section_id}


@dataclass(frozen=True)
class ExtractedSection:
    """提取层的大纲节点（内容哈希 id, 重提取稳定）。"""

    section_id: str
    title: str
    level: int                      # 1=章 2=节 ...
    position: int                   # 兄弟间顺序
    parent_id: str | None = None
    paragraphs: tuple[ParagraphBlock, ...] = ()
    formulas: tuple[FormulaBlock, ...] = ()
    images: tuple[ImageBlock, ...] = ()

    def to_dict(self) -> dict:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "level": self.level,
            "position": self.position,
            "parent_id": self.parent_id,
            "paragraphs": [p.to_dict() for p in self.paragraphs],
            "formulas": [f.to_dict() for f in self.formulas],
            "images": [i.to_dict() for i in self.images],
        }


@dataclass(frozen=True)
class ExtractedDocument:
    """提取层的富文档（title/sections/paragraphs/formulas/images）。

    ``paragraphs/formulas/images`` 是跨全部 section 的扁平视图（属性计算,
    非存储字段）; ``warnings`` 收集非致命问题（跳过的坏引用等）。
    """

    title: str
    source_path: str
    sections: tuple[ExtractedSection, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def paragraphs(self) -> tuple[ParagraphBlock, ...]:
        return tuple(p for s in self.sections for p in s.paragraphs)

    @property
    def formulas(self) -> tuple[FormulaBlock, ...]:
        return tuple(f for s in self.sections for f in s.formulas)

    @property
    def images(self) -> tuple[ImageBlock, ...]:
        return tuple(i for s in self.sections for i in s.images)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "source_path": self.source_path,
            "sections": [s.to_dict() for s in self.sections],
            "warnings": list(self.warnings),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


@dataclass(frozen=True)
class DocumentExtractionResult:
    """extract() 的唯一返回形状：成功与失败都是结果, 不抛异常。"""

    success: bool
    extractor_name: str = ""
    document_structure: ExtractedDocument | None = None
    stats: dict = field(default_factory=dict)
    error: str = ""                 # 成功为 ""; 失败为 "<错误码>: <详情>"

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "extractor_name": self.extractor_name,
            "document_structure": (
                self.document_structure.to_dict()
                if self.document_structure is not None
                else None
            ),
            "stats": dict(self.stats),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
