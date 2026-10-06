"""Bridge learning session for legacy launch and Firefly TutorTurn display.

默认模式（interactive）：Firefly 点击「开始/继续学习」→ headless init run
执行 bootstrap（skill + teach-mcp）→ 从 --json 官方输出提取 Z Code
sessionId → 写入 binding.zcode_session_id（opaque UI 恢复引用）→ 用
``--resume <sessionId>`` 打开**课程专属**可交互 Z Code TUI。教学内容、
题目与回答全部发生在 Z Code；Firefly 聊天保持普通聊天能力。

``mode="embedded"`` 为实验分支（Return Channel 的 result.json → 聊天框 +
回答拦截），默认不启用；学习状态唯一权威始终是 teach-mcp。
Phase 2C 的学生展示路径在 Worker 线程读取 teach-mcp TutorTurn，在
ChatView append 成功后异步确认展示；不使用 Return Channel 或 Z Code TUI
作为题目展示层。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from core.crash_diagnostics import check_qobject_thread
from learning._storage import BridgeFileError
from learning.course_binding import save_binding
from learning.diagnostics import log_marker
from learning.launcher import (
    LaunchError,
    launch_learning_mode,
    open_learning_session,
    read_init_session_id,
)
from learning.learning_worker import FeedbackReady, LearningError, LearningWorker, QuestionReady
from learning.result_watcher import LearningResultWatcher
from learning.resource_manager import ResourceImportError, ResourceManager
from learning.teach_mcp_stdio import McpTransportError, StdioMcpTransport

EXIT_PHRASES = ("退出学习", "结束学习")

MODE_INTERACTIVE = "interactive"
MODE_EMBEDDED = "embedded"  # experimental Return Channel (not default)


@dataclass
class PendingInteraction:
    learner_id: str
    course_id: str
    session_id: str
    question_id: str
    task_id: str


class BridgeLearningSession(QObject):
    """Owns the course → interactive Z Code learning session lifecycle."""

    result_ready = Signal(str)       # non-modal status/teaching text for chat
    agent_timeout = Signal(str)      # task_id — gentle "still working" hint
    answer_submitted = Signal(str)   # question_id (embedded mode only)
    question_ready = Signal(str, str)  # question_id, validated display text
    feedback_ready = Signal(str)  # validated tutor feedback
    presentation_error = Signal(str)  # safe diagnostic, never a question bubble
    learning_context_changed = Signal(object)  # curriculum presentation snapshot

    def __init__(
        self,
        settings,
        *,
        courses_root: Path | str | None = None,
        parent: QObject | None = None,
        mode: str = MODE_INTERACTIVE,
        poll_ms: int = 500,
        slow_poll_ms: int = 2000,
        timeout_s: float = 900.0,
        presentation_transport_factory: Callable[[], StdioMcpTransport] | None = None,
        curriculum_navigation_root: Path | None = None,
    ):
        super().__init__(parent)
        self._settings = settings
        self._learner_id = str(getattr(settings, "learning_learner_id", "") or "")
        self._courses_root = courses_root
        self._mode = mode
        self._poll_ms = poll_ms
        self._slow_poll_ms = slow_poll_ms
        self._timeout_s = timeout_s
        self._timer: QTimer | None = None
        self._watcher = None  # embedded mode only
        self._pending: PendingInteraction | None = None
        self._course_dir: Path | None = None
        self._task_id: str = ""
        self._elapsed_ms = 0
        self._hinted = False
        self._result_received = False
        self._rollover_old_session_id = ""
        self._presentation_transport_factory = presentation_transport_factory or self._configured_teach_transport
        self._presentation_worker: LearningWorker | None = None
        self._presentation_pending: PendingInteraction | None = None
        self._answer_in_flight = False
        self._curriculum = None
        self._curriculum_course_id = ""
        self._curriculum_navigation_root = curriculum_navigation_root

    @staticmethod
    def _configured_teach_transport() -> StdioMcpTransport:
        """Reuse the installed teach-mcp command, without reading learning state."""
        config_path = Path.home() / ".zcode" / "cli" / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            server = config["mcp"]["servers"]["teach-mcp"]
            command, args = server["command"], server.get("args", [])
            if not isinstance(command, str) or not command or not isinstance(args, list):
                raise ValueError("invalid teach-mcp command")
            timeout_s = max(1.0, float(server.get("timeoutMs", 60000)) / 1000)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise McpTransportError("teach-mcp 连接配置不可用") from exc
        return StdioMcpTransport(command, args, timeout_s=timeout_s)

    def _close_presentation_worker(self) -> None:
        worker, self._presentation_worker = self._presentation_worker, None
        if worker is not None:
            worker.close()

    def start_presentation(self, course_id: str, *, use_curriculum: bool = True) -> bool:
        """Queue one official TutorTurn resume without blocking the GUI thread."""
        self._curriculum = None
        self._curriculum_course_id = ""
        self.learning_context_changed.emit({})
        self._presentation_pending = None
        self._answer_in_flight = False
        self._close_presentation_worker()
        try:
            manifest = ResourceManager(self._courses_root).load_manifest(course_id)
            if manifest["learner_id"] != self._learner_id:
                raise ValueError("课程与当前学习者不匹配")
        except (OSError, ValueError, KeyError, TypeError, BridgeFileError, ResourceImportError) as exc:
            log_marker("LEARNING_PRESENTATION_REJECTED", course_id=course_id, reason=type(exc).__name__)
            self.presentation_error.emit("暂时无法显示已审核题目，请稍后重试。")
            return False
        if use_curriculum:
            from learning.orchestrator import configured_draft, load_curriculum, LearningOrchestrator
            import hashlib

            try:
                path = configured_draft(course_id)
                if path is not None:
                    scope = hashlib.sha256(f"{self._learner_id}:{course_id}".encode()).hexdigest()[:24]
                    from core.user_paths import get_user_data_paths
                    navigation_root = self._curriculum_navigation_root or get_user_data_paths().learning / "curriculum_navigation"
                    checkpoint = navigation_root / f"{scope}.json"
                    self._curriculum = LearningOrchestrator(load_curriculum(path), checkpoint)
                    self._curriculum_course_id = course_id
                    self._publish_curriculum()
                    return True
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log_marker("CURRICULUM_UI", event="load_failed", reason=type(exc).__name__)
                self.presentation_error.emit("课程草稿或导航记录无法加载；已保留原文件，请检查版本与日志。")
                return False
        worker = LearningWorker(self._presentation_transport_factory)
        self._presentation_worker = worker
        worker.event.connect(self._on_presentation_event)
        worker.resume(self._learner_id, course_id, manifest["title"])
        return True

    @property
    def learning_context(self) -> dict:
        return self._curriculum.context() if self._curriculum is not None else {}

    def _publish_curriculum(self, text: str | None = None) -> None:
        context = self.learning_context
        self.learning_context_changed.emit(context)
        if self._curriculum is not None:
            self.result_ready.emit(self._curriculum.display() if text is None else text)
            log_marker("CURRICULUM_UI", event="activity", course_id=context["course_id"],
                       chapter_id=context["chapter_id"], unit_id=context["unit_id"],
                       next_action=context["next_action"], progress=f"{context['progress']:.6f}")

    def advance_curriculum(self) -> None:
        if self._curriculum is not None:
            try:
                self._publish_curriculum(self._curriculum.advance())
            except OSError:
                self.presentation_error.emit("无法保存草稿导航位置，本次未推进。")

    def open_registered_practice(self) -> None:
        if self._curriculum_course_id:
            self.start_presentation(self._curriculum_course_id, use_curriculum=False)

    @Slot(object)
    def _on_presentation_event(self, event: object) -> None:
        check_qobject_thread("bridge_presentation_event", self)
        if self.sender() is not self._presentation_worker:
            return  # a previous course was closed while its MCP call finished
        if isinstance(event, QuestionReady):
            self._presentation_pending = PendingInteraction(
                event.learner_id, event.course_id, event.session_id, event.question_id, "",
            )
            self.question_ready.emit(event.question_id, event.display)
        elif isinstance(event, FeedbackReady):
            pending = self._presentation_pending
            if pending is None or pending.question_id != event.question_id:
                return
            self._answer_in_flight = False
            self._presentation_pending = None
            self.feedback_ready.emit(event.display)
            self.answer_submitted.emit(event.question_id)
            self._close_presentation_worker()
        elif isinstance(event, LearningError):
            self._answer_in_flight = False
            if self._presentation_pending is None:
                self._close_presentation_worker()
            self.presentation_error.emit(event.message)

    def presentation_ack(self, question_id: str, rendered_text: str) -> bool:
        """Called only after ChatView.append_assistant has returned successfully."""
        pending, worker = self._presentation_pending, self._presentation_worker
        if pending is None or worker is None or question_id != pending.question_id:
            return False
        worker.presentation_ack(question_id, rendered_text)
        return True

    # ------------------------------------------------------------ launch

    def attach_launch(self, launch_result) -> None:
        """Follow a just-launched headless INIT run.

        interactive（默认）：轮询 init.json → 取 Z Code sessionId → 写
        binding → ``--resume`` 打开课程专属 TUI。
        embedded（实验）：LearningResultWatcher 监听 result.json 并把教学
        内容投进 Firefly 聊天（Return Channel，已冻结为实验分支）。
        """
        self._curriculum = None
        self._curriculum_course_id = ""
        self.learning_context_changed.emit({})
        course_dir = launch_result.plan.workspace.parent
        task_id = launch_result.plan.task_id
        self._course_dir = course_dir
        self._pending = None
        self._result_received = False
        self._rollover_old_session_id = ""
        self._elapsed_ms = 0
        self._hinted = False
        from learning.diagnostics import log_marker

        if launch_result.plan.mode == "session-reuse":
            # Session Reuse Gate: the course's Z Code session was reopened
            # directly via --resume — nothing to watch, nothing to rebuild.
            self._task_id = task_id
            self._reset_timer()
            self._watcher = None
            log_marker(
                "LEARNING_SESSION_ATTACH",
                course_id=launch_result.plan.course_id,
                task_id=task_id,
                mode="session-reuse",
            )
            self.result_ready.emit("已请求打开 Z Code 课程会话；请确认终端中显示了当前课程。")
            return

        if launch_result.plan.mode == "session-rollover":
            # Context Budget rollover (v0.1.1): user-facing copy must never
            # mention tokens/limits — the course continues seamlessly in a
            # fresh Z Code session; teach-mcp restores the real position.
            self._task_id = task_id
            from learning.course_binding import load_binding
            self._rollover_old_session_id = str(
                (load_binding(course_dir) or {}).get("zcode_session_id", "") or ""
            )
            self._reset_timer()
            self._watcher = None
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._poll)
            self._timer.start(self._poll_ms)
            log_marker(
                "LEARNING_SESSION_ATTACH",
                course_id=launch_result.plan.course_id,
                task_id=task_id,
                mode="session-rollover",
            )
            self.result_ready.emit("学习会话已整理，继续当前课程。")
            return

        self._task_id = task_id
        self._reset_timer()
        log_marker("LEARNING_SESSION_ATTACH", course_id=launch_result.plan.course_id, task_id=task_id, mode=self._mode)
        if self._mode == MODE_EMBEDDED:
            self._watcher = LearningResultWatcher(
                course_dir, task_id, parent=self, timeout_s=self._timeout_s
            )
            self._watcher.result_received.connect(self._on_result)
            self._watcher.result_invalid.connect(
                lambda message: self.result_ready.emit(
                    f"学习代理返回了无效结果：{message}"
                )
            )
            self._watcher.still_working.connect(
                lambda task: self.agent_timeout.emit(task)
            )
            self._watcher.start()
        else:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._poll)
            self._timer.start(self._poll_ms)
        # immediate non-modal phase hint
        self.result_ready.emit("已交给学习代理，正在打开 Z Code 学习空间……")

    # ------------------------------------------------- embedded (experimental)

    def _on_result(self, result: dict) -> None:
        """EXPERIMENTAL embedded-mode result delivery (Return Channel)."""
        from learning.learning_result import display_lines

        lines = display_lines(result)
        self.result_ready.emit("\n\n".join(lines))
        question = result.get("question")
        if result.get("message_type") == "question" and isinstance(question, dict):
            session_id = str(
                (result.get("opaque_refs") or {}).get("session_id", "") or ""
            )
            self._pending = PendingInteraction(
                learner_id=self._learner_id,
                course_id=str(result["course_id"]),
                session_id=session_id,
                question_id=str(question["question_id"]),
                task_id=str(result["task_id"]),
            )
        elif result.get("message_type") != "question":
            self._pending = None

    # ------------------------------------------------------------ polling

    def _poll(self) -> None:
        if self._course_dir is None:
            return
        if self._timer is not None:
            self._elapsed_ms += self._timer.interval()
        session_id = read_init_session_id(
            self._course_dir, task_id=self._task_id,
            exclude_session_id=self._rollover_old_session_id,
            require_complete=True, require_skill_invoked=True,
        )
        if session_id is None:
            finished_without_skill = read_init_session_id(
                self._course_dir, task_id=self._task_id,
                exclude_session_id=self._rollover_old_session_id,
                require_complete=True,
            )
            if finished_without_skill is not None:
                self._timer.stop() if self._timer is not None else None
                log_marker(
                    "LEARNING_SKILL_BOOTSTRAP_FAILED",
                    course_id=self._course_dir.name,
                    task_id=self._task_id,
                )
                self.result_ready.emit("学习引导未成功启动，请重新进入当前课程。")
                return
            self._on_timeout_hint()
            return
        self._timer.stop() if self._timer is not None else None
        self._result_received = True
        # bind the course to this Z Code session (opaque UI restore ref)
        save_binding(
            self._course_dir,
            course_id=self._course_dir.name,
            zcode_session_id=session_id,
        )
        try:
            open_learning_session(self._course_dir, session_id)
        except LaunchError as exc:
            log_marker("ZCODE_TUI_OPEN_FAILED", code=exc.code, course_id=self._course_dir.name)
            self.result_ready.emit(f"Z Code 学习空间打开失败：[{exc.code}] {exc.message}")
            return
        self.result_ready.emit("已请求打开 Z Code 课程会话；请确认终端中显示了当前课程。")

    def _on_timeout_hint(self) -> None:
        if not self._hinted and self._elapsed_ms >= self._timeout_s * 1000:
            self._hinted = True
            log_marker("LEARNING_RESULT_WAIT", task_id=self._task_id, phase="timeout-hint")
            self.agent_timeout.emit(self._task_id)
            self._timer.start(self._slow_poll_ms)  # never declare failure

    def _reset_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # ------------------------------------------------------------ routing

    @property
    def has_pending_question(self) -> bool:
        return self._pending is not None or self._presentation_pending is not None

    def handle_incoming_text(self, text: str) -> str | None:
        """Route a presented TutorTurn answer before the ordinary chat pipeline."""
        if self._curriculum is not None:
            text = (text or "").strip()
            if text in EXIT_PHRASES:
                self.clear()
                return "已退出课程预览，下次进入将恢复草稿导航位置。"
            try:
                if self._curriculum.phase == "PRACTICE":
                    reply = self._curriculum.answer(text)
                elif text in {"继续", "继续学习", "下一步", "开始学习", "开始讲解", "查看例题", "开始练习", "下一题"}:
                    reply = self._curriculum.advance()
                else:
                    return "当前处于课程学习中。请点击卡片上的下一步，或输入“退出学习”返回普通聊天。"
                self._publish_curriculum(reply)
                return ""  # result_ready already appended the activity/feedback
            except OSError:
                return "无法保存草稿导航位置，本次未推进。"
        if self._presentation_pending is not None:
            return self._submit_answer_presentation((text or "").strip())
        if self._mode != MODE_EMBEDDED or self._pending is None:
            return None
        stripped = (text or "").strip()
        if any(stripped.startswith(phrase) for phrase in EXIT_PHRASES):
            self._pending = None
            return "已退出当前学习问答。学习进度仍由 teach-mcp 保存，随时可以从学习模式继续。"
        return self._submit_answer_embedded(stripped)

    def _submit_answer_presentation(self, student_answer: str) -> str:
        pending, worker = self._presentation_pending, self._presentation_worker
        if pending is None or worker is None:
            return "当前没有可回答的学习题目。"
        if any(student_answer.startswith(phrase) for phrase in EXIT_PHRASES):
            self._presentation_pending = None
            self._close_presentation_worker()
            return "已退出当前学习问答。"
        if not student_answer:
            return "请先输入你的答案。"
        if self._answer_in_flight:
            return "正在判题，请稍候……"
        self._answer_in_flight = True
        worker.submit_answer(pending.question_id, student_answer)
        return "正在判题，请稍候……"

    def _submit_answer_embedded(self, student_answer: str) -> str:
        """EXPERIMENTAL embedded-mode answer path (not the default UX)."""
        pending = self._pending
        if not student_answer:
            return "请先输入你的答案（例如 B，或一句话说明你的想法）。"
        try:
            log_marker("LEARNING_ANSWER_SUBMIT", question_id=pending.question_id)
            result = launch_learning_mode(
                pending.course_id,
                "answer",
                settings=self._settings,
                courses_root=self._courses_root,
                delivery_mode="embedded",
                interactive=False,
                answer={
                    "question_id": pending.question_id,
                    "student_answer": student_answer,
                },
            )
        except LaunchError as exc:
            return f"学习代理暂无法处理该回答：[{exc.code}] {exc.message}"
        except Exception as exc:  # noqa: BLE001 — routing boundary, report honestly
            return f"学习代理暂无法处理该回答：{exc}"
        self._pending = None
        self.attach_launch(result)
        self.answer_submitted.emit(pending.question_id)
        return "回答已提交给学习代理判题（teach-mcp），请稍候……"

    def clear(self) -> None:
        self._curriculum = None
        self._curriculum_course_id = ""
        self.learning_context_changed.emit({})
        self._reset_timer()
        self._pending = None
        self._presentation_pending = None
        self._answer_in_flight = False
        self._close_presentation_worker()
