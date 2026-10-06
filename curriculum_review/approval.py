"""Read-only approval packet from the saved curriculum draft and LLM reviews."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .evidence import REVIEW_IDS
from .schema import ReviewRecommendation


STATUS = "WAITING_HUMAN_APPROVAL"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plain(value: object) -> str:
    """Keep untrusted model prose inside one Markdown paragraph/list item."""
    return re.sub(r"\s+", " ", str(value or "")).replace("`", "\\`").strip()


def load_approval_inputs(final_path: Path, result_path: Path, report_path: Path) -> dict[str, Any]:
    final_hash = sha256(final_path)
    final = json.loads(final_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    report = report_path.read_text(encoding="utf-8")
    if (final.get("status") != "DRAFT_REVIEW_REQUIRED" or
            final.get("course", {}).get("course_id") != "control_theory" or
            final.get("course", {}).get("registered") is not False):
        raise ValueError("final draft is not an unregistered control_theory review draft")
    if (result.get("status") != "REVIEW_DECISION_REQUIRED" or
            result.get("execution_status") != "SIX_RECOMMENDATIONS_READY" or
            result.get("source", {}).get("final_sha256") != final_hash):
        raise ValueError("saved review result or final draft SHA256 mismatch")
    if "SIX_RECOMMENDATIONS_READY" not in report or final_hash not in report:
        raise ValueError("review report does not match saved result and final draft")
    topics = final["topics"]
    topic_ids = [topic["topic_id"] for topic in topics]
    if len(topics) != 56 or len(set(topic_ids)) != 56:
        raise ValueError("final draft topic IDs are not the expected unique set")
    if final.get("builder", {}).get("topic_ids") != topic_ids:
        raise ValueError("final draft builder topic IDs differ from topics")
    review_units = {item["unit_id"]: item for item in final["review_units"]}
    if set(review_units) != set(REVIEW_IDS):
        raise ValueError("final draft review queue changed")
    assigned = {item["unit_id"] for chapter in final["chapters"]
                for item in chapter["sections"] + chapter["exercises"]}
    if assigned & set(review_units) or len(assigned) != 50:
        raise ValueError("review units already assigned or chapter membership changed")
    reviews = result["reviews"]
    if tuple(row.get("chapter_id") for row in reviews) != REVIEW_IDS:
        raise ValueError("saved review order/identity changed")
    cases = []
    for row in reviews:
        unit_id = row["chapter_id"]
        unit = review_units[unit_id]
        evidence = row["input_evidence"]
        if (row.get("status") != "RECOMMENDATION_READY" or
                evidence.get("course_id") != "control_theory" or
                evidence.get("chapter_id") != unit_id or
                evidence.get("original_title") != unit["original_title"] or
                evidence.get("current_assignment", {}).get("topic_id") != unit["topic_id"] or
                evidence.get("current_assignment", {}).get("status") != "REVIEW_REQUIRED" or
                evidence.get("current_assignment", {}).get("book_chapter_ids") != unit["book_chapter_ids"] or
                evidence.get("bookmark_context", {}).get("pdf_page_range") != unit["page_range"] or
                len(evidence.get("concept_summary", [])) != 1 or
                len(evidence.get("question_summary", [])) != 5):
            raise ValueError(f"saved evidence differs from final draft: {unit_id}")
        recommendation = ReviewRecommendation.from_dict(row["recommendation"])
        cases.append({"unit": unit, "evidence": evidence,
                      "recommendation": recommendation.to_dict()})
    return {"final_hash": final_hash, "topic_ids": topic_ids,
            "topic_id_set_hash": hashlib.sha256("\n".join(sorted(topic_ids)).encode("utf-8")).hexdigest(),
            "cases": cases, "assigned_count": len(assigned),
            "review_count": len(review_units), "chapter_count": len(final["chapters"]),
            "source_model": result.get("provider", {}).get("model", "unknown")}


def render_checklist(data: dict[str, Any]) -> str:
    lines = ["# Curriculum Approval Checklist", "",
             f"**状态：{STATUS}。所有批准框均为空；本文件不是批准记录。**", "",
             "依据：`Curriculum_Review_Agent_Result.json`、`Firefly_Curriculum_Review_Agent_Report.md` 与未注册 final draft。",
             "`book_chapter_ids` 仅表示页码重叠，不表示单元已挂载到这些章节。六项均在 `review_units`。", ""]
    lines += ["下列 reason / impact 为模型原文。个别“同时挂入两章”之类表述与 final draft 的实际复核区结构不符，应以当前归属和差异预览为准。", ""]
    for case in data["cases"]:
        unit, evidence, rec = case["unit"], case["evidence"], case["recommendation"]
        question_ids = ", ".join("`" + q["id"] + "`" for q in evidence["question_summary"])
        lines += [f"## {unit['unit_id']}", "",
                  f"- **chapter_id：**`{unit['unit_id']}`（草稿单元 ID）。",
                  f"- **original_title：**{_plain(unit['original_title'])}",
                  f"- **current_assignment：**`{unit['hierarchy_status']}`；PDF {unit['page_range'][0]}–{unit['page_range'][1]} 页；重叠候选 `{', '.join(unit['book_chapter_ids'])}`；`section_number=null`。",
                  f"- **topic_id：**`{unit['topic_id']}`；**concept_id：**`{unit['concept_id']}`；**quiz_id：**`{unit['quiz_id']}`。",
                  f"- **TJU recommendation：**`{rec['decision']}`；建议位置：{_plain(rec['recommended_assignment']) or '未明确'}。",
                  f"- **confidence：**`{rec['confidence']:.2f}`（模型自报，非审核通过率）。",
                  f"- **reason：**{_plain(rec['reason'])}",
                  f"- **impact：**{_plain(rec['impact'])}",
                  f"- **现有 question IDs（5）：**{question_ids}",
                  "- **人工确认：**", "  - [ ] 已核对实际 PDF 书签、页界与原题出处。",
                  "  - [ ] 已逐题确认五道题的教材章/节/习题归属。",
                  "  - [ ] 已确认 concept、topic 与 quiz 引用是否整体保留或需拆分。",
                  "  - [ ] 已填写最终处理方式：接受建议 / 保持待审 / 修改建议（圈选）。",
                  "  - [ ] 若发生拆分，已记录旧 ID 到新 ID 的对应表。",
                  "- **人工填写：**目标书章/节或习题附属单元：________；逐题映射：________；审核人：________；日期：________。", ""]
    lines += ["任何勾选和填写均需由人工完成；生成本清单不触发 apply、注册或 GUI。", ""]
    return "\n".join(lines)


MOVE_PREVIEW = {
    "ch05": ("候选为 `book_ch02.sections`，靠近 PDF 33 页；但原页段 31–41 跨第一章习题/第二章正文。",
             "模型建议整包移动；5 道生成题偏数学模型，但 31–32 页与原始习题标题必须另行处置。"),
    "ch24": ("候选为 `book_ch05` 的频域内容或习题附属单元；PDF 263–265 页落在 `book_ch06`，节/习题类型尚未裁定。",
             "模型建议只移动可确认的第五章部分；必须逐题分配，不能据此整体搬迁 5 道题。"),
    "ch52": ("候选为 `book_ch09.exercises`；PDF 561–564 页落在 `book_ch10`。",
             "模型建议移第九章习题，但现有 TOP-Q05 题干直接涉及最优控制；其章归属需单独核对，不可整包照搬。"),
}


def render_diff_preview(data: dict[str, Any]) -> str:
    lines = ["# Curriculum Approval Diff Preview", "",
             f"**状态：{STATUS}。以下是条件预览，不是 JSON patch，也未执行变更。**", "",
             f"当前 final draft：{data['chapter_count']} 个教材大章，56 个稳定 topic ID；{data['assigned_count']} 个单元在章内，{data['review_count']} 个单元在复核区。六项共 6 个 concept、30 道 question。",
             "`book_chapter_ids` 为页码重叠候选；当前没有把同一复核单元同时挂入两个章节。", "",
             "## 如果接受模型建议", "",
             "三个 `MOVE` 只是方向性建议，尚无可安全执行的逐题映射。三个 `KEEP_ORIGINAL` 意味着维持待审状态，不能解释为批准。", ""]
    for case in data["cases"]:
        unit, evidence, rec = case["unit"], case["evidence"], case["recommendation"]
        uid = unit["unit_id"]
        lines += [f"### {uid} — `{rec['decision']}`", ""]
        if rec["decision"] == "MOVE":
            chapter_change, question_change = MOVE_PREVIEW.get(
                uid, ("目标书章/节尚未确认。", "逐题映射尚未确认，不能整体移动。"))
            lines += [f"- **chapter 结构：**{chapter_change} 只有人工定章、定节/习题类型并处理跨界页段后，才可能从 `review_units` 移入相应章节。",
                      f"- **topic 映射：**整包移动可保留 `{unit['topic_id']}`；若拆分，新增 topic ID、原 ID 去向和别名/历史引用均待人工决定。",
                      f"- **concept 引用：**整包移动可保留 `{unit['concept_id']}`；若拆分，须重审概念与各 topic 的引用关系。",
                      f"- **question 归属：**现有 5 道题引用 `{unit['quiz_id']}`。{question_change}"]
        else:
            lines += ["- **chapter 结构：**无立即变化；仍在 `review_units`，不加入书章 `sections` 或 `exercises`。",
                      f"- **topic 映射：**`{unit['topic_id']}` 保持；节级归属仍未确认。",
                      f"- **concept 引用：**`{unit['concept_id']}` 保持；需要人工核对其是否覆盖多个真实书签节。",
                      f"- **question 归属：**5 道题仍在 `{unit['quiz_id']}`，逐题书签节/习题归属待审。"]
        lines += [f"- **模型建议位置原文：**{_plain(rec['recommended_assignment']) or '未给出'}", ""]
    lines += ["## 如果保持当前原结构", "",
              "10 个大章、56 个 topic ID、50 个章内单元与 6 个复核单元均不变；6 个 concept 和 30 道题继续待审。没有 chapter、topic、concept 或 question 引用变化。此方案保留原 PDF 结构和可追溯性，但六项不能被视为人工批准。", "",
              "## 不能从建议直接生成的差异", "",
              "- `ch05/ch24/ch52` 跨大章：模型没有经人工确认的页段拆分和五道题逐题映射；不得直接生成可执行 patch。",
              "- `ch10/ch17/ch39` 虽建议 `KEEP_ORIGINAL`，其节级归属、展示标题和习题边界仍未裁定。",
              "- `ch52` 的 TOP-Q05 涉及最优控制，与模型提出的第九章整体归属存在内容张力；需核查真实教材来源。",
              "- 若未来拆分单元，新旧 topic/concept/quiz/question ID 的映射、学习路径顺序与历史引用须单独设计并审批。", ""]
    return "\n".join(lines)


def render_packet(data: dict[str, Any], final_path: Path,
                  checklist_path: Path, preview_path: Path) -> str:
    lines = ["# Firefly Control Theory Approval Packet", "",
             f"**状态：{STATUS}。未作人工批准，未执行任何建议。**", "",
             "## Evidence", "",
             f"- final draft：`{final_path}`；SHA256：`{data['final_hash']}`。",
             "- 真实 TJU 审核：`Curriculum_Review_Agent_Result.json`，6/6 项 `RECOMMENDATION_READY`；模型：`" + _plain(data["source_model"]) + "`。",
             f"- 逐项批准清单：`{checklist_path}`。",
             f"- 条件差异预览：`{preview_path}`。", "",
             "## Read-only Integrity Gates", "",
             "| 检查 | 结果 |", "| --- | --- |",
             f"| final draft SHA256 与审核输入一致，生成材料前后保持 | PASS：`{data['final_hash']}` |",
             "| curriculum 注册标志及草稿状态 | PASS：`registered=false`、`DRAFT_REVIEW_REQUIRED`，未写入 curriculum |",
             f"| 56/56 topic ID 唯一、与 builder 列表一致、生成前后集合不变 | PASS：集合 SHA256 `{data['topic_id_set_hash']}` |",
             "| 复核区隔离 | PASS：50 个已归属单元与 6 个复核单元不重叠 |", "",
             "## Decisions For Human Review", "",
             "| 单元 | TJU 建议 | confidence | 人工批准状态 |",
             "| --- | --- | ---: | --- |"]
    for case in data["cases"]:
        unit, rec = case["unit"], case["recommendation"]
        lines.append(f"| `{unit['unit_id']}` | `{rec['decision']}` | {rec['confidence']:.2f} | 未批准 |")
    lines += ["", "## Approval Questions", "",
              "1. 是否逐项确认原 PDF 书签边界、页码与草稿标题的性质？`book_chapter_ids` 目前只是重叠候选。",
              "2. 对三个 `MOVE`，是否明确目标章、节/习题类型和五道题逐题归属？跨章内容是否需拆分？",
              "3. 对三个 `KEEP_ORIGINAL`，是否继续保持待审，或另行提出节级调整？接受该建议本身不等于批准课程。",
              "4. 若涉及拆分或移动，是否明确保留或替换的 topic/concept/quiz/question ID、历史引用及学习路径顺序？",
              "5. `ch52` 的最优控制题是否确属第十章？需与模型的第九章建议分别裁决。", "",
              "**人工决定栏：**批准人：________；日期：________；六项决定及逐题映射附件：________。",
              "", "本材料只供人工审阅；没有 apply decision、课程注册或 GUI 启动。", ""]
    return "\n".join(lines)
