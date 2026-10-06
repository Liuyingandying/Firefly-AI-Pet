# -*- coding: utf-8 -*-
"""CurriculumGenerator — DocumentStructure → CurriculumDraft（M2.3）。

教材课程生成层: 只读包装既有课程管道, 零结构改动::

    M2.1 ExtractedDocument ──┐
    M2.2 DocumentPackage(可选) ──▶ CurriculumGenerator ──▶ CurriculumGenerationResult
                                   │  1. normalizer(M2.1) → 课程侧纯大纲（只读）
                                   │  2. build_draft(既有, 禁改) → CurriculumDraft
                                   │     （纯函数: 不落库、不建 Concept、不激活）
                                   └─▶ generated_sections（教学化记录:
                                       层级编号/公式元数据/图片附件引用）

六条规则（M2.3 验收口径）:
1. Section → Course Section: 提取节经既有 build_draft 映射为课程章节;
2. Heading hierarchy → lesson hierarchy: 层级编号 "1"/"1.1"/"1.1.1";
3. FormulaBlock → lesson metadata: latex+display 原样记录;
4. ImageBlock → attachment reference: 引用必有, 附件 id 由可选包解析
   （解析失败只告警, 不阻断生成）;
5. 空章节安全跳过: 无段落/公式/图片的节跳过教学化并告警
   （课程大纲仍保留该节——结构保真与教学化分离）;
6. 错误即结果: 任何失败返回 success=False 的结果, 永不抛异常。

纯内存: 不写数据库、不激活课程（draft.is_confirmed 恒 False）、
不改 KnowledgeGraph; 无 LLM、无 Docling/MinerU、无 UI。
"""

from __future__ import annotations

from core.learning.curriculum.adapter.draft import build_draft
from core.learning.curriculum_generator.schema import (
    ERR_DRAFT_BUILD,
    ERR_EMPTY_DOCUMENT,
    ERR_INVALID_INPUT,
    ERR_INVALID_STRUCTURE,
    ERR_MISSING_COURSE_ID,
    CurriculumGenerationResult,
    GeneratedSection,
)
from core.learning.extractor.normalizer import to_document_structure
from core.learning.extractor.schema import ExtractedDocument


class CurriculumGenerator:
    """教材 → 课程草案生成器（既有管道的只读包装）。"""

    def generate(
        self,
        source: ExtractedDocument,
        *,
        course_id: str,
        package=None,                      # M2.2 DocumentPackage（可选, 鸭子类型）
        author: str = "",
        document_id: str | None = None,
        draft_title: str | None = None,
    ) -> CurriculumGenerationResult:
        """生成课程草案 + 教学化节记录; 永不抛异常, 永不落库/激活。"""
        # ---- 规则 6: 非法输入 ----------------------------------------------
        sections = getattr(source, "sections", None)
        if not isinstance(sections, tuple) and not isinstance(sections, list):
            return self._fail(ERR_INVALID_INPUT, "source is not an extracted document")
        if not sections:
            return self._fail(ERR_EMPTY_DOCUMENT, "extracted document has no sections")

        course = str(course_id or "").strip()
        if not course:
            return self._fail(ERR_MISSING_COURSE_ID, "course_id is required")

        # ---- 规则 1: Section → Course Section（既有管道, 只读） ---------------
        try:
            structure = to_document_structure(
                source, document_id=document_id, author=author
            )
            draft = build_draft(structure, course, title=draft_title)
        except Exception as exc:  # noqa: BLE001 - 错误即结果
            code = ERR_INVALID_STRUCTURE if "structure" in str(type(exc).__name__).lower() else ERR_DRAFT_BUILD
            return self._fail(code, f"{type(exc).__name__}: {exc}")

        # ---- 规则 2-5: 教学化节记录 ------------------------------------------
        generated, warnings = self._build_sections(
            sections=list(sections),
            package=package,
            extractor_warnings=tuple(getattr(source, "warnings", ()) or ()),
        )

        return CurriculumGenerationResult(
            success=True,
            curriculum_draft=draft,
            source_document=source,
            generated_sections=tuple(generated),
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _build_sections(
        self,
        *,
        sections: list,
        package,
        extractor_warnings: tuple[str, ...],
    ) -> tuple[list[GeneratedSection], list[str]]:
        warnings: list[str] = [f"extractor: {w}" for w in extractor_warnings]
        generated: list[GeneratedSection] = []

        counters: dict[int, int] = {}          # level → 当前编号
        chapter_by_root: dict[str, str] = {}   # root section_id → 章 title
        current_chapter = ""
        parents: dict[str, str] = {}

        for node in sections:
            # ---- 规则 5: 空章节安全跳过（大纲保真, 教学化跳过） ----------------
            has_content = bool(
                getattr(node, "paragraphs", ())
                or getattr(node, "formulas", ())
                or getattr(node, "images", ())
            )
            if not has_content:
                warnings.append(f"empty section skipped: {getattr(node, 'title', '?')}")
                continue

            level = int(getattr(node, "level", 1))
            section_id = getattr(node, "section_id", "")
            title = getattr(node, "title", "")
            parent_id = getattr(node, "parent_id", None)

            # ---- 规则 2: 层级编号 -------------------------------------------
            counters[level] = counters.get(level, 0) + 1
            for deeper in [n for n in counters if n > level]:
                counters.pop(deeper)
            lesson_path = ".".join(str(counters[l]) for l in sorted(counters) if l <= level)

            # ---- 规则 1: 章归属 ---------------------------------------------
            if parent_id is None:
                current_chapter = title
            parents[section_id] = parent_id or ""
            chapter_title = self._chapter_title(
                section_id, parents, sections, fallback=current_chapter
            )
            chapter_by_root[section_id] = chapter_title

            # ---- 规则 3: 公式元数据 ------------------------------------------
            formulas = tuple(
                {"latex": str(f.latex), "display": bool(getattr(f, "display", True))}
                for f in getattr(node, "formulas", ())
            )

            # ---- 规则 4: 图片引用 → 附件 -------------------------------------
            image_refs: list[str] = []
            attachment_ids: list[str] = []
            for img in getattr(node, "images", ()):
                ref = str(img.path)
                image_refs.append(ref)
                if package is None:
                    continue
                resolution = package.resolve(ref, caption=getattr(img, "caption", ""))
                if resolution.ok:
                    attachment_ids.append(resolution.attachment.attachment_id)
                else:
                    warnings.append(
                        f"attachment failed ({resolution.error}): {ref}"
                    )

            generated.append(
                GeneratedSection(
                    section_id=section_id,
                    title=title,
                    level=level,
                    lesson_path=lesson_path,
                    chapter_title=chapter_title,
                    paragraph_count=len(getattr(node, "paragraphs", ()) or ()),
                    formulas=formulas,
                    image_refs=tuple(image_refs),
                    attachment_ids=tuple(attachment_ids),
                )
            )

        return generated, warnings

    @staticmethod
    def _chapter_title(
        section_id: str,
        parents: dict[str, str],
        sections: list,
        *,
        fallback: str,
    ) -> str:
        """沿 parent 链回溯最近 level-1 祖先的标题（规则 1 的章归属）。"""
        by_id = {getattr(n, "section_id", ""): n for n in sections}
        current = section_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            node = by_id.get(current)
            if node is None:
                break
            if int(getattr(node, "level", 1)) == 1:
                return getattr(node, "title", "")
            current = getattr(node, "parent_id", None) or ""
        return fallback

    @staticmethod
    def _fail(code: str, detail: str) -> CurriculumGenerationResult:
        return CurriculumGenerationResult(
            success=False,
            error=f"{code}: {detail}",
        )
