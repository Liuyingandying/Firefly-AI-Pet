"""Light-glass workflow lifecycle card for Phase 9D.3.

Shown after ``Plan with Claude``: one compact card that tracks the
PLAN_IMPLEMENT_REVIEW workflow entirely through application-level
:class:`WorkflowEvent` (and immutable :class:`WorkflowPlan` snapshots fetched
from the coordinator). It never consumes provider stream-json, never parses
:class:`AgentEvent`, and never reads a QuickAskRunner / ProcessLauncher.

The card is a *view*: it renders the three fixed steps (Claude Plan / Codex
Implement / Claude Review) with short status copy and very light state dots.
Phase 9D.4 adds the Step-2 actions: ``Implement with Codex`` (user confirmation
of Step 2) and ``Implementation complete`` (user confirmation that the Codex
implementation is done — the only way Step 2 may succeed). When the workspace
scan finds no changes the card asks for an explicit override before an empty
CHANGED_FILES may be written. Phase 9D.5 adds the Step-3 action ``Review with
Claude`` (user confirmation of Step 3), the ``Open review`` action, and the
completion copy: Step 3 SUCCEEDED shows ``Review ready`` (never "Code approved")
and a SUCCEEDED workflow shows ``Workflow complete``. A Review step being done
never means the implementation passed. Its other actions are ``Open plan`` (a
signal the app resolves to an ArtifactStore-validated path) and ``Cancel
workflow`` (a signal the app routes to the executor or coordinator). Closing
the card only hides it — it never cancels the workflow, the executor, the
artifacts, or a detached Codex process. Hiding != cancelling.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from core.workflow_models import (
    ArtifactKind,
    WorkflowState,
    WorkflowStepIntent,
    WorkflowStepState,
)

from . import theme
from .popover_base import PopoverBase


AGENT_DISPLAY = {"claude": "Claude", "codex": "Codex", "chatgpt": "ChatGPT"}

# Step state -> tiny status-dot palette key (very light, one dot per row).
_DOT_STATE = {
    WorkflowStepState.PENDING: "unavailable",
    WorkflowStepState.AWAITING_CONFIRMATION: "waiting",
    WorkflowStepState.READY: "working",
    WorkflowStepState.RUNNING: "working",
    WorkflowStepState.SUCCEEDED: "success",
    WorkflowStepState.FAILED: "error",
    WorkflowStepState.CANCELLED: "error",
    WorkflowStepState.SKIPPED: "unavailable",
}

# Fixed status copy, owned by this UI layer. Core never emits display prose.
_FIXED_STEP_STATUS = {
    WorkflowStepState.PENDING: "Pending",
    WorkflowStepState.AWAITING_CONFIRMATION: "Waiting for confirmation",
    WorkflowStepState.FAILED: "Failed",
    WorkflowStepState.CANCELLED: "Cancelled",
    WorkflowStepState.SKIPPED: "Skipped",
}

_RUNNING_STATUS = {
    WorkflowStepIntent.PLAN: "Planning…",
    WorkflowStepIntent.IMPLEMENT: "Working in Codex",
    WorkflowStepIntent.REVIEW: "Reviewing…",
}

_SUCCEEDED_STATUS = {
    WorkflowStepIntent.PLAN: "Plan ready",
    WorkflowStepIntent.IMPLEMENT: "Implementation confirmed",
    WorkflowStepIntent.REVIEW: "Review ready",
}

_WORKFLOW_STATUS = {
    WorkflowState.CREATED: "Created",
    WorkflowState.RUNNING: "Running…",
    WorkflowState.WAITING_FOR_USER: "Waiting for confirmation",
    WorkflowState.SUCCEEDED: "Workflow complete",
    WorkflowState.FAILED: "Failed",
    WorkflowState.CANCELLED: "Cancelled",
}

_TERMINAL_STATES = frozenset(
    {WorkflowState.SUCCEEDED, WorkflowState.FAILED, WorkflowState.CANCELLED}
)
_NOTICE_MS = 3_000
_BLOCKED_MESSAGE = "A workflow is already in progress."
_NO_CHANGES_MESSAGE = "No workspace changes were detected."
_CANCEL_AFTER_HANDOFF_MESSAGE = (
    "Workflow cancelled. Codex may still be running separately."
)


def _step_status_text(step, is_current: bool, *, exec_finished: bool = False) -> str:
    state = step.state
    if state == WorkflowStepState.RUNNING:
        if step.intent == WorkflowStepIntent.IMPLEMENT and exec_finished:
            # Managed exec ended cleanly but the user has not yet confirmed
            # completion: the step is still RUNNING.
            return "Codex finished · Review changes"
        return _RUNNING_STATUS.get(step.intent, "Working…")
    if state == WorkflowStepState.SUCCEEDED:
        return _SUCCEEDED_STATUS.get(step.intent, "Completed")
    if state == WorkflowStepState.READY:
        # READY exists only right after confirm and before execute: the card
        # shows "Preparing…" so the user sees the step starting immediately.
        return "Preparing…" if is_current else "Ready"
    return _FIXED_STEP_STATUS.get(state, "…")


class _StepRow(QWidget):
    """One workflow step: dot + agent + title + short status."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACE_SM)
        self._dot = theme.StatusDot("unavailable")
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._agent = QLabel("")
        self._agent.setStyleSheet(theme.primary_label_style(size=9))
        layout.addWidget(self._agent, 0, Qt.AlignVCenter)
        self._title = QLabel("")
        self._title.setStyleSheet(theme.secondary_label_style(size=8))
        layout.addWidget(self._title, 0, Qt.AlignVCenter)
        layout.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet(theme.secondary_label_style(size=8))
        self._status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self._status, 0, Qt.AlignVCenter)

    def set_step(self, step, is_current: bool, *, exec_finished: bool = False) -> None:
        if step is None:
            self._dot.set_state("unavailable")
            self._agent.setText("")
            self._title.setText("")
            self._status.setText("")
            return
        self._dot.set_state(_DOT_STATE.get(step.state, "unavailable"))
        self._agent.setText(AGENT_DISPLAY.get(step.agent_id, step.agent_id.title()))
        self._title.setText(step.title or step.intent.value)
        self._status.setText(_step_status_text(step, is_current, exec_finished=exec_finished))


