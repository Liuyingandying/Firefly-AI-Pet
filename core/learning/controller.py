"""LearningModeController (Phase 1B).

Owns the learning-mode / course / session lifecycle ONLY:

- enter / exit learning mode
- create / select / list / delete courses (user-visible "学习项目",
  internally the Phase 1A Course entity)
- start / end StudySessions (a session starts only AFTER a course is
  selected; switching courses safely ends the old session first)
- persist ``learning.last_course_id`` via SettingsManager and restore it

Forbidden: direct provider calls, quiz generation, OCR, vision, mastery
writes. The future LearningOrchestrator builds task orchestration on top;
this controller stays small.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from core.learning.intents import (
    LearningIntent,
    ParsedIntent,
    is_cancellation,
    is_confirmation,
    parse_learning_intent,
)
from core.learning.mode import LearningModeState, LearningRuntimeContext
from core.learning.models import Course, CourseStatus, StudySession
from core.learning.store import LearningStore, LearningStoreError

# Phase 1D: assessment markers come from the assessment service (single
# source; the controller only routes explicit check-understanding requests).
from core.learning.assessment import (
    is_check_understanding_request,
    is_review_practice_request,
)

LAST_COURSE_SETTING = "learning.last_course_id"

log = logging.getLogger(__name__)

# Pending flows the controller awaits an explicit user answer for.
_PENDING_CREATE = "create_course"
_PENDING_DELETE = "delete_course"


class LearningModeError(RuntimeError):
    """Controller-level error (store unavailable, invalid state...)."""


class _SettingsBridge:
    """Minimal settings abstraction: a SettingsManager (preferences dict) or
    a plain dict-like object. Persistence is best-effort — a missing settings
    object only disables last-course restore, never breaks the mode."""

    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings

    def get_last_course_id(self) -> str | None:
        if self._settings is None:
            return None
        try:
            return getattr(self._settings, "learning_last_course_id", None) or None
        except Exception:  # noqa: BLE001 - persistence is best-effort
            return None

    def set_last_course_id(self, course_id: str | None) -> None:
        if self._settings is None:
            return
        try:
            setter = getattr(self._settings, "set_learning_last_course_id", None)
            if callable(setter):
                setter(course_id or "")
        except Exception:  # noqa: BLE001 - persistence is best-effort
            pass

    def get_owner_name(self) -> str:
        """Best-effort user display name for the greeting ("欢迎回来，X。").

        Returns "" when settings carry no such key — the UI then greets without
        a name rather than inventing one.
        """
        if self._settings is None:
            return ""
        for key in ("owner_name", "user_name", "nickname", "display_name"):
            try:
                value = getattr(self._settings, key, None)
            except Exception:  # noqa: BLE001
                value = None
            if value:
                return str(value).strip()
        return ""


class LearningModeController:
    """Mode / course / session lifecycle controller."""

    def __init__(
        self,
        store: LearningStore | None = None,
        settings: Any | None = None,
        assessment_service: Any | None = None,
        curriculum_store: Any | None = None,
    ) -> None:
        self._store = store if store is not None else LearningStore()
        self._settings = _SettingsBridge(settings)
        self.state = LearningModeState()
        self._pending: str | None = None
        self._pending_course_id: str | None = None
        self._pending_course_name: str | None = None
        # Phase 2-UX: True while the entry card (welcome / project picker) is
        # the current UI state — set when the mode opens with no running course
        # and cleared as soon as a course is actually selected. The console
        # reads it to decide whether to render the entry surface; it is state,
        # not a UI rule.
        self.entry_pending = False
        # Phase 1E: the console builds the controller at startup with the
        # default store, so make sure the schema exists before the first user
        # action. initialize() is idempotent; a failure only disables the
        # persistence-backed commands (the mode shell keeps working).
        self.store_ready = True
        try:
            self._store.initialize()
        except Exception:  # noqa: BLE001 - startup must never crash the shell
            self.store_ready = False
            log.warning("learning store initialization failed", exc_info=True)
        # Phase 1D: optional assessment flow (quiz -> evidence -> rule engine).
        # The controller only routes; it never grades or writes mastery.
        self.assessment_service = assessment_service
        # Phase 2-LC: read-only curriculum context (lazily built from the same
        # LearningStore). Never activated here, never written to.
        self._curriculum_store = curriculum_store
        # Phase 3.5: learning-interaction tracking (append-only facts about what
        # the learner discussed). Lazily built; recording never breaks a turn.
        self._interaction_recorder = None
        # Phase 5: action executor (read-only by default). Lazily built.
        self._executor = None
        # Phase 6: loop orchestrator (pure flow composition). Lazily built.
        self._orchestrator_instance = None
        # Phase 7B: resource store (read-only use in teaching context). Lazy.
        self._resource_store = None

    # ------------------------------------------------------------------
    # mode entry / exit
    # ------------------------------------------------------------------

    def enter_mode(self) -> str:
        """Enable learning mode and ask which project to work on (Phase 1E.1).

        Never starts a StudySession and never activates a course here: the
        remembered last project is only *offered* (the user must pick 继续),
        and a session starts only after a course is actually selected (see
        select_course / continue_last_course). Without any course the
        controller enters the awaiting-name flow (the next user message is
        treated as a candidate course name — with a conservative guard).
        """
        self.state.enabled = True
        self.entry_pending = self.state.active_course_id is None
        courses = self._store.list_courses()
        last = self.last_course()
        if last is not None:
            return f"上次我们学到「{last.name}」，要继续吗？"
        if courses:
            names = "、".join(course.name for course in courses[:5])
            return f"进入学习模式了。要从哪个学习项目开始？现在的项目有：{names}"
        self._pending = _PENDING_CREATE
        return "进入学习模式了。我们先建一个学习项目吧，你现在想学哪门课？"

    def exit_mode(self) -> str:
        """Leave learning mode (Phase 1E.1 frozen exit semantics).

        - ends the active StudySession (one runtime owns one session)
        - clears the runtime state: enabled / active_course_id /
          active_session_id  ->  False / None / None
        - KEEPS ``last_course_id``: the project is a long-lived asset, so a
          later explicit "继续学习" can still offer it
        - cancels any in-flight assessment

        After this the app is in FREE_CHAT: a restart stays in FREE_CHAT and
        never re-activates the course.
        """
        self._cancel_assessment()
        self._end_active_session()
        self.state = LearningModeState(enabled=False)
        self._pending = None
        self._pending_course_id = None
        self._pending_course_name = None
        self.entry_pending = False
        return "好，先休息一下。想学的时候随时叫我。"

    # ------------------------------------------------------------------
    # course ("学习项目") operations
    # ------------------------------------------------------------------

    def create_course(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return "项目叫什么名字？"
        existing = self._find_course_by_name(name)
        if existing is not None:
            return (
                f"已经有「{existing.name}」这个学习项目了，"
                "要直接切过去吗？（说「继续」或「切换到它」即可）"
            )
        self._pending = None
        try:
            course = self._store.create_course(name)
        except LearningStoreError as exc:
            return f"（创建学习项目失败：{exc}）"
        self._settings.set_last_course_id(course.id)
        return self._select_course_impl(course, reply=f"好，已经建立「{course.name}」。我们从哪里开始？")

    def select_course(self, course_id: str) -> str:
        course = self._course_or_none(course_id)
        if course is None:
            return "（这个学习项目已经不存在了，可能已被删除。）"
        return self._select_course_impl(course, reply=f"那我们继续「{course.name}」。")

    def select_course_by_name(self, name: str) -> str:
        course = self._find_course_by_name(name)
        if course is None:
            return f"还没有「{name}」这个学习项目，要现在新建吗？"
        return self.select_course(course.id)

    def continue_last_course(self) -> str | None:
        """Restore the persisted last course; None when nothing to restore.

        This is a USER-EXPLICIT action (the 继续 chip / "继续学习"): it selects
        the course, which starts a NEW StudySession for this run. It is never
        called from startup.
        """
        course = self.last_course()
        if course is None:
            return None
        return self.select_course(course.id)

    def last_course(self) -> Course | None:
        """The remembered last project — validated, never activated.

        Returns None when nothing is remembered, and safely CLEARS a stale id
        whose course was deleted. Read-only for the runtime: the mode state,
        the active course and the sessions are all untouched.
        """
        last_id = self._settings.get_last_course_id()
        if not last_id:
            return None
        course = self._course_or_none(last_id)
        if course is None:
            # Stale reference (project deleted): clear it and degrade safely.
            self._settings.set_last_course_id(None)
            return None
        return course

    def last_course_tuple(self) -> tuple[str, str] | None:
        """[(course_id, name)] for the 继续 chip, or None (read-only)."""
        course = self.last_course()
        return (course.id, course.name) if course is not None else None

    def list_courses(self) -> str:
        courses = self._store.list_courses()
        if not courses:
            return "还没有学习项目。说「新建学习项目」或直接告诉我课程名就可以创建。"
        lines = [f"{index}. {course.name}" for index, course in enumerate(courses, 1)]
        return "你的学习项目：\n" + "\n".join(lines)

    def list_course_tuples(self) -> list[tuple[str, str]]:
        """[(course_id, name)] for light-weight UI chips (no state change)."""
        return [(course.id, course.name) for course in self._store.list_courses()]

    def begin_create_prompt(self) -> str:
        """Enter the awaiting-name flow (UI '＋ 新建学习项目' chip path)."""
        self._pending = _PENDING_CREATE
        return "项目叫什么名字？"

    def request_delete_course(self, name: str) -> str:
        """First step of the two-step delete: ask for explicit confirmation."""
        course = self._find_course_by_name(name)
        if course is None:
            return f"没有找到「{name}」这个学习项目。"
        self._pending = _PENDING_DELETE
        self._pending_course_id = course.id
        self._pending_course_name = course.name
        if course.id == self.state.active_course_id:
            return (
                f"「{course.name}」是当前正在学习的项目，确定要删除吗？"
                "（这会删除这个项目的知识点、学习记录和复习数据）"
            )
        return (
            f"这会删除「{course.name}」的知识点、学习记录和复习数据。确定删除吗？"
        )

    def confirm_pending_delete(self) -> str:
        if self._pending != _PENDING_DELETE or self._pending_course_id is None:
            return ""
        course_id = self._pending_course_id
        name = self._pending_course_name or ""
        self._pending = None
        self._pending_course_id = None
        self._pending_course_name = None
        try:
            self._store.delete_course(course_id)
        except LearningStoreError as exc:
            return f"（删除失败：{exc}）"
        # If the deleted course was active, close its session cleanly.
        if self.state.active_course_id == course_id:
            self._end_active_session()
            self.state.active_course_id = None
            self.state.active_course_name = None
        if self._settings.get_last_course_id() == course_id:
            self._settings.set_last_course_id(None)
        return f"已删除学习项目「{name}」。"

    def cancel_pending(self) -> str | None:
        if self._pending is None:
            return None
        self._pending = None
        self._pending_course_id = None
        self._pending_course_name = None
        return "好，先不动它。"

    # ------------------------------------------------------------------
    # StudySession lifecycle
    # ------------------------------------------------------------------

    def _select_course_impl(self, course: Course, *, reply: str) -> str:
        """Switch active course: safely end the old session, then start a
        NEW session for the selected course. One active session at a time."""
        self._pending = None
        self._pending_course_id = None
        self._pending_course_name = None
        self._cancel_assessment()
        self._end_active_session()
        try:
            session = self._start_fresh_session(course.id)
        except LearningStoreError as exc:
            return f"（启动学习会话失败：{exc}）"
        self.state.active_course_id = course.id
        self.state.active_course_name = course.name
        self.state.active_session_id = session.id
        self._settings.set_last_course_id(course.id)
        # A running course means the entry surface is done.
        self.entry_pending = False
        return reply

    def _start_fresh_session(self, course_id: str) -> StudySession:
        """Open a NEW StudySession for this run (Phase 1E.1 session semantics).

        Two runs are never merged into one session. A session row left
        ``active`` can only come from a previous run that did not shut down
        cleanly (``exit_mode`` ends its own session), so close it first — this
        run then always gets a fresh session instead of appending to
        yesterday's.
        """
        leftover = self._store.get_active_session(course_id)
        while leftover is not None:
            self._store.end_session(leftover.id)
            leftover = self._store.get_active_session(course_id)
        return self._store.start_session(course_id)

    def _cancel_assessment(self) -> None:
        if self.assessment_service is not None:
            try:
                self.assessment_service.cancel()
            except Exception:  # noqa: BLE001 - cancel must never block
                pass

    def _assessment_waiting(self) -> bool:
        if self.assessment_service is None:
            return False
        from core.learning.assessment import AssessmentState
        return self.assessment_service.state == AssessmentState.WAITING_ANSWER

    def _start_assessment(self, *, prefer_due: bool = False) -> str:
        """Route an explicit assessment request into AssessmentService.

        Degrades with a plain prompt (never an LLM call, never a store write)
        when there is no active project yet. Phase 3.5: a successful start is
        recorded as an ASSESSMENT_START interaction fact (append-only).
        """
        if self.assessment_service is None:
            return "（学习测评还没准备好，稍后再试一次？）"
        if self.state.active_course_id is None:
            return "先选一个学习项目，我就能考考你了（说「继续学习」或直接告诉我课程名）。"
        reply = self.assessment_service.start_assessment(
            self.state.active_course_id,
            self.state.active_course_name or "",
            session_id=self.state.active_session_id,
            prefer_due=prefer_due,
        )
        self._record_assessment_start()
        return reply

    def _end_active_session(self) -> None:
        if self.state.active_session_id is None:
            return
        try:
            self._store.end_session(self.state.active_session_id)
        except LearningStoreError:
            pass  # never block a course switch on a store hiccup
        self.state.active_session_id = None

    # ------------------------------------------------------------------
    # Phase 3.5: learning interaction facts (append-only, write-gated)
    # ------------------------------------------------------------------

    def _recorder(self):
        """Lazily build the interaction recorder over this controller's store."""
        if self._interaction_recorder is None:
            try:
                from core.learning.interactions import InteractionRecorder

                self._interaction_recorder = InteractionRecorder(self._store)
            except Exception:  # noqa: BLE001 - tracking is optional
                return None
        return self._interaction_recorder

    def _record_concept_interaction(self, text: str) -> None:
        """Record an explicit concept interaction found in ``text``.

        Called only from the ordinary-chat branch of :meth:`handle_text` while
        learning mode is ON with an active course. The recorder itself applies
        the narrow gate (course concept match + explicit intent), so an
        ordinary message, an ambient source or an unknown term writes nothing.
        Never raises: a tracking failure must not affect the turn.
        """
        if not self.state.enabled or self.state.active_course_id is None:
            return
        recorder = self._recorder()
        if recorder is None:
            return
        context = self.learning_context()
        focus = getattr(context, "current_focus", None) if context is not None else None
        try:
            recorder.record_from_message(
                self.state.active_course_id, text, current_focus=focus
            )
        except Exception:  # noqa: BLE001 - belt and braces
            log.debug("interaction tracking failed", exc_info=True)

    # -- Phase 9C: initial learning bootstrap (read-only) ------------------

    def learning_bootstrap(self):
        """The bootstrap starting point of the ACTIVE course (Phase 9C).

        Generated only for a FRESH course (active curriculum, focus None, no
        learning history). Read-only: no writes of any kind, current_focus is
        never modified. None for every non-fresh situation.
        """
        context = self.learning_context()
        if context is None or context.current_focus is not None:
            return None
        view = self._active_view()
        if view is None:
            return None
        try:
            from core.learning.bootstrap import build_learning_bootstrap

            return build_learning_bootstrap(
                self._store, view,
                course_id=context.course_id,
                course_name=context.course_name,
                learning_context=context,
            )
        except Exception:  # noqa: BLE001 - bootstrap is optional
            log.warning("learning bootstrap build failed", exc_info=True)
            return None

    # -- Phase 8B-2: review reminder (read-only) ---------------------------

    def get_due_reviews(self) -> list[dict]:
        """Due reviews of the ACTIVE course, oldest first (read-only).

        [] when no course is running or nothing is due — never a guess. The
        concept name is resolved for display; scheduling is untouched.
        """
        course_id = self.state.active_course_id
        if not course_id:
            return []
        try:
            due = self._store.get_due_reviews()
        except LearningStoreError:
            return []
        names = {
            concept.id: concept.canonical_name
            for concept in self._store.list_concepts(course_id)
        }
        return [
            {
                "concept_id": item.concept_id,
                "concept_name": names.get(item.concept_id),
                "due_at": item.due_at,
                "interval_days": item.interval_days,
            }
            for item in due
            if item.concept_id in names
        ]

    # -- Phase 8B-1: pending assessment selection --------------------------

    def pending_assessment(self) -> dict | None:
        """The needs_choice quiz waiting for a concept selection (read-only).

        Returns None when nothing is pending. The concept list comes from the
        pending course, so the UI never has to look it up itself.
        """
        service = self.assessment_service
        pending = getattr(service, "pending_choice", None)
        if not pending:
            return None
        course_id, course_name, session_id = pending
        try:
            concepts = [
                (concept.id, concept.canonical_name)
                for concept in self._store.list_concepts(course_id)
            ]
        except LearningStoreError:
            concepts = []
        return {
            "course_id": course_id,
            "course_name": course_name,
            "session_id": session_id,
            "concepts": concepts,
        }

    def resume_pending_assessment(self, concept_name: str) -> str | None:
        """Resume the pending quiz with the user-selected concept (explicit).

        Mirrors the Phase 8A text path exactly: same service call, same
        assessment_start recording. Returns None when nothing is pending.
        """
        service = self.assessment_service
        if service is None or getattr(service, "pending_choice", None) is None:
            return None
        reply = service.resume_pending_choice(concept_name)
        self._record_assessment_start()
        return reply

    def _match_course_concept(self, text: str):
        """Deterministic course-scoped concept match for a user message."""
        if self.state.active_course_id is None:
            return None
        try:
            concepts = self._store.list_concepts(self.state.active_course_id)
        except LearningStoreError:
            return None
        from core.learning.interactions.recorder import match_course_concept

        return match_course_concept(text, concepts)

    def _record_assessment_start(self) -> None:
        """Record the check-understanding flow starting (system-generated)."""
        service = self.assessment_service
        active = getattr(service, "active", None) if service is not None else None
        concept_id = getattr(active, "concept_id", None) if active is not None else None
        course_id = self.state.active_course_id
        if not concept_id or not course_id:
            return
        recorder = self._recorder()
        if recorder is None:
            return
        try:
            recorder.record_assessment_start(course_id, concept_id)
        except Exception:  # noqa: BLE001 - tracking is optional
            log.debug("assessment interaction tracking failed", exc_info=True)

    # ------------------------------------------------------------------
    # text handling (console sends user text here first)
    # ------------------------------------------------------------------

    def handle_text(self, text: str) -> str | None:
        """Route one user message while learning mode is active.

        Returns a reply string when the message was a learning-mode command
        (or a pending-flow answer); returns None to let the ordinary chat
        path answer normally.
        """
        if not self.state.enabled:
            # Phase 1E.1: a cold start is FREE_CHAT, so exactly ONE
            # user-explicit action is intercepted while the mode is off —
            # "继续学习"（read last_course_id, enter the continue flow）.
            # Everything else stays ordinary chat.
            return self._resume_from_free_chat(text)
        text = (text or "").strip()
        if not text:
            return None

        # Pending delete confirmation: an explicit yes/no only (checked first
        # so "确定" is never mistaken for something else).
        if self._pending == _PENDING_DELETE:
            if is_confirmation(text):
                return self.confirm_pending_delete()
            if is_cancellation(text):
                return self.cancel_pending()
            return "不确定的话，我可以先不动它。要删除吗？（确定 / 取消）"

        # Phase 1D/1E: explicit check-understanding / review-practice / answer
        # flow (the ONLY path into the rule engine). Every branch needs an
        # active course; "解释/普通问答/闲聊" never matches these markers.
        if (self.assessment_service is not None
                and self.state.active_course_id is not None):
            if is_check_understanding_request(text):
                return self._start_assessment(prefer_due=False)
            if is_review_practice_request(text):
                return self._start_assessment(prefer_due=True)
        if self.assessment_service is not None and self._assessment_waiting():
            outcome = self.assessment_service.submit_answer(text)
            return outcome.reply
        # Phase 8A: the user naming a concept answers the "要检查哪个知识点"
        # prompt — resume the pending quiz with that concept (deterministic
        # course-scoped match; unknown terms stay ordinary chat).
        if (self.assessment_service is not None
                and getattr(self.assessment_service, "pending_choice", None)
                and self.state.active_course_id is not None):
            concept = self._match_course_concept(text)
            if concept is not None:
                reply = self.assessment_service.resume_pending_choice(
                    concept.canonical_name
                )
                self._record_assessment_start()
                return reply

        parsed: ParsedIntent = parse_learning_intent(text)
        if parsed.intent is None:
            # Awaiting-project-name flow: a plain short phrase becomes the
            # project name; anything else (a question, an explanation
            # request, chat) still flows to the ordinary chat path.
            if self._pending == _PENDING_CREATE:
                return self._handle_pending_create_name(text)
            # Phase 3.5: an explicit concept interaction in learning mode is
            # recorded as a fact (updates current_focus). Ordinary chat and
            # unknown terms write nothing (the recorder gates it), and the
            # message still flows to the normal chat path.
            self._record_concept_interaction(text)
            return None  # ordinary chat

        if parsed.intent is LearningIntent.EXIT_LEARNING:
            return self.exit_mode()
        if parsed.intent is LearningIntent.ENTER_LEARNING:
            if self.state.active_course_id is not None:
                return f"我们正在学「{self.state.active_course_name}」。"
            return self.enter_mode()
        if parsed.intent is LearningIntent.CHAPTER_REQUEST:
            return self._chapter_reply(parsed.chapter_name)
        if parsed.intent is LearningIntent.NEXT_STEP:
            return self._chapter_reply(None)
        if parsed.intent is LearningIntent.LIST_COURSES:
            return self.list_courses()
        if parsed.intent is LearningIntent.CURRENT_COURSE:
            if self.state.active_course_name:
                return f"当前学习项目：{self.state.active_course_name}"
            return "还没有选择学习项目。"
        if parsed.intent is LearningIntent.QUIZ_REQUEST:
            return self._start_assessment(prefer_due=False)
        if parsed.intent is LearningIntent.REVIEW_PRACTICE:
            return self._start_assessment(prefer_due=True)
        if parsed.intent is LearningIntent.CONTINUE_LEARNING:
            reply = self.continue_last_course()
            if reply is not None:
                return reply
            courses = self._store.list_courses()
            if courses:
                return "要学哪个项目？" + "".join(
                    f"\n[{course.name}]" for course in courses[:5]
                )
            return "还没有学习项目，先建一个吧——你想学哪门课？"
        if parsed.intent is LearningIntent.CREATE_COURSE:
            if parsed.course_name:
                return self.create_course(parsed.course_name)
            self._pending = _PENDING_CREATE
            return "项目叫什么名字？"
        if parsed.intent is LearningIntent.SWITCH_COURSE:
            if parsed.course_name:
                return self.select_course_by_name(parsed.course_name)
            return "要切换到哪个学习项目？"
        if parsed.intent is LearningIntent.DELETE_COURSE:
            if parsed.course_name:
                return self.request_delete_course(parsed.course_name)
            return "要删除哪个学习项目？"
        return None

    def _handle_pending_create_name(self, text: str) -> str | None:
        """Awaiting-project-name flow: capture a plausible short name, fall
        back to ordinary chat otherwise. Never silently creates a project out
        of a sentence/question."""
        self._pending = None
        if _looks_like_course_name(text):
            return self.create_course(text)
        return None

    def _resume_from_free_chat(self, text: str) -> str | None:
        """FREE_CHAT interception (Phase 1E.1 + Phase 2-UX).

        Exactly two user-explicit actions reach this while the mode is off:
        entering learning mode ("开始学习" / "打开学习模式") and continuing the
        remembered project ("继续学习"). Both are user actions — never
        background auto-restore. Entering only OFFERS the project; it never
        activates a course or opens a session.
        """
        text = (text or "").strip()
        if not text:
            return None
        if not self.store_ready:
            return None
        parsed = parse_learning_intent(text)
        if parsed.intent is LearningIntent.ENTER_LEARNING:
            return self.enter_mode()
        if parsed.intent is not LearningIntent.CONTINUE_LEARNING:
            return None
        self.state.enabled = True
        reply = self.continue_last_course()
        # Nothing remembered (or a stale id): fall back to the normal entry
        # prompt so the user can still pick / create a project.
        return reply if reply is not None else self.enter_mode()

    # ------------------------------------------------------------------
    # Phase 2-UX: structural chapter queries (read-only, no AI)
    # ------------------------------------------------------------------

    def _chapter_reply(self, chapter_name: str | None) -> str:
        """Answer a chapter / next-step request from the ACTIVE curriculum.

        Purely structural: it matches the requested chapter against the active
        curriculum's chapters (or reports the next node). It never activates,
        never writes, never plans a route.
        """
        if self.state.active_course_id is None:
            return "先选一个学习项目，我就能带你进入章节了（说「继续学习」或「开始学习」）。"
        context = self.learning_context()
        if context is None or not context.has_curriculum:
            return "这个学习项目还没有课程结构（可以从教材 PDF 导入大纲）。"

        if chapter_name is None:  # NEXT_STEP
            if context.next_in_order:
                return f"按照课程顺序，接下来是：{context.next_in_order}。"
            return "已经到课程结构的末尾了。"

        view = self._active_view()
        if view is None:
            return "这个学习项目还没有课程结构（可以从教材 PDF 导入大纲）。"
        wanted = _normalize_label(chapter_name)
        for chapter in view.chapters_in_order:
            if _normalize_label(chapter.title) == wanted or _normalize_label(
                chapter.title
            ).startswith(wanted):
                lines = [f"📘 {context.course_name}", "", f"{chapter.title}"]
                links = view.concepts_of_chapter(chapter.id)
                if links:
                    names = self._concept_names([link.concept_id for link in links])
                    if names:
                        lines += ["", "本节概念：", *[f"· {name}" for name in names]]
                return "\n".join(lines)
        titles = "、".join(chapter.title for chapter in view.chapters_in_order)
        return f"课程结构里没有找到「{chapter_name}」。现有章节：{titles}"

    def _active_view(self):
        curricula = self._curricula()
        if curricula is None or self.state.active_course_id is None:
            return None
        try:
            return curricula.get_active_curriculum(self.state.active_course_id)
        except Exception:  # noqa: BLE001 - structure is optional
            return None

    def _concept_names(self, concept_ids) -> list[str]:
        names: list[str] = []
        for concept_id in concept_ids:
            try:
                concept = self._store.get_concept(concept_id)
            except LearningStoreError:
                concept = None
            if concept is not None:
                names.append(concept.canonical_name)
        return names

    # ------------------------------------------------------------------
    # context for the chat prompt (read-only)
    # ------------------------------------------------------------------

    def context_block(self) -> str | None:
        """Prompt context for the chat turn.

        Phase 2-LC: when an ACTIVE curriculum exists for the running course the
        block is enriched with the read-only structure (current chapter /
        focus / next node) sourced from LearningContext — the same channel
        (``turn_context``) as before, still never persisted.
        """
        runtime_block = self.build_runtime_context().context_block()
        structure = self.learning_context()
        structure_block = structure.context_block() if structure is not None else None
        if runtime_block is None:
            return structure_block
        if structure_block is None:
            return runtime_block
        return f"{runtime_block}\n\n{structure_block}"

    def status_line(self) -> str:
        return self.state.status_line()

    # -- Phase 2-LC: read-only curriculum context -------------------------

    def _curricula(self):
        """Lazily build the CurriculumStore over this controller's store.

        Read-only use only; a construction failure degrades to "no structure"
        instead of breaking the learning shell.
        """
        if self._curriculum_store is None:
            try:
                from core.learning.curriculum.store import CurriculumStore

                self._curriculum_store = CurriculumStore(self._store)
            except Exception:  # noqa: BLE001 - structure is optional
                return None
        return self._curriculum_store

    def learning_context(self):
        """The read-only LearningContext of the ACTIVE course, else None.

        None means "no course is running" (free chat or the entry screen); it
        never means "guessed structure".
        """
        if self.state.active_course_id is None:
            return None
        try:
            from core.learning.context import LearningContextBuilder

            return LearningContextBuilder(self._store, self._curricula()).build(
                self.state.active_course_id
            )
        except Exception:  # noqa: BLE001 - never break chat on context read
            log.warning("learning context build failed", exc_info=True)
            return None

    def teaching_context(self):
        """Read-only TeachingContext for the running course (Phase 3).

        Deterministically derived by TeachingPolicy from the current focus's
        stored mastery and its review-due state. The controller only routes:
        it never decides mastery and never writes. Phase 7B: the current
        concept's resource references are injected (read-only display data).
        """
        context = self.learning_context()
        if context is None:
            return None
        try:
            from core.learning.teaching import TeachingPolicy

            return TeachingPolicy(self._store).build(
                context, resources=self._focus_resources(context)
            )
        except Exception:  # noqa: BLE001 - teaching posture is optional
            log.warning("teaching context build failed", exc_info=True)
            return None

    def _focus_resources(self, context) -> tuple:
        """Read-only resource references for the current focus concept."""
        focus = getattr(context, "current_focus", None)
        course_id = self.state.active_course_id
        if not focus or not course_id:
            return ()
        try:
            from core.learning.resources import ResourceStore

            concept = self._store.find_concept(course_id, focus)
            if concept is None:
                return ()
            resources = self._resource_store_lazy()
            if resources is None:
                return ()
            return tuple(resources.get_resources_for_concept(concept.id))
        except Exception:  # noqa: BLE001 - resources are optional display data
            log.warning("resource read failed", exc_info=True)
            return ()

    def teaching_block(self) -> str | None:
        """Prompt block for the teaching posture; None when there is none."""
        context = self.teaching_context()
        return context.context_block() if context is not None else None

    # -- Phase 4: read-only decision layer --------------------------------

    def learning_decision(self):
        """Read-only next-action recommendation (Phase 4).

        Pure rules over the LC/teaching signals plus the chapter-completion
        fact. Deterministic; never writes and never calls a provider.
        """
        context = self.learning_context()
        if context is None:
            return None
        try:
            from core.learning.decision import LearningDecisionPolicy

            chapter_complete, next_label = self._chapter_progress(context)
            return LearningDecisionPolicy.from_contexts(
                context,
                self.teaching_context(),
                chapter_complete=chapter_complete,
                next_label=next_label,
            )
        except Exception:  # noqa: BLE001 - the decision is optional
            log.warning("learning decision build failed", exc_info=True)
            return None

    def decision_block(self) -> str | None:
        """Prompt block for the next-action recommendation (read-only)."""
        recommendation = self.learning_decision()
        return recommendation.context_block() if recommendation is not None else None

    # -- Phase 5: read-only action execution ------------------------------

    def _action_executor(self):
        """Lazily build the action executor over this controller's services."""
        if self._executor is None:
            try:
                from core.learning.action import LearningActionExecutor

                self._executor = LearningActionExecutor(
                    store=self._store,
                    assessment_service=self.assessment_service,
                )
            except Exception:  # noqa: BLE001 - execution is optional
                return None
        return self._executor

    def learning_action(self, *, run_assessment: bool = False):
        """Execute the current recommendation (Phase 5).

        Read-only by default: ``run_assessment=False`` prepares the action's
        guidance without starting an assessment, so it is safe to call on every
        turn. Only an explicit invocation passes ``run_assessment=True``.
        """
        recommendation = self.learning_decision()
        if recommendation is None:
            return None
        executor = self._action_executor()
        if executor is None:
            return None
        context = self.learning_context()
        _complete, next_label = self._chapter_progress(context)
        return executor.execute(
            recommendation,
            course_id=self.state.active_course_id,
            course_name=self.state.active_course_name or "",
            session_id=self.state.active_session_id,
            next_label=next_label,
            run_assessment=run_assessment,
        )

    def action_block(self) -> str | None:
        """Prompt block for the prepared action (read-only, no side effects)."""
        result = self.learning_action(run_assessment=False)
        return result.context_block() if result is not None else None

    def execute_learning_action(self):
        """Explicitly run the decided action (PRACTICE starts the assessment).

        Never called automatically: the runtime only prepares actions unless the
        user explicitly asks to proceed (e.g. by saying 「考考我」).
        """
        return self.learning_action(run_assessment=True)

    # -- Phase 6: loop orchestration (read-only injection) -----------------

    def _orchestrator(self):
        """Lazily build the loop orchestrator over this controller."""
        if self._orchestrator_instance is None:
            try:
                from core.learning.orchestrator import LearningLoopOrchestrator

                self._orchestrator_instance = LearningLoopOrchestrator(self)
            except Exception:  # noqa: BLE001 - orchestration is optional
                return None
        return self._orchestrator_instance

    def learning_loop_block(self) -> str | None:
        """Read-only composition of the learning loop blocks (Phase 6).

        Context + teaching + decision + action in the frozen flow order. Never
        calls :meth:`handle_text`, so it can never double-record an interaction
        or double-start an assessment.
        """
        orchestrator = self._orchestrator()
        if orchestrator is None:
            return None
        try:
            return orchestrator.loop_block()
        except Exception:  # noqa: BLE001 - injection must never break a turn
            log.warning("learning loop block failed", exc_info=True)
            return None

    def run_learning_loop(self, user_message: str):
        """Run the full learning turn (detection + recording + contexts).

        For turn owners (console / tests). The runner only uses the read-only
        :meth:`learning_loop_block`.
        """
        orchestrator = self._orchestrator()
        if orchestrator is None:
            return None
        try:
            return orchestrator.run(user_message)
        except Exception:  # noqa: BLE001 - never break the turn
            log.warning("learning loop run failed", exc_info=True)
            return None

    # -- Phase 7A: textbook import wiring ----------------------------------

    def create_course_shell(self, name: str) -> Course:
        """Create a course ROW without selecting it (Phase 7A).

        Used by the textbook-import flow to anchor a draft: no learning-mode
        state changes, no StudySession, no activation — the user still has to
        confirm the draft (and pick the project) explicitly. The project IS
        remembered as ``last_course_id`` so a later explicit 「继续学习」 finds
        it (Phase 8A: the confirm message promises exactly that).
        """
        name = (name or "").strip()
        if not name:
            raise LearningModeError("course name must not be empty")
        course = self._store.create_course(name)
        self._settings.set_last_course_id(course.id)
        return course

    def textbook_review_service(self):
        """The CF3 review service wired to this controller's store/curricula."""
        curricula = self._curricula()
        if curricula is None:
            return None
        try:
            from core.learning.curriculum.adapter.review import (
                CurriculumDraftReviewService,
            )

            return CurriculumDraftReviewService(curricula)
        except Exception:  # noqa: BLE001 - review is optional wiring
            return None

    def resource_store(self):
        """The Phase 7B ResourceStore over this controller's store (lazy)."""
        try:
            from core.learning.resources import ResourceStore

            return ResourceStore(self._store)
        except Exception:  # noqa: BLE001 - resources are optional
            return None

    def _resource_store_lazy(self):
        if self._resource_store is None:
            self._resource_store = self.resource_store()
        return self._resource_store

    def _chapter_progress(self, context) -> tuple[bool, str | None]:
        """(chapter_complete, next_label) from the ACTIVE curriculum — read-only.

        ``chapter_complete`` means every concept linked to the current chapter
        has reached the mastered band (``MASTERY_MASTERED``). ``next_label`` is
        the following structural node, or None at the end of the route. Both are
        facts, not judgements: nothing is written and mastery is only read.
        """
        view = self._active_view()
        if view is None or not context or not context.current_chapter:
            return False, None
        chapter = next(
            (c for c in view.chapters if c.title == context.current_chapter), None
        )
        if chapter is None:
            return False, None
        links = view.concepts_of_chapter(chapter.id)
        if not links:
            return False, None
        try:
            from core.learning.decision import MASTERY_MASTERED
        except Exception:  # noqa: BLE001
            MASTERY_MASTERED = 4
        complete = True
        for link in links:
            try:
                concept = self._store.get_concept(link.concept_id)
            except LearningStoreError:
                concept = None
            if concept is None or concept.mastery_level < MASTERY_MASTERED:
                complete = False
                break

        order = tuple(getattr(view, "learning_order", ()) or ())
        next_label: str | None = None
        for index, item in enumerate(order):
            if item.target_id != chapter.id:
                continue
            if index + 1 < len(order):
                following = order[index + 1]
                following_chapter = view.chapter_by_id(following.target_id)
                next_label = (
                    following_chapter.title if following_chapter is not None
                    else following.target_id
                )
            break
        return complete, next_label

    def entry_snapshot(self) -> dict:
        """Read-only data for the Phase 2-UX entry card.

        Shows (never activates) the remembered last project and the existing
        project list. ``owner_name`` is empty unless settings really carry one —
        the UI must not invent a name.
        """
        last = self.last_course()
        courses = self._store.list_courses()
        return {
            "owner_name": self._settings.get_owner_name(),
            "last_course": (last.id, last.name) if last is not None else None,
            "courses": [(course.id, course.name) for course in courses],
            "course_count": len(courses),
            "enabled": self.state.enabled,
        }

    def project_summaries(self) -> list[dict]:
        """Read-only per-project summaries for the picker (no activation).

        Each entry reports the project's ACTIVE curriculum structure (title +
        version) when one exists and its most recently studied concept, so the
        picker can render "当前课程结构 / 最近学习 / 状态" without computing
        anything itself.
        """
        curricula = self._curricula()
        summaries: list[dict] = []
        for course in self._store.list_courses():
            view = None
            if curricula is not None:
                try:
                    view = curricula.get_active_curriculum(course.id)
                except Exception:  # noqa: BLE001 - structure is optional
                    view = None
            concept_name = self._recent_concept_name(course.id)
            summaries.append(
                {
                    "course_id": course.id,
                    "name": course.name,
                    "curriculum_title": view.curriculum.title if view is not None else None,
                    "curriculum_version": view.curriculum.version if view is not None else None,
                    "recent": concept_name,
                    "status": (
                        "继续学习" if view is not None or concept_name else "还没有课程结构"
                    ),
                    "action": "进入",
                }
            )
        return summaries

    def recent_concept_name(self, course_id: str | None) -> str | None:
        """Most recently studied concept of a course (read-only)."""
        return self._recent_concept_name(course_id)

    def _recent_concept_name(self, course_id: str | None) -> str | None:
        if not course_id:
            return None
        try:
            studied = [
                concept
                for concept in self._store.list_concepts(course_id)
                if concept.last_studied_at
            ]
        except LearningStoreError:
            return None
        if not studied:
            return None
        return max(studied, key=lambda c: c.last_studied_at or "").canonical_name


    # -- Phase 1E: runtime context & startup restore ----------------------

    def build_runtime_context(self) -> LearningRuntimeContext:
        """Read-only aggregation of the current learning runtime.

        Reads the store (course concepts / due reviews) but never writes;
        injected into ordinary chat turns via the existing context channel.
        """
        enabled = self.state.enabled
        course_id = self.state.active_course_id if enabled else None
        concept_name = None
        concept_mastery = None
        due_count = 0
        if course_id:
            concepts = [
                c for c in self._store.list_concepts(course_id)
                if c.last_studied_at
            ]
            if concepts:
                latest = max(concepts, key=lambda c: c.last_studied_at or "")
                concept_name = latest.canonical_name
                concept_mastery = latest.mastery_level
            try:
                due_count = len(self._store.get_due_reviews())
            except LearningStoreError:
                due_count = 0
        session_active = (
            enabled and self.state.active_session_id is not None
        )
        return LearningRuntimeContext(
            enabled=enabled,
            course_id=course_id,
            course_name=self.state.active_course_name if enabled else None,
            concept_name=concept_name,
            concept_mastery=concept_mastery,
            session_id=self.state.active_session_id if enabled else None,
            session_active=session_active,
            due_review_count=due_count,
        )

    def restore_runtime(self) -> bool:
        """Startup validation ONLY (Phase 1E.1 redefinition).

        Frozen startup semantics: a cold start is ALWAYS FREE_CHAT. This
        method therefore never turns learning mode on — it does not set
        ``enabled``, does not set ``active_course_id``, does not select a
        course, does not start or re-attach a StudySession, and does not build
        an active learning context. It only checks that the remembered
        ``last_course_id`` still resolves, clearing a stale id, so a LATER
        user-explicit "继续学习" can offer it.

        Returns True when a remembered project is available to offer (purely
        informational for the UI); the runtime state is never touched.
        Never raises: startup must stay crash-free with a missing/corrupt
        store."""
        try:
            return self.last_course() is not None
        except Exception:  # noqa: BLE001 - degrade to "nothing remembered"
            log.warning("learning last-course validation failed", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _course_or_none(self, course_id: str) -> Course | None:
        try:
            return self._store.get_course(course_id)
        except LearningStoreError:
            return None

    def _find_course_by_name(self, name: str) -> Course | None:
        key = re.sub(r"\s+", "", name or "").lower()
        for course in self._store.list_courses():
            if re.sub(r"\s+", "", course.name).lower() == key:
                return course
        return None


_QUESTION_MARKERS = ("？", "?", "吗", "是什么", "什么", "怎么", "为什么",
                     "如何", "帮我", "看一下", "解释", "讲讲", "看到", "觉得")


def _looks_like_course_name(text: str) -> bool:
    """Conservative guard for the awaiting-name flow: a short phrase without
    question/chat markers is treated as a course name; anything else falls
    back to ordinary chat (never silently create a course from a sentence)."""
    compact = re.sub(r"\s+", "", text or "")
    if not compact or len(compact) > 8:
        return False
    return not any(marker in compact for marker in _QUESTION_MARKERS)


def _normalize_label(value: str | None) -> str:
    """Whitespace/space-insensitive label comparison for chapter matching."""
    return re.sub(r"[\s:：、,，.。]+", "", value or "").lower()
