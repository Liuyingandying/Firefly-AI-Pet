"""Single-threaded teach-mcp work for the Firefly student presentation path.

The executor serializes MCP operations and owns its transport. Qt signals carry
validated display text back to the GUI thread; no learning facts are persisted
in Firefly.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import Callable

from PySide6.QtCore import QObject, Signal

from core.crash_diagnostics import log_thread
from learning.diagnostics import log_marker
from learning.student_presentation import PresentationError, feedback_to_display, question_to_display
from learning.teach_mcp_client import TeachMcpClient, _tool_payload


@dataclass(frozen=True)
class QuestionReady:
    learner_id: str
    course_id: str
    session_id: str
    question_id: str
    display: str


@dataclass(frozen=True)
class FeedbackReady:
    question_id: str
    display: str


@dataclass(frozen=True)
class LearningError:
    message: str


class LearningWorker(QObject):
    """Run one TutorTurn's resume, presentation ACK and answer off the UI thread."""

    event = Signal(object)

    def __init__(self, transport_factory: Callable[[], object]):
        super().__init__()
        self._transport_factory = transport_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="firefly-learning")
        self._closed = Event()
        self._transport = None
        self._client: TeachMcpClient | None = None
        self._turn: QuestionReady | None = None
        self._requested_topic = ""
        self._resumed_topic = ""

    def resume(self, learner_id: str, course_id: str, topic: str) -> None:
        self._queue(self._resume, learner_id, course_id, topic)

    def presentation_ack(self, question_id: str, rendered_text: str) -> None:
        self._queue(self._presentation_ack, question_id, rendered_text)

    def submit_answer(self, question_id: str, student_answer: str) -> None:
        self._queue(self._submit_answer, question_id, student_answer)

    def _queue(self, action, *args) -> None:
        if not self._closed.is_set():
            self._executor.submit(self._run, action, *args)

    def _run(self, action, *args) -> None:
        if self._closed.is_set():
            return
        log_thread(f"learning_worker_{action.__name__}")
        try:
            action(*args)
        except Exception as exc:  # worker boundary: never emit raw tool output
            log_marker("LEARNING_UI", event="error", reason=type(exc).__name__)
            if not self._closed.is_set():
                self.event.emit(LearningError("学习服务暂时无法完成操作，请稍后重试。"))

    @staticmethod
    def _success(payload: dict, operation: str) -> dict:
        if payload.get("status") != "success":
            raise PresentationError(f"{operation} failed")
        return payload

    def _checked_turn(self, payload: dict, turn: QuestionReady) -> dict:
        self._success(payload, "tutor turn")
        if any(payload.get(key) != expected for key, expected in (
            ("learner_id", turn.learner_id),
            ("session_id", turn.session_id),
        )):
            raise PresentationError("tutor turn identity mismatch")
        returned_course = payload.get("course_id")
        if returned_course != turn.course_id:
            # Older TutorTurn rows have an explicit empty course_id. Accept
            # one only when both the resume result and the turn match the
            # exact course title requested by this Firefly presentation.
            if not (
                returned_course == ""
                and self._resumed_topic == self._requested_topic
                and payload.get("topic") == self._resumed_topic
            ):
                raise PresentationError("tutor turn identity mismatch")
        return payload

    def _resume(self, learner_id: str, course_id: str, topic: str) -> None:
        transport = self._transport_factory()
        transport.start()
        self._transport = transport
        client = TeachMcpClient(transport.call_tool)
        self._client = client
        resumed = self._success(_tool_payload(transport.call_tool(
            "resume_learning", {"learner_id": learner_id, "topic": topic},
        )), "resume_learning")
        session_id = resumed.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise PresentationError("missing session reference")
        self._requested_topic = topic
        self._resumed_topic = resumed.get("topic") if isinstance(resumed.get("topic"), str) else ""
        turn_ref = QuestionReady(learner_id, course_id, session_id, "", "")
        snapshot = self._checked_turn(client.tutor_resume_turn(session_id), turn_ref)
        if snapshot.get("stage") == "ADVANCE":
            # A completed concept has no pending question. Let teach-mcp
            # choose the next concept before using the normal question path.
            advanced = client.tutor_advance(session_id)
            if advanced.get("error_code") == "QUESTION_BANK_INSUFFICIENT":
                log_marker("LEARNING_UI", event="unavailable", reason="QUESTION_BANK_INSUFFICIENT")
                if not self._closed.is_set():
                    self.event.emit(LearningError("下一知识点尚缺足够的已审核题目，暂时无法继续出题。"))
                return
            snapshot = self._checked_turn(advanced, turn_ref)
        if snapshot.get("stage") not in ("TEACH", "CHECK", "RECHECK"):
            raise PresentationError("unsupported TutorTurn stage")
        if snapshot.get("pending_question") is None:
            snapshot = self._checked_turn(client.tutor_next_question(session_id), turn_ref)
        question = snapshot.get("pending_question")
        log_marker("LEARNING_UI", event="question_received", course_id=course_id)
        display = question_to_display(question)
        if question.get("session_id", session_id) != session_id:
            raise PresentationError("question session mismatch")
        question_id = question.get("question_id")
        if not isinstance(question_id, str) or not question_id:
            raise PresentationError("missing question reference")
        self._turn = QuestionReady(learner_id, course_id, session_id, question_id, display)
        if not self._closed.is_set():
            self.event.emit(self._turn)

    def _presentation_ack(self, question_id: str, rendered_text: str) -> None:
        turn, client = self._turn, self._client
        if turn is None or client is None or turn.question_id != question_id or turn.display != rendered_text:
            raise PresentationError("presentation ACK mismatch")
        snapshot = self._checked_turn(client.tutor_resume_turn(turn.session_id), turn)
        question = snapshot.get("pending_question")
        if not isinstance(question, dict) or question.get("question_id") != question_id:
            raise PresentationError("pending question mismatch")
        if question.get("session_id", turn.session_id) != turn.session_id:
            raise PresentationError("question session mismatch")
        if question_to_display(question) != rendered_text:
            raise PresentationError("rendered text mismatch")
        if not snapshot.get("question_presented"):
            marked = self._checked_turn(client.tutor_mark_presented(
                turn.session_id, question_id, rendered_text,
            ), turn)
            marked_question = marked.get("pending_question")
            if (not isinstance(marked_question, dict)
                    or marked_question.get("question_id") != question_id
                    or marked.get("question_presented") is not True):
                raise PresentationError("presentation ACK failed")
        log_marker("LEARNING_UI", event="presentation_ack", question_id=question_id)

    def _submit_answer(self, question_id: str, student_answer: str) -> None:
        turn, client = self._turn, self._client
        if turn is None or client is None or turn.question_id != question_id:
            raise PresentationError("answer question mismatch")
        snapshot = self._checked_turn(client.tutor_resume_turn(turn.session_id), turn)
        question = snapshot.get("pending_question")
        if not isinstance(question, dict) or question.get("question_id") != question_id:
            raise PresentationError("pending question changed")
        if snapshot.get("question_presented") is not True:
            raise PresentationError("question was not presented")
        result = client.tutor_submit_answer(turn.session_id, question_id, student_answer)
        self._success(result, "tutor_submit_answer")
        log_marker("LEARNING_UI", event="answer_submitted", question_id=question_id)
        feedback = feedback_to_display(result)
        log_marker("LEARNING_UI", event="feedback_received", question_id=question_id)
        if not self._closed.is_set():
            self.event.emit(FeedbackReady(question_id, feedback))

    def _close_transport(self) -> None:
        transport, self._transport = self._transport, None
        self._client = None
        self._turn = None
        if transport is not None:
            transport.close()

    def close(self) -> None:
        """Suppress late events and close MCP after the running operation exits."""
        if self._closed.is_set():
            return
        self._closed.set()
        self._executor.submit(self._close_transport)
        self._executor.shutdown(wait=False)
