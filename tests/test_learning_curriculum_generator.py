# -*- coding: utf-8 -*-
"""M2.3 Document Curriculum Generator 验收测试。

覆盖：简单教材生成 / 多章节生成 / 图片引用 / 公式引用 / 空文档 /
非法输入 / JSON 序列化 / 与已有 build_draft 兼容。
隔离：全部在 tmp 目录; 不落库、不激活课程、不改 KnowledgeGraph;
无 LLM、无 Docling/MinerU、无 UI。
"""

from __future__ import annotations

import json

import pytest

from core.learning.curriculum.adapter.draft import build_draft
from core.learning.curriculum.models import DraftStatus
from core.learning.curriculum_generator import (
    ERR_EMPTY_DOCUMENT,
    ERR_INVALID_INPUT,
    ERR_MISSING_COURSE_ID,
    CurriculumGenerationResult,
    CurriculumGenerator,
)
from core.learning.document import DocumentPackage
from core.learning.extractor import ExtractedDocument, NativeExtractor

MD_TEXTBOOK = """# 电磁场导论

引言正文。

## 均匀平面波

平面波正文。

$$E(z,t) = E_0 \\cos(kz - \\omega t)$$

![平面波示意](figs/wave.png)

### TE 极化

TE 正文，行内公式 $k = 2\\pi/\\lambda$。

## 空节

# 天线基础

天线正文。
"""


@pytest.fixture(scope="module")
def textbook_dir(tmp_path_factory):
    base = tmp_path_factory.mktemp("m23_textbook")
    (base / "figs").mkdir()
    (base / "figs" / "wave.png").write_bytes(b"\x89PNG fake")
    (base / "textbook.md").write_text(MD_TEXTBOOK, encoding="utf-8")
    return base


@pytest.fixture(scope="module")
def extracted(textbook_dir):
    result = NativeExtractor().extract(textbook_dir / "textbook.md")
    assert result.success is True
    return result.document_structure


@pytest.fixture(scope="module")
def generator():
    return CurriculumGenerator()


# ---------------------------------------------------------------------------
# 1. 简单教材生成
# ---------------------------------------------------------------------------

def test_simple_textbook_generation(generator, extracted):
    result = generator.generate(extracted, course_id="course-em")
    assert isinstance(result, CurriculumGenerationResult)
    assert result.success is True
    assert result.error == ""
    draft = result.curriculum_draft
    assert draft is not None
    assert draft.course_id == "course-em"
    assert draft.is_confirmed is False                      # 不自动激活
    assert draft.status == DraftStatus.DRAFT.value
    assert len(result.generated_sections) >= 1


# ---------------------------------------------------------------------------
# 2. 多章节生成（heading hierarchy → lesson hierarchy）
# ---------------------------------------------------------------------------

def test_multi_chapter_generation(generator, extracted):
    result = generator.generate(extracted, course_id="course-em")
    sections = result.generated_sections
    by_title = {s.title: s for s in sections}

    # 层级编号: 章=1/2, 节=1.1/1.2, 小节=1.1.1
    assert by_title["电磁场导论"].lesson_path == "1"
    assert by_title["均匀平面波"].lesson_path == "1.1"
    assert by_title["TE 极化"].lesson_path == "1.1.1"
    assert by_title["天线基础"].lesson_path == "2"
    # 章归属（规则 1）
    assert by_title["均匀平面波"].chapter_title == "电磁场导论"
    assert by_title["TE 极化"].chapter_title == "电磁场导论"
    assert by_title["天线基础"].chapter_title == "天线基础"
    # 空节跳过（规则 5）: "空节" 不在教学化记录中, 但出现在警告里
    assert "空节" not in by_title
    assert any("空节" in w for w in result.warnings)
    # 课程草案仍保留全部结构（结构保真与教学化分离）:
    # "空节" 是 level-2 节 → 既有 build_draft 将其收入所属章的 description digest
    draft_titles = [c.title for c in draft_chapters(result)]
    assert draft_titles == ["电磁场导论", "天线基础"]
    assert "空节" in draft_chapters(result)[0].description


def draft_chapters(result):
    chapters = result.curriculum_draft.chapters
    return sorted(chapters, key=lambda c: (c.position, c.id))


# ---------------------------------------------------------------------------
# 3. 图片引用（有包 → attachment_ids; 无包 → 仅引用）
# ---------------------------------------------------------------------------

def test_image_reference_with_package(generator, extracted, textbook_dir):
    package = DocumentPackage(textbook_dir)
    result = generator.generate(extracted, course_id="course-em", package=package)
    wave = next(s for s in result.generated_sections if s.title == "均匀平面波")
    assert wave.image_refs == ("figs/wave.png",)
    assert len(wave.attachment_ids) == 1                    # 包解析成功
    assert wave.attachment_ids[0] in {a.attachment_id for a in package.attachments}


