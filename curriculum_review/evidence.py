"""Build review evidence from existing drafts and PDF outline, without writes."""

from __future__ import annotations

import json
from pathlib import Path

from tools.curriculum_normalization import load_pdf_bookmark_evidence

from .schema import ReviewCase


REVIEW_IDS = ("ch05", "ch10", "ch17", "ch24", "ch39", "ch52")


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return data


def _short(text: object, limit: int = 200) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit] + ("…" if len(value) > limit else "")


def build_review_cases(final_path: Path, v2_path: Path, pdf_path: Path) -> list[ReviewCase]:
    final = _read_json(final_path)
    v2 = _read_json(v2_path)
    if final.get("status") != "DRAFT_REVIEW_REQUIRED":
        raise ValueError("final draft is not waiting for review")
    final_course = final.get("course", {})
    v2_course = v2.get("course", {})
    if (final_course.get("course_id") != "control_theory" or
            final_course.get("registered") is not False or
            final_course.get("source_run_id") != "bp-b3b2be5267" or
            v2_course.get("course_id") != final_course.get("course_id") or
            v2_course.get("source_run_id") != final_course.get("source_run_id")):
        raise ValueError("draft identity or registration gate changed")
    review = {item["unit_id"]: item for item in final["review_units"]}
    if set(review) != set(REVIEW_IDS):
        raise ValueError("review queue differs from the six expected units")
    units = {item["unit_id"]: item for chapter in v2["chapters"] for item in chapter["units"]}
    outline = load_pdf_bookmark_evidence(pdf_path)
    bookmarks = outline["chapter_bookmarks"]
    cases = []
    for unit_id in REVIEW_IDS:
        item = review[unit_id]
        unit = units[unit_id]
        start, end = unit["page_range"]
        if (item["topic_id"] != unit["topic_id"] or
                item["hierarchy_status"] != "REVIEW_REQUIRED" or
                item["page_range"] != unit["page_range"]):
            raise ValueError(f"review evidence drift: {unit_id}")
        chapters = []
        for index, bookmark in enumerate(bookmarks):
            chapter_start = bookmark["page"]
            chapter_end = (bookmarks[index + 1]["page"] - 1
                           if index + 1 < len(bookmarks) else outline["page_count"])
            if chapter_start <= end and start <= chapter_end:
                chapters.append({**bookmark, "end_page": chapter_end,
                                 "chapter_id": f"book_ch{index + 1:02d}"})
        if [chapter["chapter_id"] for chapter in chapters] != item["book_chapter_ids"]:
            raise ValueError(f"PDF bookmark chapter mismatch: {unit_id}")
        sections = [entry for entry in outline["section_bookmarks"]
                    if start - 12 <= entry["page"] <= end and
                    any(entry["chapter"] == chapter["order"] for chapter in chapters)]
        exercises = [entry for entry in outline["exercise_ranges"]
                     if entry["start_page"] <= end and start <= entry["end_page"]]
        concepts = [{"id": concept["id"], "title": concept["title"],
                     "core_points": [_short(point.get("text"), 180)
                                     for point in concept.get("core_points", [])[:4]]}
                    for concept in unit["concepts"]]
        questions = [{"id": question["id"], "question": _short(question.get("question"), 240),
                      "correct_option": question.get("correct_option"),
                      "explanation": _short(question.get("explanation"), 180)}
                     for quiz in unit["quizzes"] for question in quiz["questions"]]
        if not concepts or not questions:
            raise ValueError(f"review case lacks content: {unit_id}")
        cases.append(ReviewCase(
            course_id=final_course["course_id"], chapter_id=unit_id,
            original_title=unit["title_classification"]["original_title"],
            bookmark_context={"pdf_page_range": [start, end],
                              "chapter_bookmarks": chapters,
                              "nearby_section_bookmarks": sections,
                              "overlapping_exercise_bookmarks": exercises,
                              "candidate_is_bookmark": False},
            current_assignment={"status": item["hierarchy_status"],
                                "book_chapter_ids": item["book_chapter_ids"],
                                "section_number": item["section_number"],
                                "display_title": item["title"],
                                "topic_id": item["topic_id"],
                                "concept_id": item["concept_id"]},
            conflict_type="; ".join(unit.get("review_flags") or [item["classified_type"]]),
            concept_summary=concepts, question_summary=questions,
        ))
    return cases
