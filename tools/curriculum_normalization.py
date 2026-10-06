"""Read-only draft curriculum normalization; does not publish or call a builder."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import re


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
_CHAPTER_ID = re.compile(r"ch\d{2,3}\Z", re.ASCII)
_SECTION = re.compile(r"^\s*(\d+)\s*[-–]\s*(\d+)\s+(.+?)\s*$")
_BOOK_CHAPTER = re.compile(r"^\s*第\s*[一二三四五六七八九十百\d]+\s*章")
TITLE_TYPES = frozenset({"CHAPTER_TITLE", "EXERCISE_TITLE", "SECTION_TITLE",
                         "TRUNCATED_TITLE", "NOISE"})


def load_pdf_bookmark_evidence(document_path: str | Path) -> dict:
    """Read the existing PDF outline; no chapter discovery or PDF mutation."""
    from pypdf import PdfReader
    from learning.skill_source.textbook_chapter_policy import bookmark_policy

    evidence = bookmark_policy(document_path)
    reader = PdfReader(str(document_path))
    chapters = []

    def visit(items: list, depth: int = 0) -> None:
        for item in items:
            if isinstance(item, list):
                visit(item, depth + 1)
                continue
            title = str(getattr(item, "title", "")).strip()
            if not _BOOK_CHAPTER.match(title):
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except (AttributeError, KeyError, ValueError):
                continue
            chapters.append({"page": page, "title": title, "depth": depth})

    visit(reader.outline)
    chapters.sort(key=lambda item: item["page"])
    for order, chapter in enumerate(chapters, 1):
        chapter["order"] = order
    if len(chapters) != evidence["chapter_count"]:
        raise ValueError("chapter bookmark evidence count mismatch")
    evidence["chapter_bookmarks"] = chapters
    return evidence


def _plain(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).lower()


def topic_identity(course_id: str, package_path: str | Path, *,
                   chapter_id: str | None = None,
                   draft_root: str | Path | None = None) -> dict:
    """Keep legacy paths untouched; validate new course/chapter/package layout."""
    original_path = str(package_path)
    package = Path(original_path)
    if chapter_id is None:
        return {"topic_id": package.name, "identity_mode": "legacy_slug",
                "package_path": original_path, "chapter_id": None,
                "package_id": None}
    if not _SAFE_ID.fullmatch(course_id) or not _CHAPTER_ID.fullmatch(chapter_id):
        raise ValueError("invalid course_id or chapter_id")
    if draft_root is None:
        raise ValueError("draft_root is required for namespaced packages")
    relative = package.resolve().relative_to(Path(draft_root).resolve())
    if len(relative.parts) < 5 or relative.parts[0:2] != (course_id, chapter_id):
        raise ValueError("package path does not match course/chapter namespace")
    package_id = relative.parts[2]
    if not _SAFE_ID.fullmatch(package_id):
        raise ValueError("invalid package_id")
    # Length prefixes make this tuple encoding unambiguous even if IDs contain underscores.
    topic_id = (f"c{len(course_id)}_{course_id}_h{len(chapter_id)}_{chapter_id}"
                f"_p{len(package_id)}_{package_id}")
    return {"topic_id": topic_id, "identity_mode": "course_chapter_package",
            "package_path": original_path, "chapter_id": chapter_id,
            "package_id": package_id}


def classify_title(original_title: str, start_page: int, concept_title: str,
                   evidence: dict) -> dict:
    """Prefer PDF outline evidence, then numbering, then text/context cues."""
    original = str(original_title or "")
    base = {"original_title": original, "classified_type": "NOISE",
            "decision_reason": "", "display_title": concept_title,
            "title_source": "package_concept_fallback", "requires_manual_review": True,
            "evidence": {"page": start_page}}
    chapter_bookmark = next((item for item in evidence.get("chapter_bookmarks", [])
                             if item["page"] == start_page and _plain(item["title"]) == _plain(original)), None)
    if chapter_bookmark:
        return {**base, "classified_type": "CHAPTER_TITLE",
                "decision_reason": "PDF outline contains a matching chapter bookmark at this page",
                "display_title": chapter_bookmark["title"], "title_source": "pdf_chapter_bookmark",
                "requires_manual_review": False,
                "evidence": {"page": start_page, "bookmark": chapter_bookmark}}
    exercise = next((span for span in evidence.get("exercise_ranges", [])
                     if span["start_page"] <= start_page <= span["end_page"]), None)
    if exercise:
        return {**base, "classified_type": "EXERCISE_TITLE",
                "decision_reason": "PDF outline places this page inside a bookmarked exercise range",
                "evidence": {"page": start_page, "exercise_range": exercise}}
    section = _SECTION.match(original)
    matching_bookmark = next((item for item in evidence.get("section_bookmarks", [])
                              if item["page"] == start_page and section
                              and item["chapter"] == int(section.group(1))
                              and item["section"] == int(section.group(2))), None)
    if matching_bookmark:
        canonical = (f"{matching_bookmark['chapter']}-{matching_bookmark['section']} "
                     f"{matching_bookmark['title']}")
        if _plain(canonical) == _plain(original):
            return {**base, "classified_type": "SECTION_TITLE",
                    "decision_reason": "PDF outline section bookmark matches the source heading and page",
                    "display_title": matching_bookmark["title"],
                    "title_source": "pdf_section_bookmark", "requires_manual_review": False,
                    "evidence": {"page": start_page, "bookmark": matching_bookmark}}
        return {**base, "classified_type": "TRUNCATED_TITLE",
                "decision_reason": "numbered source heading disagrees with the PDF section bookmark",
                "display_title": matching_bookmark["title"], "title_source": "pdf_section_bookmark",
                "evidence": {"page": start_page, "bookmark": matching_bookmark}}
    conflicting_bookmark = next((item for item in evidence.get("section_bookmarks", [])
                                 if item["page"] == start_page), None)
    if conflicting_bookmark and section:
        return {**base, "classified_type": "NOISE",
                "decision_reason": "source section number has no matching bookmark, while the PDF outline names a different section on this page",
                "evidence": {"page": start_page, "conflicting_bookmark": conflicting_bookmark}}
    if _BOOK_CHAPTER.match(original):
        return {**base, "classified_type": "CHAPTER_TITLE",
                "decision_reason": "chapter number pattern found without matching PDF bookmark",
                "display_title": original.strip(), "title_source": "chapter_number_pattern"}
    if section:
        body = section.group(3).strip()
        if (body.startswith(("所示", "图中", "因此")) or
                ("。" in body and len(body) > 25)):
            return {**base, "classified_type": "TRUNCATED_TITLE",
                    "decision_reason": "numbered text starts mid-sentence or contains a sentence fragment; no matching section bookmark",
                    "evidence": {"page": start_page, "numbered_heading": True,
                                 "matching_section_bookmark": False}}
        if ("？" in body or "?" in body or body.startswith(("设", "已知"))
                or body.endswith(("要求：", "要求:"))):
            return {**base, "classified_type": "EXERCISE_TITLE",
                    "decision_reason": "numbered text is phrased as an exercise; no matching section bookmark",
                    "evidence": {"page": start_page, "numbered_heading": True,
                                 "matching_section_bookmark": False}}
        if 2 <= len(body) <= 48:
            return {**base, "classified_type": "SECTION_TITLE",
                    "decision_reason": "section number pattern and plausible title length; no matching PDF bookmark",
                    "display_title": body, "title_source": "section_number_pattern",
                    "requires_manual_review": True,
                    "evidence": {"page": start_page, "numbered_heading": True,
                                 "matching_section_bookmark": False}}
    return {**base, "classified_type": "NOISE",
            "decision_reason": "no chapter/section bookmark or reliable number pattern; preserved for review"}


def normalize_curriculum(v1: dict, *, draft_root: str | Path, evidence: dict) -> dict:
    if v1.get("schema") != "firefly.curriculum.preflight.v1":
        raise ValueError("expected curriculum preflight v1")
    out = deepcopy(v1)
    out["schema"] = "firefly.curriculum.preflight.v2"
    out["status"] = "DRAFT_REVIEW_REQUIRED"
    out["source_v1_draft_id"] = v1["draft_id"]
    out["draft_id"] = v1["draft_id"] + ":v2"
    course_id = out["course"]["course_id"]
    root = Path(draft_root)
    topic_ids = []
    classifications = []
    crosses_chapter = []
    chapter_pages = sorted(item["page"] for item in evidence.get("chapter_bookmarks", []))
    for chapter in out["chapters"]:
        book_number = chapter["order"]
        book_mark = next((item for item in evidence.get("chapter_bookmarks", [])
                          if item["order"] == book_number), None)
        if book_mark:
            chapter["title_classification"] = classify_title(
                book_mark["title"], book_mark["page"], chapter["title"], evidence)
            chapter["title"] = chapter["title_classification"]["display_title"]
        for unit in chapter["units"]:
            package = root / unit["package_path"]
            draft_base = (root / "draft_packages").resolve()
            try:
                draft_parts = package.resolve().relative_to(draft_base).parts
            except ValueError:
                draft_parts = ()
            if len(draft_parts) >= 5:
                if draft_parts[0:2] != (course_id, unit["unit_id"]):
                    raise ValueError(f"package namespace mismatch: {unit['unit_id']}")
                identity = topic_identity(course_id, package, chapter_id=unit["unit_id"],
                                          draft_root=draft_base)
            else:
                identity = topic_identity(course_id, unit["package_path"])
            if not package.is_dir():
                raise ValueError(f"missing package: {unit['unit_id']}")
            unit["topic_id"] = identity["topic_id"]
            unit["package_id"] = identity["package_id"]
            unit["identity_mode"] = identity["identity_mode"]
            unit["concepts"][0]["topic_id"] = identity["topic_id"]
            classification = classify_title(unit["source_heading"], unit["page_range"][0],
                                            unit["concepts"][0]["title"], evidence)
            unit["title_classification"] = classification
            unit["title"] = classification["display_title"]
            topic_ids.append(identity["topic_id"])
            classifications.append(classification["classified_type"])
            crossed = [page for page in chapter_pages
                       if unit["page_range"][0] < page <= unit["page_range"][1]]
            if crossed:
                unit["review_flags"] = ["CROSSES_BOOK_CHAPTER_BOUNDARY"]
                crosses_chapter.append({"unit_id": unit["unit_id"], "chapter_start_pages": crossed})
    if len(topic_ids) != len(set(topic_ids)):
        raise ValueError("topic_id collision after normalization")
    out["normalization"] = {"topic_identity": "length-prefixed course_id/chapter_id/package_id",
                            "topic_count": len(topic_ids), "unique_topic_count": len(set(topic_ids)),
                            "title_type_counts": dict(Counter(classifications)),
                            "cross_chapter_units": crosses_chapter,
                            "source_package_paths_changed": False,
                            "official_builder_compatibility": "UNVERIFIED_BUILDER_STILL_USES_SLUG"}
    return out


def validate_curriculum_v2(draft: dict, *, draft_root: str | Path) -> dict:
    """Validate the isolated v2 structure without invoking the formal builder."""
    errors: list[str] = []
    if draft.get("schema") != "firefly.curriculum.preflight.v2":
        errors.append("schema mismatch")
    course = draft.get("course", {})
    if course.get("registered") is not False:
        errors.append("draft must remain unregistered")
    root = Path(draft_root).resolve()
    package_root = (root / "draft_packages" / str(course.get("course_id", ""))).resolve()
    units = [unit for chapter in draft.get("chapters", []) for unit in chapter.get("units", [])]
    topic_ids = []
    question_ids = []
    for unit in units:
        uid = unit.get("unit_id", "missing-unit")
        topic_ids.append(unit.get("topic_id"))
        title = unit.get("title_classification", {})
        if (title.get("classified_type") not in TITLE_TYPES or
                not title.get("original_title") or not title.get("decision_reason") or
                not isinstance(title.get("evidence"), dict)):
            errors.append(f"{uid}: incomplete title classification")
        try:
            package = (root / unit["package_path"]).resolve()
            if unit.get("identity_mode") != "legacy_slug":
                package.relative_to(package_root)
            if not package.is_dir():
                errors.append(f"{uid}: package missing")
        except (KeyError, ValueError):
            errors.append(f"{uid}: package outside isolated course root")
        concepts = unit.get("concepts", [])
        lessons = unit.get("lessons", [])
        quizzes = unit.get("quizzes", [])
        if len(concepts) != 1 or len(lessons) != 1 or len(quizzes) != 1:
            errors.append(f"{uid}: expected one concept, lesson and quiz")
            continue
        concept_id = concepts[0].get("id")
        if concepts[0].get("topic_id") != unit.get("topic_id"):
            errors.append(f"{uid}: concept topic identity mismatch")
        if lessons[0].get("concept_id") != concept_id or quizzes[0].get("concept_id") != concept_id:
            errors.append(f"{uid}: lesson/quiz concept reference mismatch")
        for question in quizzes[0].get("questions", []):
            question_ids.append(question.get("id"))
            letter = question.get("correct_option", "")
            options = question.get("options", [])
            if (not question.get("question") or not question.get("explanation") or
                    letter not in ("A", "B", "C", "D") or len(options) < 3 or
                    len(options) <= ord(letter) - ord("A") or
                    options[ord(letter) - ord("A")] != question.get("answer")):
                errors.append(f"{uid}: invalid question {question.get('id')}")
    if len(topic_ids) != len(set(topic_ids)) or None in topic_ids:
        errors.append("topic_id collision or missing ID")
    if len(question_ids) != len(set(question_ids)) or None in question_ids:
        errors.append("question ID collision or missing ID")
    stats = draft.get("statistics", {})
    if (stats.get("chapter_units") != len(units) or
            stats.get("concepts") != len(units) or
            stats.get("lessons") != len(units) or
            stats.get("quizzes") != len(units) or
            stats.get("questions") != len(question_ids)):
        errors.append("statistics mismatch")
    return {"status": "success" if not errors else "error", "errors": errors,
            "stats": {"units": len(units), "topics": len(topic_ids),
                      "unique_topics": len(set(topic_ids)), "questions": len(question_ids)}}