def test_image_reference_without_package(generator, extracted):
    result = generator.generate(extracted, course_id="course-em")
    wave = next(s for s in result.generated_sections if s.title == "均匀平面波")
    assert wave.image_refs == ("figs/wave.png",)
    assert wave.attachment_ids == ()                        # 无包 → 仅引用


def test_missing_attachment_warns_not_fails(generator, extracted, tmp_path):
    base = tmp_path / "t"
    base.mkdir()
    (base / "textbook.md").write_text("# 章\n\n![缺失](figs/none.png)\n", encoding="utf-8")
    extracted2 = NativeExtractor().extract(base / "textbook.md").document_structure
    result = generator.generate(
        extracted2, course_id="c", package=DocumentPackage(base)
    )
    assert result.success is True                           # 附件失败不阻断生成
    assert any("attachment failed" in w for w in result.warnings)
    assert result.generated_sections[0].attachment_ids == ()


# ---------------------------------------------------------------------------
# 4. 公式引用（FormulaBlock → lesson metadata）
# ---------------------------------------------------------------------------

def test_formula_metadata(generator, extracted):
    result = generator.generate(extracted, course_id="course-em")
    by_title = {s.title: s for s in result.generated_sections}
    wave_formulas = by_title["均匀平面波"].formulas
    assert wave_formulas == (
        {"latex": "E(z,t) = E_0 \\cos(kz - \\omega t)", "display": True},
    )
    te_formulas = by_title["TE 极化"].formulas
    assert te_formulas == ({"latex": "k = 2\\pi/\\lambda", "display": False},)


# ---------------------------------------------------------------------------
# 5. 空文档 / 6. 非法输入（错误即结果）
# ---------------------------------------------------------------------------

def test_empty_document_fails(generator):
    empty = ExtractedDocument(title="空", source_path="x.md", sections=())
    result = generator.generate(empty, course_id="c")
    assert result.success is False
    assert result.error.startswith(ERR_EMPTY_DOCUMENT)
    assert result.curriculum_draft is None


def test_invalid_input_fails(generator):
    for bad in (None, "not-doc", 42):
        result = generator.generate(bad, course_id="c")
        assert result.success is False
        assert result.error.startswith(ERR_INVALID_INPUT)
        assert result.curriculum_draft is None


def test_missing_course_id_fails(generator, extracted):
    result = generator.generate(extracted, course_id="")
    assert result.success is False
    assert result.error.startswith(ERR_MISSING_COURSE_ID)
    result = generator.generate(extracted, course_id="   ")
    assert result.error.startswith(ERR_MISSING_COURSE_ID)


# ---------------------------------------------------------------------------
# 7. JSON 序列化
# ---------------------------------------------------------------------------

def test_json_serialization(generator, extracted, textbook_dir):
    result = generator.generate(
        extracted, course_id="course-em", package=DocumentPackage(textbook_dir)
    )
    d = result.to_dict()
    text = json.dumps(d, ensure_ascii=False)               # 全 JSON 原生类型
    parsed = json.loads(text)
    assert parsed["success"] is True
    assert parsed["curriculum_draft"]["course_id"] == "course-em"
    assert parsed["curriculum_draft"]["is_confirmed"] is False
    assert parsed["curriculum_draft"]["chapter_count"] == len(draft_chapters(result))
    paths = [s["lesson_path"] for s in parsed["generated_sections"]]
    assert "1.1.1" in paths
    formulas = next(
        s["formulas"] for s in parsed["generated_sections"] if s["title"] == "均匀平面波"
    )
    assert formulas and formulas[0]["display"] is True

    failed = json.loads(
        CurriculumGenerationResult(success=False, error=f"{ERR_EMPTY_DOCUMENT}: x").to_json()
    )
    assert failed["curriculum_draft"] is None


# ---------------------------------------------------------------------------
# 8. 与已有 build_draft 兼容（只读包装等价性）
# ---------------------------------------------------------------------------

def test_compatible_with_existing_build_draft(generator, extracted, textbook_dir):
    result = generator.generate(extracted, course_id="course-em")
    from core.learning.extractor import to_document_structure

    structure = to_document_structure(extracted)
    reference = build_draft(structure, "course-em")

    mine = result.curriculum_draft
    # 章脊柱等价（同一既有管道产出）
    assert [c.title for c in draft_chapters(result)] == [
        c.title for c in sorted(reference.chapters, key=lambda c: (c.position, c.id))
    ]
    assert mine.course_id == reference.course_id == "course-em"
    # 同为未确认草案——生成器没有获得任何激活特权
    assert mine.is_confirmed is False and reference.is_confirmed is False
    assert mine.is_consumable is False


def test_generator_never_touches_store():
    """纯内存契约：generator 模块不 import 课程存储/文件写入（静态断言）。"""
    import core.learning.curriculum_generator.generator as generator_mod
    import inspect

    source = inspect.getsource(generator_mod)
    assert "curriculum.store" not in source
    assert "Store(" not in source
    assert ".save(" not in source
    assert "open(" not in source
