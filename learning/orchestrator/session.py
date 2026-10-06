"""Deterministic presentation activities and a version-bound navigation bookmark.

Draft choice practice uses the supplied answer key. No formal assessment,
mastery, MCP session, course registration, or user answer is written here.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from learning._storage import atomic_write_json
from .curriculum import CurriculumError, DraftCurriculum

PHASES = {"OVERVIEW", "EXPLANATION", "EXAMPLE", "PRACTICE", "FEEDBACK", "COMPLETE"}
LABELS = {"OVERVIEW": "开始讲解", "EXPLANATION": "查看例题", "EXAMPLE": "开始练习",
          "PRACTICE": "请在下方回答", "FEEDBACK": "继续学习", "COMPLETE": "预览已完成"}


@dataclass(frozen=True)
class LearningState:
    course_id: str
    chapter_id: str
    lesson_id: str
    concept_id: str
    progress: float
    mastery: float | None
    next_action: str


class LearningOrchestrator:
    def __init__(self, curriculum: DraftCurriculum, checkpoint: Path):
        self.curriculum, self.checkpoint = curriculum, checkpoint
        self.index, self.phase, self.question_index = 0, "OVERVIEW", 0
        self.feedback = ""
        if checkpoint.exists():
            try:
                saved = json.loads(checkpoint.read_text(encoding="utf-8"))
                if (saved["schema"] != 1 or saved["course_id"] != curriculum.course_id
                        or saved["draft_sha256"] != curriculum.sha256):
                    raise CurriculumError("草稿导航记录版本不匹配，未重置或覆盖。")
                index, phase, qi = saved["lesson_index"], saved["phase"], saved["question_index"]
                if (type(index) is not int or not 0 <= index < len(curriculum.lessons)
                        or phase not in PHASES or type(qi) is not int
                        or not 0 <= qi < len(curriculum.lessons[index].questions)-1
                        or saved["lesson_id"] != curriculum.lessons[index].lesson_id
                        or (phase in {"OVERVIEW", "EXPLANATION", "EXAMPLE"} and qi != 0)
                        or (phase == "COMPLETE" and (index != len(curriculum.lessons)-1
                                                    or qi != len(curriculum.lessons[index].questions)-2))):
                    raise CurriculumError("草稿导航记录损坏，未重置或覆盖。")
                self.index, self.phase, self.question_index = index, phase, qi
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise CurriculumError("无法恢复草稿导航记录；原文件已保留。") from exc

    @property
    def lesson(self):
        return self.curriculum.lessons[self.index]

    @property
    def state(self) -> LearningState:
        counts = [len(lesson.questions)+1 for lesson in self.curriculum.lessons]
        current = {"OVERVIEW": 0, "EXPLANATION": 0, "EXAMPLE": 1,
                   "PRACTICE": 2+self.question_index, "FEEDBACK": 3+self.question_index,
                   "COMPLETE": counts[self.index]}[self.phase]
        progress = (sum(counts[:self.index])+current)/sum(counts)
        return LearningState(self.curriculum.course_id, self.lesson.chapter_id,
                             self.lesson.lesson_id, self.lesson.concept_id, progress,
                             None, self.phase)

    def context(self) -> dict:
        return {**asdict(self.state), "course_title": self.curriculum.title,
                "chapter_title": self.lesson.chapter_title, "concept_title": self.lesson.title,
                "unit_id": self.lesson.unit_id, "topic_id": self.lesson.topic_id,
                "button_label": LABELS[self.phase], "can_advance": self.phase not in {"PRACTICE", "COMPLETE"},
                "lesson_position": self.index+1, "lesson_count": len(self.curriculum.lessons),
                "review_count": len(self.curriculum.review_units)}

    def _move(self, index, phase, question_index=0):
        # Commit bookmark before publishing a new visible state.
        payload = {
            "schema": 1, "course_id": self.curriculum.course_id,
            "draft_sha256": self.curriculum.sha256, "lesson_index": index,
            "lesson_id": self.curriculum.lessons[index].lesson_id,
            "phase": phase, "question_index": question_index,
        }
        for attempt in range(3):
            try:
                atomic_write_json(self.checkpoint, payload)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02)
        self.index, self.phase, self.question_index = index, phase, question_index

    @staticmethod
    def _question(q):
        return q["question"] + "\n\n" + "\n".join(f"{chr(65+i)}. {o}" for i, o in enumerate(q["options"]))

    def display(self) -> str:
        lesson = self.lesson
        if self.phase == "OVERVIEW":
            goals = "\n".join(f"• 能说明{p.split('：', 1)[0]}。" for p in lesson.points[:3])
            return (f"课程：{self.curriculum.title}\n章节：{lesson.chapter_title}\n知识点：{lesson.title}\n\n"
                    f"学习目标\n{goals}\n\n学习顺序：讲解 → 例题 → {len(lesson.questions)-1} 道练习 → 下一知识点。\n"
                    "这是课程草稿预览；待审单元保留待审。点击“开始讲解”或输入“继续学习”。")
        if self.phase == "EXPLANATION":
            return "讲解 · " + lesson.title + "\n\n" + "\n\n".join(f"{i+1}. {p}" for i,p in enumerate(lesson.points)) + "\n\n下一步：查看例题。"
        if self.phase == "EXAMPLE":
            q = lesson.questions[0]
            return "例题\n\n" + self._question(q) + f"\n\n答案：{q['correct_option']}\n解析：{q['explanation']}\n\n下一步：开始练习。"
        if self.phase == "PRACTICE":
            return (f"练习 {self.question_index+1}/{len(lesson.questions)-1} · {lesson.title}\n\n"
                    + self._question(lesson.questions[self.question_index+1]) + "\n\n请输入选项字母或完整选项文本。")
        if self.phase == "FEEDBACK":
            q = lesson.questions[self.question_index+1]
            return ((self.feedback or "已恢复到本题反馈。") + f"\n答案：{q['correct_option']}\n解析：{q['explanation']}\n\n"
                    + ("下一步：下一道练习。" if self.question_index < len(lesson.questions)-2 else "本知识点练习已完成。下一步：按课程顺序继续。"))
        return f"已完成 {len(self.curriculum.lessons)} 个可预览单元。另有 {len(self.curriculum.review_units)} 个单元等待结构审核。草稿完成度不代表正式掌握度。"

    def advance(self) -> str:
        if self.phase in {"OVERVIEW", "EXPLANATION", "EXAMPLE"}:
            self._move(self.index, {"OVERVIEW": "EXPLANATION", "EXPLANATION": "EXAMPLE", "EXAMPLE": "PRACTICE"}[self.phase])
        elif self.phase == "FEEDBACK":
            if self.question_index < len(self.lesson.questions)-2:
                self._move(self.index, "PRACTICE", self.question_index+1)
            elif self.index+1 < len(self.curriculum.lessons):
                self._move(self.index+1, "OVERVIEW")
            else:
                self._move(self.index, "COMPLETE", self.question_index)
            self.feedback = ""
        return self.display()

    def answer(self, text: str) -> str:
        if self.phase != "PRACTICE":
            return "当前不是作答阶段，请点击卡片上的下一步。"
        q = self.lesson.questions[self.question_index+1]
        text = text.strip()
        option = text.upper()
        if text in q["options"]:
            option = chr(65+q["options"].index(text))
        if option not in tuple(chr(65+i) for i in range(len(q["options"]))):
            return "尚未提交：请输入一个选项字母或完整选项文本。自由文本不会被猜测判分。"
        self._move(self.index, "FEEDBACK", self.question_index)
        self.feedback = "回答正确。" if option == q["correct_option"] else "这次选择不正确，请结合解析复习。"
        return self.display()
