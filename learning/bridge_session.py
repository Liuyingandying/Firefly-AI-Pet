"""Bridge learning session (Interactive Z Code Learning, v0.1 纠偏).

默认模式（interactive）：Firefly 点击「开始/继续学习」→ headless init run
执行 bootstrap（skill + teach-mcp）→ 从 --json 官方输出提取 Z Code
sessionId → 写入 binding.zcode_session_id（opaque UI 恢复引用）→ 用
``--resume <sessionId>`` 打开**课程专属**可交互 Z Code TUI。教学内容、
题目与回答全部发生在 Z Code；Firefly 聊天保持普通聊天能力。

``mode="embedded"`` 为实验分支（Return Channel 的 result.json → 聊天框 +
回答拦截），默认不启用；学习状态唯一权威始终是 teach-mcp。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from learning.course_binding import save_binding
from learning.diagnostics import log_marker
from learning.launcher import (
    LaunchError,
    launch_learning_mode,
    open_learning_session,
    read_init_session_id,
)
from learning.result_watcher import LearningResultWatcher

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

    # ------------------------------------------------------------ launch

    def attach_launch(self, launch_result) -> None:
        """Follow a just-launched headless INIT run.

        interactive（默认）：轮询 init.json → 取 Z Code sessionId → 写
        binding → ``--resume`` 打开课程专属 TUI。
        embedded（实验）：LearningResultWatcher 监听 result.json 并把教学
        内容投进 Firefly 聊天（Return Channel，已冻结为实验分支）。
        """
        course_dir = launch_result.plan.workspace.parent
        task_id = launch_result.plan.task_id
        self._course_dir = course_dir
        self._pending = None
        self._result_received = False
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
            self._reset_timer()
            self._watcher = None
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
        session_id = read_init_session_id(self._course_dir)
        if session_id is None:
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
        return self._pending is not None

    def handle_incoming_text(self, text: str) -> str | None:
        """Chat-routing gate. Interactive mode keeps normal chat: only the
        experimental embedded mode intercepts pending-question answers."""
        if self._mode != MODE_EMBEDDED or self._pending is None:
            return None
        stripped = (text or "").strip()
        if any(stripped.startswith(phrase) for phrase in EXIT_PHRASES):
            self._pending = None
            return "已退出当前学习问答。学习进度仍由 teach-mcp 保存，随时可以从学习模式继续。"
        return self._submit_answer_embedded(stripped)

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
        self._reset_timer()
        self._pending = None
