"""Firefly Phase 8A.2 visual shell composition root.

The application keeps the proven state broker and animation contract while
presenting Firefly as a small constellation of character-anchored overlays.
The legacy CompanionPanel remains on disk but is not imported or shown.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

from core.agent_router import AgentRouter
from core.artifact_store import ArtifactStore
from core.handoff import (
    HandoffMetrics,
    HandoffTelemetry,
    HandoffState,
    handoff_hash,
    workspace_token,
)
from core.keep_awake import KeepAwakeService
from core.models import AgentState, ResolvedState
from core.notification_manager import NotificationManager
from core.quick_ask_metrics import MetricsWriter
from core.routing_models import HandoffMode, TaskRequest
from core.session_manager import SessionManager
from core.session_store import SessionStore
from core.settings_manager import SettingsManager
from core.state_monitor import StateMonitor
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
from core.workflow_models import (
    ArtifactKind,
    WorkflowEventType,
    WorkflowState,
    WorkflowStepIntent,
)
from core.workspace_manager import WorkspaceManager
from ui import theme
from ui.agent_dock import AgentDock
from ui.overlay_coordinator import OverlayCoordinator
from ui.permission_card import PermissionCard
from ui.pet_overlay import PetOverlay
from ui.process_launcher import ProcessLauncher, QuickAskRunner
from ui.quick_chat_protocol import classify_short_ask
from ui.recommendation_card import RecommendationCard
from ui.session_popover import SessionPopover
from ui.settings_popover import SettingsPopover
from ui.short_ask import AskPill, ShortAskPanel
from ui.speech_bubble import SpeechBubble
from ui.vertical_toolbar import VerticalToolbar
from ui.workflow_card import WorkflowCard
from ui.workflow_executor import PlanStepExecutor
from ui.workflow_implement_executor import ImplementStepExecutor
from ui.workspace_store import WorkspaceStore
from ui.workflow_review_executor import ReviewStepExecutor
from ui.workspace_popover import WorkspacePopover


PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets" / "animations"
RUNTIME_DIR = PROJECT_DIR / "runtime"
CONFIG_DIR = PROJECT_DIR / "config"
SESSIONS_FILE = CONFIG_DIR / "sessions.json"
STATE_FILE = RUNTIME_DIR / "state.json"
PID_FILE = RUNTIME_DIR / "pet.pid"
SERVER_NAME = "FireflyAIPet-SingleInstance"
MUTEX_NAME = "FireflyAIPet-SingleInstance-Mutex"
ERROR_ALREADY_EXISTS = 183
STATE_GIF = {
    "idle": "idle.gif",
    "thinking": "review.gif",
    "working": "running.gif",
    "waiting": "waiting.gif",
    "success": "waving.gif",
    "error": "failed.gif",
    "sleeping": "idle.gif",
}


class VisualShell(QObject):
    """Compose the visual islands with the lifecycle monitor."""

    def __init__(
        self,
        server: QLocalServer | None,
        *,
        sessions_file: Path | str | None = None,
        artifact_root: Path | str | None = None,
        workspace_settings_file: Path | str | None = None,
    ):
        super().__init__()
        self._server = server
        self._shutting_down = False

        self.pet = PetOverlay(ASSETS_DIR, STATE_GIF, max_dimension=theme.PET_MAX_DIMENSION)
        self.dock = AgentDock()
        self.bubble = SpeechBubble()
        self.toolbar = VerticalToolbar()
        self.workspace_manager = (
            WorkspaceManager(store=WorkspaceStore(workspace_settings_file))
            if workspace_settings_file is not None
            else WorkspaceManager()
        )
        if sessions_file is not None:
            self.session_manager = SessionManager(store=SessionStore(sessions_file))
        else:
            self.session_manager = SessionManager()
        self.settings = SettingsManager()
        self.workspace_popover = WorkspacePopover(self.workspace_manager)
        self.session_popover = SessionPopover(self.session_manager, self.workspace_manager)
        self.permission_card = PermissionCard()
        self.settings_popover = SettingsPopover(self.settings)
        self.ask_pill = AskPill()
        self.short_ask = ShortAskPanel()
        self.recommendation_card = RecommendationCard()
        self.workflow_coordinator = WorkflowCoordinator()
        self.artifact_store = (
            ArtifactStore(artifact_root) if artifact_root is not None else ArtifactStore()
        )
        self.plan_executor = PlanStepExecutor(
            self.workflow_coordinator, self.artifact_store, parent=self
        )
        self.implement_executor = ImplementStepExecutor(
            self.workflow_coordinator, self.artifact_store, parent=self
        )
        self.review_executor = ReviewStepExecutor(
            self.workflow_coordinator, self.artifact_store, parent=self
        )
        self.workflow_card = WorkflowCard(coordinator=self.workflow_coordinator)
        self._active_workflow_id: str | None = None
        self.agent_router = AgentRouter()
        self.coordinator = OverlayCoordinator(
            self.pet,
            self.dock,
            self.bubble,
            self.toolbar,
            self,
            workspace_popover=self.workspace_popover,
            session_popover=self.session_popover,
            permission_card=self.permission_card,
            settings_popover=self.settings_popover,
            settings_manager=self.settings,
            ask_pill=self.ask_pill,
            short_ask=self.short_ask,
            recommendation_card=self.recommendation_card,
            workflow_card=self.workflow_card,
        )
        self.state_monitor = StateMonitor(
            RUNTIME_DIR / "sources",
            STATE_FILE,
            parent=self,
        )
        self.notification_manager = NotificationManager()
        self.keep_awake = KeepAwakeService()
        self.notification_manager.set_enabled(self.settings.notifications_enabled)
        self.keep_awake.set_enabled(self.settings.keep_awake_enabled)
        self.settings.connect(self._on_settings_changed)
        self.notification_manager.connect(self.coordinator.on_notification)

        self.quick_ask = QuickAskRunner(session_manager=self.session_manager, parent=self)
        self.ask_metrics = MetricsWriter()
        self.handoff_metrics = HandoffMetrics()
        self._short_ask_turn_failed = False
        self._last_short_ask_prompt = ""
        self._resume_fallbacks_this_cycle = 0
        self._scale_mode = False
        self._scale_save_timer = QTimer(self)
        self._scale_save_timer.setSingleShot(True)
        self._scale_save_timer.setInterval(400)
        self._scale_save_timer.timeout.connect(self._persist_ui_scale)

        self.pet.quit_requested.connect(QApplication.quit)
        self.pet.scale_mode_toggled.connect(self._on_scale_mode_toggled)
        self.pet.scale_wheel.connect(self._on_scale_wheel)
        self.pet.scale_exit_requested.connect(self._on_scale_exit)
        self.dock.agent_selected.connect(self._on_dock_agent_selected)
        self.state_monitor.agent_state_changed.connect(self._on_agent_state_changed)
        self.state_monitor.resolved_state_changed.connect(self._on_resolved_state_changed)
        self.session_popover.continue_requested.connect(self._on_continue_session)
        self.session_popover.new_requested.connect(self._on_new_session)
        self.coordinator.permission_view_requested.connect(self._on_permission_view)
        self.coordinator.short_ask_requested.connect(self._on_short_ask_requested)
        self.short_ask.send_requested.connect(self._on_short_ask_send)
        self.short_ask.force_send_requested.connect(self._on_short_ask_force_send)
        self.short_ask.retry_requested.connect(self._on_short_ask_retry)
        self.short_ask.stop_requested.connect(self._on_short_ask_stop)
        self.short_ask.open_agent_requested.connect(self._on_short_ask_open_agent)
        self.recommendation_card.send_requested.connect(self._on_recommendation_send)
        self.recommendation_card.open_native_requested.connect(self._on_recommendation_open_native)
        self.recommendation_card.plan_with_claude_requested.connect(
            self._on_recommendation_plan_with_claude
        )
        self.workflow_card.open_plan_requested.connect(self._on_workflow_open_plan)
        self.workflow_card.open_review_requested.connect(self._on_workflow_open_review)
        self.workflow_card.cancel_workflow_requested.connect(self._on_workflow_cancel)
        self.workflow_card.implement_with_codex_requested.connect(self._on_workflow_implement)
        self.workflow_card.implementation_complete_requested.connect(
            self._on_workflow_implement_complete
        )
        self.workflow_card.implementation_override_requested.connect(
            lambda workflow_id: self._on_workflow_implement_complete(workflow_id, override=True)
        )
        self.workflow_card.review_with_claude_requested.connect(self._on_workflow_review)
        self.implement_executor.execution_finished.connect(self._on_implement_execution_finished)
        self.workflow_coordinator.connect(self._on_workflow_event)
        self.workspace_manager.connect(self._on_workspace_changed)
        # The Short Talk panel consumes neutral AgentEvents only.
        self.quick_ask.agent_event.connect(self.short_ask.on_agent_event)
        self.quick_ask.finished.connect(self._on_short_ask_finished)
        self.quick_ask.failed.connect(self._on_short_ask_failed)
        self.quick_ask.telemetry.connect(self._on_quick_ask_telemetry)
        if self._server is not None:
            self._server.newConnection.connect(self._on_control_connection)

    def start(self) -> None:
        self._write_pid()
        # Restore persisted native sessions so Short Talk resumes across a
        # Firefly restart. Safe even before the UI is shown.
        self.session_manager.load()
        self.state_monitor.start()
        # Apply the persisted ui_scale before the first paint (no 100% flash).
        theme.set_ui_scale(self.settings.ui_scale)
        self.coordinator.show_shell()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.state_monitor.stop()
        self.keep_awake.shutdown()
        self.quick_ask.shutdown()
        self._persist_ui_scale()
        if self.plan_executor.running:
            self.plan_executor.stop()
        if self.review_executor.running:
            self.review_executor.stop()
        if self.implement_executor.running:
            self.implement_executor.stop()
        self.implement_executor.reset()
        self.coordinator.close_overlays()
        self.pet.shutdown()
        if self._server is not None:
            self._server.close()
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def _on_agent_state_changed(self, agent_id: str, state: AgentState) -> None:
        self.dock.set_agent_state(agent_id, state.state.value)
        self.session_popover.set_agent_state(agent_id, state.state.value)
        self.coordinator.on_agent_state(agent_id, state)
        self.notification_manager.on_agent_state(state)
        self.keep_awake.on_agent_state(state)

    def _on_dock_agent_selected(self, agent_id: str) -> None:
        # ChatGPT has no in-app backend: a dock click opens the web UI directly.
        # Claude/Codex keep their selection-only dock behavior — the Ask entry
        # drives Quick Ask, unchanged.
        if agent_id == "chatgpt":
            self._open_chatgpt()

    def _open_chatgpt(self) -> None:
        ok, _ = ProcessLauncher.open_chatgpt()
        if not ok:
            try:
                self.bubble.show_message(
                    "ChatGPT",
                    "Couldn't open ChatGPT.",
                    duration_ms=3_200,
                    accent=theme.ERROR_STATUS,
                )
            except Exception:
                pass

    def _on_resolved_state_changed(self, state: ResolvedState) -> None:
        self.pet.apply_state(state.state.value)

    # -- UI Scale Mode ---------------------------------------------------

    def _on_scale_mode_toggled(self) -> None:
        self._scale_mode = not self._scale_mode
        if self._scale_mode:
            self._show_scale_notice()
            self.pet.setFocus()
        else:
            self._scale_save_timer.stop()
            self._persist_ui_scale()

    def _on_scale_exit(self) -> None:
        if not self._scale_mode:
            return
        self._scale_mode = False
        self._scale_save_timer.stop()
        self._persist_ui_scale()

    def _on_scale_wheel(self, direction: int) -> None:
        if not self._scale_mode:
            return
        theme.set_ui_scale(theme.ui_scale() + direction * theme.SCALE_STEP)
        self._show_scale_notice()
        self._scale_save_timer.start()

    def _show_scale_notice(self) -> None:
        percent = round(theme.ui_scale() * 100)
        self.bubble.show_message("Scale", f"{percent}%", duration_ms=1_200)

    def _persist_ui_scale(self) -> None:
        self.settings.set_ui_scale(theme.ui_scale())

    def _on_settings_changed(self, _prefs) -> None:
        self.notification_manager.set_enabled(self.settings.notifications_enabled)
        self.keep_awake.set_enabled(self.settings.keep_awake_enabled)

    def _on_permission_view(self, agent_id: str) -> None:
        ProcessLauncher.launch_agent(agent_id, self.workspace_manager.current())

    def _on_continue_session(self, agent_id: str) -> None:
        ProcessLauncher.launch_agent(agent_id, self.workspace_manager.current())
        self.session_popover.dismiss()

    def _on_new_session(self, agent_id: str) -> None:
        self.session_manager.clear(agent_id, self.workspace_manager.current())
        ProcessLauncher.launch_agent(agent_id, self.workspace_manager.current())
        self.session_popover.dismiss()

    # -- Short Talk -----------------------------------------------------

    def _on_short_ask_requested(self) -> None:
        if self.coordinator.permission_card is not None and self.coordinator.permission_card.isVisible():
            return
        # A hidden pending recommendation (overlay conflict) is restored on the
        # next Ask instead of starting over (Phase 9B section 21/22).
        if self.recommendation_card.has_pending:
            self.coordinator.show_recommendation()
            return
        # A hidden/live turn is resumed so the user sees its current state
        # instead of starting over (Phase 8C.3 section 18).
        if self.short_ask.has_pending_state():
            self.coordinator.show_short_ask()
            return
        agent = self.dock.selected_agent
        workspace = self.workspace_manager.current()
        if agent == "codex":
            # Codex Short Talk is ephemeral single-turn (read-only): no resume.
            self.short_ask.show_input("codex", resume=False)
            self.coordinator.show_short_ask()
            return
        if agent == "chatgpt":
            self.short_ask.show_notice(
                "chatgpt",
                "Direct ChatGPT talk isn't available yet.",
                open_label="Open ChatGPT",
            )
            self.coordinator.show_short_ask()
            return
        resuming = self.session_manager.has(agent, workspace)
        self.short_ask.show_input("claude", resume=resuming)
        self.coordinator.show_short_ask()

    def _task_request(self, prompt: str, requested_agent: str | None = None) -> TaskRequest:
        """Build the router request. ``requested_agent`` is only set when the
        panel was opened for an explicit non-default agent (Ask Codex), so the
        router honors the user's choice; otherwise it stays free to recommend
        the best agent for the task."""
        return TaskRequest(
            text=prompt,
            workspace=str(self.workspace_manager.current()),
            requested_agent=requested_agent,
        )

    def _on_short_ask_send(self, prompt: str) -> None:
        if self.short_ask.running:
            return  # never launch a second concurrent internal ask
        self._resume_fallbacks_this_cycle = 0
        # Ask Codex is an explicit agent choice; Ask Claude stays prompt-routed.
        requested = self.short_ask.agent if self.short_ask.agent == "codex" else None
        top = self.agent_router.recommend(self._task_request(prompt, requested_agent=requested))[0]
        if top.handoff_mode == HandoffMode.SHORT_TALK:
            self._short_talk(top.agent_id, prompt)
            return
        # OPEN_NATIVE and UNAVAILABLE both render on the recommendation card;
        # the card only acts on an explicit user click (never auto-launch).
        self.recommendation_card.show_recommendation(top, prompt, str(self.workspace_manager.current()))
        self.coordinator.show_recommendation()

    def _short_talk(self, agent: str, prompt: str) -> None:
        """Route a prompt into the managed Short Talk path for ``agent``.

        The router already decided this agent + SHORT_TALK. Claude additionally
        runs the long-task steer (Phase 9B): a complex prompt reroutes to the
        native Claude surface instead of a lightweight read-only ask.
        """
        if self.short_ask.running:
            return
        self._resume_fallbacks_this_cycle = 0
        if agent == "claude" and classify_short_ask(prompt) == "complex":
            self.short_ask.show_recommendation(
                "claude",
                "This looks like a longer task. Open Claude instead?",
                open_label=self.short_ask.open_label(),
                prompt=prompt,
            )
        else:
            self._do_short_ask(agent, prompt)
        self.coordinator.show_short_ask()

    def _on_recommendation_send(
        self, agent_id: str, workspace: str, prompt: str
    ) -> None:
        """User confirmed ``Send to <agent>``: hand the original task to the
        native Agent in the workspace locked at recommendation creation time.

        Only this click executes the handoff (user confirmation is the single
        execution gate). The workspace lock is double-checked against the live
        workspace; if it drifted, the pending handoff is cancelled instead of
        being silently delivered to the wrong workspace. Success here means the
        native process started and the payload was handed over — never task
        completion.
        """
        if workspace != str(self.workspace_manager.current()):
            self.recommendation_card.on_handoff_result(False, "workspace changed")
            return
        try:
            ok, message = ProcessLauncher.launch_agent(
                agent_id, workspace, initial_prompt=prompt
            )
        except Exception as exc:  # never leave the card stuck in LAUNCHING
            ok, message = False, f"launch error: {exc}"
        self.recommendation_card.on_handoff_result(ok, message)
        self._record_handoff_metric(agent_id, workspace, ok, message)

    def _record_handoff_metric(self, agent_id: str, workspace: str, ok: bool, message: str) -> None:
        """Minimal allowlisted handoff telemetry. Never contains the prompt."""
        request = self.recommendation_card.handoff
        if request is None:
            return
        duration = None
        if request.created_at:
            duration = max(0, int(time.time() * 1000) - request.created_at)
        state = (HandoffState.HANDED_OFF if ok else HandoffState.FAILED).value
        self.handoff_metrics.record(
            HandoffTelemetry(
                handoff_hash=handoff_hash(request.handoff_id),
                agent=request.agent_id,
                workspace=workspace_token(request.workspace),
                created_at=request.created_at,
                duration_ms=duration,
                state=state,
            )
        )

    def _on_recommendation_open_native(self, agent_id: str, workspace: str) -> None:
        """Open the recommended agent's native surface in the workspace that was
        locked at recommendation creation time. No prompt, no handoff."""
        if agent_id == "chatgpt":
            ProcessLauncher.open_chatgpt()
        else:
            ProcessLauncher.launch_agent(agent_id, workspace)

    # -- Workflow (Plan with Claude) ------------------------------------

    def _on_recommendation_plan_with_claude(self, prompt: str, workspace: str) -> None:
        """User confirmed Step 1 (Claude Plan) by clicking ``Plan with Claude``.

        The click is the user's explicit confirmation of workflow Step 1. This
        application layer keeps confirm and execute as two separate calls:
        ``confirm_step`` only advances the coordinator to READY; the executor
        consumes READY afterwards. Everything uses the prompt + workspace
        locked at recommendation creation time — never a re-read of the input
        or the live workspace.
        """
        if workspace != str(self.workspace_manager.current()):
            # Workspace drift: cancel the pending recommendation and prompt a
            # resubmit. Never create a workflow against the wrong workspace.
            self.recommendation_card.clear_pending()
            self._show_workflow_notice("Workspace changed. Please resubmit.")
            return
        active = self._active_workflow_plan()
        if active is not None:
            # One active workflow at a time: no second workflow is created.
            self.recommendation_card.clear_pending()
            self.workflow_card.show_in_progress(active)
            self.coordinator.show_workflow()
            return
        request = TaskRequest(text=prompt, workspace=workspace)
        try:
            plan = self.workflow_coordinator.create_plan_implement_review(request)
        except WorkflowTransitionError as exc:
            self.recommendation_card.clear_pending()
            self._show_workflow_notice("Couldn't create a workflow.")
            return
        try:
            plan = self.workflow_coordinator.confirm_step(plan, plan.steps[0].step_id)
        except WorkflowTransitionError:
            self.recommendation_card.clear_pending()
            self._show_workflow_notice("Couldn't start the workflow.")
            return
        self.recommendation_card.clear_pending()
        self.workflow_card.show_workflow(plan)
        self.coordinator.show_workflow()
        try:
            self.plan_executor.execute(plan, plan.steps[0].step_id)
        except Exception:
            self.workflow_card.on_open_plan_failed("Workflow couldn't start.")

    def _active_workflow_plan(self):
        if self._active_workflow_id is None:
            return None
        plan = self.workflow_coordinator.get_plan(self._active_workflow_id)
        if plan is None or plan.state in (
            WorkflowState.SUCCEEDED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        ):
            return None
        return plan

    def _on_workflow_event(self, event) -> None:
        """Drive the workflow card from application-level WorkflowEvent only."""
        self.workflow_card.on_workflow_event(event)
        if event.type == WorkflowEventType.WORKFLOW_CREATED:
            self._active_workflow_id = event.workflow_id
        elif event.type in (
            WorkflowEventType.WORKFLOW_SUCCEEDED,
            WorkflowEventType.WORKFLOW_FAILED,
            WorkflowEventType.WORKFLOW_CANCELLED,
        ):
            if event.workflow_id == self._active_workflow_id:
                self._active_workflow_id = None

    def _on_workflow_open_plan(self, workflow_id: str) -> None:
        """Open the PLAN artifact with an ArtifactStore-validated path only.

        The UI never supplies an arbitrary path: the artifact ref is resolved
        and re-checked to stay inside the artifact root before the OS opens it.
        A failed open shows a short message, never a crash.
        """
        plan = self.workflow_coordinator.get_plan(workflow_id)
        if plan is None:
            self.workflow_card.on_open_plan_failed("Workflow no longer available.")
            return
        ref = None
        for step in plan.steps:
            for attached in step.attached_artifacts:
                if attached.kind == ArtifactKind.PLAN:
                    ref = attached
                    break
            if ref is not None:
                break
        if ref is None:
            self.workflow_card.on_open_plan_failed("No plan artifact yet.")
            return
        try:
            path = self.artifact_store.resolve_path(ref)
        except Exception:
            self.workflow_card.on_open_plan_failed("Plan file unavailable.")
            return
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        if not ok:
            self.workflow_card.on_open_plan_failed("Couldn't open the plan file.")

    def _on_workflow_cancel(self, workflow_id: str) -> None:
        """Cancel the workflow.

        While the Plan/Review step is RUNNING the executor owns the cancel: its
        real CANCELLED transport event completes the coordinator cancel. While
        the managed Codex exec runs (Step 2), the executor owns the process, so
        cancel stops the owned process tree first, then drops the executor
        state and cancels the workflow. Unrelated processes are never killed.
        """
        plan = self.workflow_coordinator.get_plan(workflow_id)
        if plan is None or plan.state in (
            WorkflowState.SUCCEEDED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        ):
            return
        if self.plan_executor.running:
            self.plan_executor.stop()
            return
        if self.review_executor.running:
            self.review_executor.stop()
            return
        codex_active = self.implement_executor.running
        if codex_active:
            self.implement_executor.stop()  # owned process-tree cancel
            self.implement_executor.reset()  # drop baseline/exec state
        try:
            self.workflow_coordinator.cancel_workflow(plan)
        except WorkflowTransitionError:
            pass
        if codex_active:
            self.workflow_card.show_notice("Workflow cancelled.")

    def _on_implement_execution_finished(self, _exit_code: int) -> None:
        """Route a finished managed Codex exec to the card (Step 2 still RUNNING).

        The workflow is not advanced here: completion stays a separate explicit
        user action with workspace evidence.
        """
        self.workflow_card.on_implement_execution_finished()

    def _on_workflow_implement(self, workflow_id: str) -> None:
        """User clicked ``Implement with Codex``: confirm then execute Step 2.

        Confirmation and execution stay two separate calls: ``confirm_step``
        only advances Step 2 to READY; ``implement_executor.execute`` consumes
        READY afterwards and drives the native Codex handoff. A double click is
        a no-op after the first launch (button disabled + executor busy gate).
        """
        plan = self.workflow_coordinator.get_plan(workflow_id)
        if plan is None or plan.state in (
            WorkflowState.SUCCEEDED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        ):
            return
        if self._active_workflow_id != workflow_id:
            return
        if self.implement_executor.running:
            return
        step = plan.steps[plan.current_step_index]
        if step.agent_id != "codex" or step.intent != WorkflowStepIntent.IMPLEMENT:
            return
        try:
            plan = self.workflow_coordinator.confirm_step(plan, step.step_id)
        except WorkflowTransitionError:
            self.workflow_card.show_notice("Couldn't start the implementation step.")
            self.workflow_card.enable_implement_action()
            return
        try:
            self.implement_executor.execute(plan, step.step_id)
        except Exception as exc:
            self.workflow_card.show_notice(str(exc))
            self.workflow_card.enable_implement_action()

    def _on_workflow_implement_complete(
        self, workflow_id: str, *, override: bool = False
    ) -> None:
        """User clicked ``Implementation complete`` (or the empty-diff override).

        The completion scan, artifact write, and USER_CONFIRMED success are all
        handled by the implement executor; this app layer only routes the
        result back to the card. A failed scan keeps Step 2 RUNNING so the user
        can retry; an empty diff without an override never succeeds the step.
        """
        plan = self.workflow_coordinator.get_plan(workflow_id)
        if plan is None or plan.state in (
            WorkflowState.SUCCEEDED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        ):
            return
        if self._active_workflow_id != workflow_id:
            return
        if not self.implement_executor.running:
            return
        step = plan.steps[plan.current_step_index]
        try:
            attempt = self.implement_executor.confirm_completion(
                plan, step.step_id, override_empty=override
            )
        except Exception as exc:
            self.workflow_card.show_notice(f"Couldn't finish the implementation step: {exc}")
            return
        self.workflow_card.on_completion_result(attempt)

    def _on_workflow_review(self, workflow_id: str) -> None:
        """User clicked ``Review with Claude``: confirm then execute Step 3.

        Confirmation and execution stay two separate calls: ``confirm_step``
        only advances Step 3 to READY; ``review_executor.execute`` consumes
        READY afterwards and drives the managed read-only Claude review. A
        double click is a no-op after the first start (button disabled +
        executor busy gate).
        """
        plan = self.workflow_coordinator.get_plan(workflow_id)
        if plan is None or plan.state in (
            WorkflowState.SUCCEEDED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        ):
            return
        if self._active_workflow_id != workflow_id:
            return
        if self.review_executor.running:
            return
        step = plan.steps[plan.current_step_index]
        if step.agent_id != "claude" or step.intent != WorkflowStepIntent.REVIEW:
            return
        try:
            plan = self.workflow_coordinator.confirm_step(plan, step.step_id)
        except WorkflowTransitionError:
            self.workflow_card.show_notice("Couldn't start the review step.")
            self.workflow_card.enable_review_action()
            return
        try:
            self.review_executor.execute(plan, step.step_id)
        except Exception as exc:
            self.workflow_card.show_notice(str(exc))
            self.workflow_card.enable_review_action()

    def _on_workflow_open_review(self, workflow_id: str) -> None:
        """Open the REVIEW artifact with an ArtifactStore-validated path only.

        Mirrors ``_on_workflow_open_plan``: the UI never supplies an arbitrary
        path; the artifact ref is resolved and re-checked to stay inside the
        artifact root before the OS opens it. A failed open shows a short
        message, never a crash.
        """
        plan = self.workflow_coordinator.get_plan(workflow_id)
        if plan is None:
            self.workflow_card.on_open_review_failed("Workflow no longer available.")
            return
        ref = None
        for step in plan.steps:
            for attached in step.attached_artifacts:
                if attached.kind == ArtifactKind.REVIEW:
                    ref = attached
                    break
            if ref is not None:
                break
        if ref is None:
            self.workflow_card.on_open_review_failed("No review artifact yet.")
            return
        try:
            path = self.artifact_store.resolve_path(ref)
        except Exception:
            self.workflow_card.on_open_review_failed("Review file unavailable.")
            return
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        if not ok:
            self.workflow_card.on_open_review_failed("Couldn't open the review file.")

    def _show_workflow_notice(self, message: str) -> None:
        try:
            self.bubble.show_message(
                "Workflow",
                message,
                duration_ms=3_200,
                accent=theme.WAITING_STATUS,
            )
        except Exception:
            pass

    def _on_workspace_changed(self, _workspace) -> None:
        # A recommendation is locked to the workspace it was created in; a
        # workspace switch drops the pending recommendation rather than
        # silently handing the task to the new workspace (Phase 9B section 23).
        self.recommendation_card.clear_pending()

    def _on_short_ask_force_send(self, prompt: str) -> None:
        if self.short_ask.running:
            return
        self._resume_fallbacks_this_cycle = 0
        self._do_short_ask(self.short_ask.agent, prompt)

    def _on_short_ask_retry(self) -> None:
        """Retry the last prompt after a Short Talk hard timeout."""
        prompt = self._last_short_ask_prompt
        if not prompt or self.short_ask.running:
            return
        self._do_short_ask(self.short_ask.agent, prompt)

    def _do_short_ask(self, agent: str, prompt: str) -> None:
        workspace = self.workspace_manager.current()
        self._short_ask_turn_failed = False
        self._last_short_ask_prompt = prompt
        # Claude persists its native session; Codex Short Talk is ephemeral
        # single-turn (read-only, no thread resume).
        persistent = agent == "claude"
        if not self.quick_ask.ask(
            agent,
            prompt,
            workspace,
            effort="low",
            persistent=persistent,
            isolated=True,
        ):
            return
        self.short_ask.set_running("Connecting…")

    def _on_short_ask_stop(self) -> None:
        self.quick_ask.stop()
        self.short_ask.begin_cancel()

    def _on_short_ask_finished(self, text: str, exit_code: int) -> None:
        if self._short_ask_turn_failed:
            self._short_ask_turn_failed = False
            return
        if self.short_ask.is_cancelling():
            # Cancel is only complete when the transport has actually finished.
            self.short_ask.on_cancelled()
            return
        if text.strip() or self.short_ask.full_answer():
            self.short_ask.finish_turn(text)
        else:
            self.short_ask.reset_with_note("No text returned.")

    def _on_short_ask_failed(self, message: str) -> None:
        self._short_ask_turn_failed = True
        if self.quick_ask.stale_cleared:
            # Resume id is no longer valid: the record was already cleared.
            # Fall back to a fresh session exactly once.
            if self._resume_fallbacks_this_cycle < 1:
                self._resume_fallbacks_this_cycle += 1
                prompt = self._last_short_ask_prompt
                if prompt:
                    self._do_short_ask(self.short_ask.agent, prompt)
                    if self.short_ask.running:
                        self.short_ask.set_running(
                            "Previous session expired. Starting a new session…"
                        )
            return
        if self.short_ask.state.value != "error":
            self.short_ask.show_error(message)

    def _on_short_ask_open_agent(self, agent: str) -> None:
        if agent == "chatgpt":
            ProcessLauncher.open_chatgpt()
        else:
            ProcessLauncher.launch_agent(agent, self.workspace_manager.current())

    def _on_quick_ask_telemetry(self, telemetry) -> None:
        self.ask_metrics.record(telemetry)
        self.short_ask.on_telemetry(telemetry)

    def _on_control_connection(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        socket.readyRead.connect(lambda current=socket: self._handle_control(current))
        if socket.bytesAvailable():
            self._handle_control(socket)

    @staticmethod
    def _handle_control(socket) -> None:
        data = bytes(socket.readAll()).decode("utf-8", "ignore")
        if "quit" in data:
            socket.disconnectFromServer()
            QApplication.quit()

    @staticmethod
    def _write_pid() -> None:
        try:
            PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass


_mutex_handle = None


def _acquire_single_instance() -> bool:
    """Return True if this process now owns the single-instance mutex."""
    global _mutex_handle
    if os.name != "nt":
        return True
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return False
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Firefly AI Companion")
    app.setQuitOnLastWindowClosed(False)
    app.setFont(QFont(theme.FONT_FAMILY, theme.FONT_SIZE_BODY))

    if not _acquire_single_instance():
        return 0

    server = QLocalServer()
    server.removeServer(SERVER_NAME)
    if not server.listen(SERVER_NAME):
        server = None

    shell = VisualShell(server, sessions_file=SESSIONS_FILE)
    app.aboutToQuit.connect(shell.shutdown)
    shell.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