class WorkflowCard(PopoverBase):
    """Frameless light-glass card that renders one workflow's lifecycle.

    Consumes only application-level workflow semantics: WorkflowEvent
    notifications plus immutable WorkflowPlan snapshots from the coordinator.
    It never renders internal ids, paths, or provider tokens.
    """

    open_plan_requested = Signal(str)  # workflow_id
    open_review_requested = Signal(str)  # workflow_id (Open review)
    cancel_workflow_requested = Signal(str)  # workflow_id
    implement_with_codex_requested = Signal(str)  # workflow_id (Step 2 confirm)
    implementation_complete_requested = Signal(str)  # workflow_id (Step 2 done)
    implementation_override_requested = Signal(str)  # workflow_id (empty diff override)
    review_with_claude_requested = Signal(str)  # workflow_id (Step 3 confirm)

    def __init__(self, *, coordinator=None, width: int = theme.POPOVER_WIDTH, parent=None):
        super().__init__(width=width, parent=parent)
        self.setWindowTitle("Firefly Workflow")
        self._coordinator = coordinator
        self._workflow_id: str | None = None
        self._plan = None
        self._blocked_message = ""
        self._notice = ""
        self._no_changes_pending = False
        self._exec_finished = False
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._clear_notice)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_SM)
        self._title = QLabel("Workflow")
        self._title.setStyleSheet(theme.primary_label_style(size=11))
        header.addWidget(self._title, 1, Qt.AlignVCenter)
        self._close_btn = QPushButton("×")
        self._close_btn.setObjectName("workflowClose")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setStyleSheet(theme.link_button_style("workflowClose"))
        self._close_btn.clicked.connect(self.dismiss)
        header.addWidget(self._close_btn, 0, Qt.AlignVCenter)
        self.content_layout.addLayout(header)

        self._status = QLabel("")
        self._status.setStyleSheet(theme.secondary_label_style())
        self._status.setWordWrap(True)
        self.content_layout.addWidget(self._status)

        self._rows = [_StepRow() for _ in range(3)]
        for row in self._rows:
            self.content_layout.addWidget(row)

        actions2 = QHBoxLayout()
        actions2.setContentsMargins(0, 0, 0, 0)
        actions2.setSpacing(theme.SPACE_XS)
        actions2.addStretch(1)
        self._implement_btn = QPushButton("Implement with Codex")
        self._implement_btn.setObjectName("workflowImplement")
        self._implement_btn.setCursor(Qt.PointingHandCursor)
        self._implement_btn.setStyleSheet(theme.popover_button_style("workflowImplement"))
        self._implement_btn.setVisible(False)
        self._implement_btn.clicked.connect(self._on_implement)
        actions2.addWidget(self._implement_btn)
        self._complete_btn = QPushButton("Implementation complete")
        self._complete_btn.setObjectName("workflowComplete")
        self._complete_btn.setCursor(Qt.PointingHandCursor)
        self._complete_btn.setStyleSheet(theme.popover_button_style("workflowComplete"))
        self._complete_btn.setVisible(False)
        self._complete_btn.clicked.connect(self._on_complete)
        actions2.addWidget(self._complete_btn)
        self._keep_waiting_btn = QPushButton("Keep waiting")
        self._keep_waiting_btn.setObjectName("workflowKeepWaiting")
        self._keep_waiting_btn.setCursor(Qt.PointingHandCursor)
        self._keep_waiting_btn.setStyleSheet(theme.link_button_style("workflowKeepWaiting"))
        self._keep_waiting_btn.setVisible(False)
        self._keep_waiting_btn.clicked.connect(self._on_keep_waiting)
        actions2.addWidget(self._keep_waiting_btn)
        self._override_btn = QPushButton("Mark complete anyway")
        self._override_btn.setObjectName("workflowOverride")
        self._override_btn.setCursor(Qt.PointingHandCursor)
        self._override_btn.setStyleSheet(theme.popover_button_style("workflowOverride"))
        self._override_btn.setVisible(False)
        self._override_btn.clicked.connect(self._on_override)
        actions2.addWidget(self._override_btn)
        self._review_btn = QPushButton("Review with Claude")
        self._review_btn.setObjectName("workflowReview")
        self._review_btn.setCursor(Qt.PointingHandCursor)
        self._review_btn.setStyleSheet(theme.popover_button_style("workflowReview"))
        self._review_btn.setVisible(False)
        self._review_btn.clicked.connect(self._on_review)
        actions2.addWidget(self._review_btn)
        self.content_layout.addLayout(actions2)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(theme.SPACE_XS)
        actions.addStretch(1)
        self._cancel_btn = QPushButton("Cancel workflow")
        self._cancel_btn.setObjectName("workflowCancel")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.setStyleSheet(theme.link_button_style("workflowCancel"))
        self._cancel_btn.setVisible(False)
        self._cancel_btn.clicked.connect(self._on_cancel)
        actions.addWidget(self._cancel_btn)
        self._open_btn = QPushButton("Open plan")
        self._open_btn.setObjectName("workflowOpen")
        self._open_btn.setCursor(Qt.PointingHandCursor)
        self._open_btn.setStyleSheet(theme.popover_button_style("workflowOpen"))
        self._open_btn.setVisible(False)
        self._open_btn.clicked.connect(self._on_open)
        actions.addWidget(self._open_btn)
        self._open_review_btn = QPushButton("Open review")
        self._open_review_btn.setObjectName("workflowOpenReview")
        self._open_review_btn.setCursor(Qt.PointingHandCursor)
        self._open_review_btn.setStyleSheet(theme.popover_button_style("workflowOpenReview"))
        self._open_review_btn.setVisible(False)
        self._open_review_btn.clicked.connect(self._on_open_review)
        actions.addWidget(self._open_review_btn)
        self.content_layout.addLayout(actions)

    # -- public ----------------------------------------------------------

    @property
    def workflow_id(self) -> str | None:
        return self._workflow_id

    @property
    def plan(self):
        return self._plan

    @property
    def has_active_workflow(self) -> bool:
        if self._plan is None:
            return False
        return self._plan.state not in _TERMINAL_STATES

    def show_workflow(self, plan) -> None:
        """Show the card for a freshly confirmed workflow (Step 1 READY)."""
        self._workflow_id = plan.workflow_id
        self._plan = plan
        self._blocked_message = ""
        self._notice = ""
        self._no_changes_pending = False
        self._exec_finished = False
        self._stop_notice()
        self._refresh()
        self.adjustSize()
        self.show()
        self.raise_()

    def show_in_progress(self, plan) -> None:
        """Re-show the active workflow with a "already in progress" banner."""
        self._workflow_id = plan.workflow_id
        self._plan = plan
        self._blocked_message = _BLOCKED_MESSAGE
        self._notice = ""
        self._no_changes_pending = False
        self._exec_finished = False
        self._stop_notice()
        self._refresh()
        self.adjustSize()
        self.show()
        self.raise_()

    def resume_show(self) -> None:
        """Re-show after a hide (recovery): state is preserved, never cleared."""
        self.adjustSize()
        self.show()
        self.raise_()

    def hide_card(self) -> None:
        """Hide only. Never cancels the workflow/executor/artifacts."""
        self.hide()

    def on_workflow_event(self, event) -> None:
        """Refresh from the coordinator's new immutable plan snapshot.

        Ignores events for workflows this card is not tracking, so a stale
        event can never paint the wrong workflow.
        """
        if self._workflow_id is None or event.workflow_id != self._workflow_id:
            return
        if self._coordinator is not None:
            plan = self._coordinator.get_plan(event.workflow_id)
            if plan is not None:
                self._plan = plan
        self._notice = ""
        self._stop_notice()
        self._refresh()
        if self.isVisible():
            self.adjustSize()

    def on_open_plan_failed(self, message: str) -> None:
        self._notice = message
        self._no_changes_pending = False
        self._refresh()
        self._notice_timer.start(_NOTICE_MS)

    def on_open_review_failed(self, message: str) -> None:
        self._notice = message
        self._no_changes_pending = False
        self._refresh()
        self._notice_timer.start(_NOTICE_MS)

    def show_notice(self, message: str) -> None:
        """Transient status notice (auto-cleared)."""
        self._notice = message
        self._no_changes_pending = False
        self._refresh()
        self._notice_timer.start(_NOTICE_MS)

    def on_implement_execution_finished(self) -> None:
        """The managed Codex exec ended cleanly; Step 2 is still RUNNING.

        The step only succeeds on an explicit user confirmation, so the card
        switches from "Working in Codex" to "Codex finished · Review changes"
        and exposes the Implementation complete action.
        """
        self._exec_finished = True
        self._notice = ""
        self._no_changes_pending = False
        self._stop_notice()
        self._refresh()
        if self.isVisible():
            self.adjustSize()

    def on_completion_result(self, attempt) -> None:
        """Surface one Implement completion attempt to the Step-2 actions."""
        if attempt.ok:
            self._no_changes_pending = False
            self._notice = ""
            self._stop_notice()
            self._refresh()
            return
        if attempt.no_changes:
            # Persistent until the user chooses Keep waiting or overrides.
            self._no_changes_pending = True
            self._notice = _NO_CHANGES_MESSAGE
            self._stop_notice()
            self._refresh()
            return
        # Failure / snapshot-error: keep the complete action available to retry.
        self._no_changes_pending = False
        self._notice = attempt.message or "Couldn't finish the implementation step."
        self._complete_btn.setEnabled(True)
        self._refresh()
        self._notice_timer.start(_NOTICE_MS)

    def enable_implement_action(self) -> None:
        """Re-enable the Step-2 confirmation after a failed start attempt."""
        self._implement_btn.setEnabled(True)

    def enable_review_action(self) -> None:
        """Re-enable the Step-3 confirmation after a failed start attempt."""
        self._review_btn.setEnabled(True)

    def dismiss(self) -> None:
        """Close = hide only. The workflow keeps running and stays resumable."""
        if self.isVisible():
            self.hide()

    # -- rendering ------------------------------------------------------

    def _refresh(self) -> None:
        plan = self._plan
        if plan is None:
            self._status.setText("")
            for row in self._rows:
                row.set_step(None, False)
            for name in (
                "_open_btn", "_open_review_btn", "_cancel_btn", "_implement_btn",
                "_complete_btn", "_keep_waiting_btn", "_override_btn", "_review_btn",
            ):
                getattr(self, name).setVisible(False)
            return
        if self._blocked_message:
            self._status.setText(self._blocked_message)
        elif self._notice:
            self._status.setText(self._notice)
        else:
            self._status.setText(_WORKFLOW_STATUS.get(plan.state, "…"))
        for index, row in enumerate(self._rows):
            if index < len(plan.steps):
                step = plan.steps[index]
                row.set_step(
                    step,
                    index == plan.current_step_index,
                    exec_finished=(
                        self._exec_finished
                        and index == 1
                        and step.intent == WorkflowStepIntent.IMPLEMENT
                    ),
                )
            else:
                row.set_step(None, False)
        has_plan = any(
            a.kind == ArtifactKind.PLAN
            for s in plan.steps
            for a in s.attached_artifacts
        )
        has_review = any(
            a.kind == ArtifactKind.REVIEW
            for s in plan.steps
            for a in s.attached_artifacts
        )
        self._open_btn.setVisible(has_plan)
        self._open_review_btn.setVisible(has_review)
        self._cancel_btn.setVisible(plan.state not in _TERMINAL_STATES)
        self._refresh_step2_actions(plan)
        self._refresh_step3_actions(plan)

    def _refresh_step2_actions(self, plan) -> None:
        self._implement_btn.setVisible(False)
        self._complete_btn.setVisible(False)
        self._keep_waiting_btn.setVisible(False)
        self._override_btn.setVisible(False)
        if plan.state in _TERMINAL_STATES:
            return
        if len(plan.steps) < 2:
            return
        step2 = plan.steps[1]
        if step2.intent != WorkflowStepIntent.IMPLEMENT:
            return
        if step2.state == WorkflowStepState.AWAITING_CONFIRMATION:
            self._implement_btn.setVisible(True)
            self._implement_btn.setEnabled(True)
        elif step2.state == WorkflowStepState.RUNNING:
            # During the managed exec there is no completion action; only after
            # the exec finishes cleanly does "Implementation complete" appear.
            if not self._exec_finished:
                return
            if self._no_changes_pending:
                self._keep_waiting_btn.setVisible(True)
                self._override_btn.setVisible(True)
            else:
                self._complete_btn.setVisible(True)
                self._complete_btn.setEnabled(True)

    def _refresh_step3_actions(self, plan) -> None:
        self._review_btn.setVisible(False)
        if plan.state in _TERMINAL_STATES:
            return
        if len(plan.steps) < 3:
            return
        step3 = plan.steps[2]
        if step3.intent != WorkflowStepIntent.REVIEW:
            return
        if step3.state == WorkflowStepState.AWAITING_CONFIRMATION:
            self._review_btn.setVisible(True)
            self._review_btn.setEnabled(True)

    def _clear_notice(self) -> None:
        self._notice = ""
        self._no_changes_pending = False
        self._refresh()

    def _stop_notice(self) -> None:
        self._notice_timer.stop()

    def _on_open(self) -> None:
        if self._workflow_id is not None:
            self.open_plan_requested.emit(self._workflow_id)

    def _on_open_review(self) -> None:
        if self._workflow_id is not None:
            self.open_review_requested.emit(self._workflow_id)

    def _on_cancel(self) -> None:
        if self._workflow_id is not None:
            self.cancel_workflow_requested.emit(self._workflow_id)

    def _on_implement(self) -> None:
        if self._workflow_id is None:
            return
        self._implement_btn.setEnabled(False)  # double-click guard
        self.implement_with_codex_requested.emit(self._workflow_id)

    def _on_complete(self) -> None:
        if self._workflow_id is None:
            return
        self._complete_btn.setEnabled(False)  # double-click guard
        self.implementation_complete_requested.emit(self._workflow_id)

    def _on_keep_waiting(self) -> None:
        self._no_changes_pending = False
        self._notice = ""
        self._stop_notice()
        self._refresh()

    def _on_override(self) -> None:
        if self._workflow_id is None:
            return
        self._override_btn.setEnabled(False)  # double-click guard
        self.implementation_override_requested.emit(self._workflow_id)

    def _on_review(self) -> None:
        if self._workflow_id is None:
            return
        self._review_btn.setEnabled(False)  # double-click guard
        self.review_with_claude_requested.emit(self._workflow_id)
