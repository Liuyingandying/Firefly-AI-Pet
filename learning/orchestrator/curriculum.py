"""Join final draft references to their original v2 content, without registration."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CurriculumError(ValueError):
    pass


@dataclass(frozen=True)
class Lesson:
    chapter_id: str
    chapter_title: str
    unit_id: str
    topic_id: str
    lesson_id: str
    concept_id: str
    title: str
    points: tuple[str, ...]
    questions: tuple[dict, ...]


@dataclass(frozen=True)
class DraftCurriculum:
    course_id: str
    title: str
    sha256: str
    lessons: tuple[Lesson, ...]
    review_units: tuple[str, ...]


def configured_draft(runtime_course_id: str) -> Path | None:
    """Explicit course mapping, never fuzzy title matching or directory inference."""
    mapping = ROOT / "learning/courses/control_theory/experience.json"
    if not mapping.exists():
        # A public checkout need not contain a user's imported course assets.
        return None
    config = json.loads(mapping.read_text(encoding="utf-8"))
    if runtime_course_id != config["runtime_course_id"]:
        return None
    path = ROOT / config["curriculum_draft"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != config["sha256"]:
        raise CurriculumError("课程草稿版本已改变，需要重新确认课程映射。")
    return path


def _unique(items, key):
    values = {x[key]: x for x in items}
    if len(values) != len(items):
        raise CurriculumError(f"课程存在重复引用：{key}")
    return values


def load_curriculum(path: Path) -> DraftCurriculum:
    try:
        raw = path.read_bytes()
        final = json.loads(raw)
        # Use the pinned sibling artifact, not an arbitrary path embedded in content.
        v2_raw = (path.parent / "control_theory_curriculum_draft_v2.json").read_bytes()
        if hashlib.sha256(v2_raw).hexdigest() != final["source_v2_sha256"]:
            raise CurriculumError("课程内容版本与目录引用不一致。")
        v2 = json.loads(v2_raw)
        course = final["course"]
        if course["course_id"] != v2["course"]["course_id"] or course.get("registered") is not False:
            raise CurriculumError("课程草稿身份不一致。")
        units = _unique([u for ch in v2["chapters"] for u in ch["units"]], "unit_id")
        topics = _unique(final["topics"], "topic_id")
        review = tuple(u["unit_id"] for u in final["review_units"])
        lessons, seen = [], set()
        chapters = sorted(final["chapters"], key=lambda ch: ch["order"])
        _unique(chapters, "chapter_id")
        for chapter in chapters:
            refs = chapter["sections"] + chapter["exercises"]
            refs.sort(key=lambda ref: units[ref["unit_id"]]["order"])
            for ref in refs:
                uid = ref["unit_id"]
                if uid in review or uid in seen:
                    raise CurriculumError("待审或重复单元不能自动进入章节。")
                seen.add(uid)
                unit = units[uid]
                if unit["topic_id"] != ref["topic_id"] or topics[ref["topic_id"]] != ref:
                    raise CurriculumError("稳定 topic 引用不一致。")
                concept = _unique(unit["concepts"], "id")[ref["concept_id"]]
                lesson = _unique(unit["lessons"], "id")[ref["lesson_id"]]
                quiz = _unique(unit["quizzes"], "id")[ref["quiz_id"]]
                if any(x["concept_id"] != concept["id"] for x in (lesson, quiz)):
                    raise CurriculumError("lesson/quiz concept 引用不一致。")
                points = tuple(p["text"].strip() for p in concept["core_points"])
                questions = quiz["questions"]
                if not points or not all(points) or len(questions) < 2:
                    raise CurriculumError("单元缺少讲解、例题或练习。")
                _unique(questions, "id")
                for q in questions:
                    options = q["options"]
                    if (not q["question"].strip() or not q["explanation"].strip()
                            or not isinstance(options, list) or not 2 <= len(options) <= 26
                            or not all(isinstance(o, str) and o.strip() for o in options)
                            or q["correct_option"] not in tuple(chr(65+i) for i in range(len(options)))
                            or q["answer"] != options[ord(q["correct_option"])-65]):
                        raise CurriculumError("练习字段不完整或答案不一致。")
                lessons.append(Lesson(chapter["chapter_id"], chapter["title"], uid,
                                      ref["topic_id"], lesson["id"], concept["id"],
                                      concept["title"], points, tuple(questions)))
        if not lessons or seen | set(review) != set(units) or len(topics) != len(units):
            raise CurriculumError("课程单元覆盖不完整。")
        return DraftCurriculum(course["course_id"], course["title"],
                               hashlib.sha256(raw).hexdigest(), tuple(lessons), review)
    except (KeyError, TypeError, AttributeError, IndexError, OSError, json.JSONDecodeError) as exc:
        raise CurriculumError("课程草稿无法读取或引用不完整。") from exc
