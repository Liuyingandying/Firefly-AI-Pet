"""Small light-glass recommendation card for Phase 9B/9C router UX.

Shown only when the deterministic AgentRouter picks OPEN_NATIVE or UNAVAILABLE.
Claude SHORT_TALK continues directly in ShortAskPanel with zero extra clicks.

The card is a *pre-turn routing decision*, not a streaming surface: it stores
the prompt / recommended agent / workspace captured at show time and only acts
on an explicit user click. It never starts a subprocess on its own, never
overrides the router, and never fabricates a managed backend for an unavailable
agent. The only agent it may act on is the one the router recommended at
creation time (agent lock), in the workspace captured at creation time
(workspace lock).

Phase 9C adds the user-confirmed native handoff: for agents whose native
interactive CLI has a verified safe positional-prompt transport (Codex/Claude),
the primary action becomes ``Send to <agent>``. Only that click creates a
HandoffRequest and hands the original task to the native Agent. Phase 9D.3
restyles the Codex coding card: ``Plan with Claude`` (secondary) is the entry
into the managed PLAN_IMPLEMENT_REVIEW workflow and ``Open only`` (light) keeps
the plain native open. ``Open only`` never creates a handoff. The card never
displays internal scores or raw reason tokens.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from core.handoff import (
    HandoffRequest,
    HandoffState,
    make_handoff_request,
)
from core.routing_models import AgentRecommendation, HandoffMode, ReasonCode
from ui.process_launcher import HANDOFF_TRANSPORT_VERIFIED

from . import theme
from .popover_base import PopoverBase


AGENT_DISPLAY = {"claude": "Claude", "codex": "Codex", "chatgpt": "ChatGPT"}

# Core emits stable ReasonCode tokens; this UI layer owns the display copy.
REASON_TITLE = {
    ReasonCode.BEST_FOR_CODING: "Coding task",
    ReasonCode.CODING_CAPABILITY: "Coding task",
    ReasonCode.NATIVE_SURFACE_REQUIRED: "Coding task",
    ReasonCode.AMBIGUOUS_TASK: "Coding task",
    ReasonCode.USER_REQUESTED_AGENT: "Task",
    ReasonCode.OPEN_REQUESTED: "Open agent",
    ReasonCode.VISION_MANAGED_UNAVAILABLE: "Vision isn't available here yet",
    ReasonCode.BACKEND_UNAVAILABLE: "Not available yet",
}

_VISION_NOTE = "No managed route for images right now."
_CHATGPT_NOTE = "Direct task routing isn't available for this agent yet."
_HANDOFF_FADE_MS = 1500


class RecommendationCard(PopoverBase):
    """Frameless light-glass card that renders one AgentRecommendation.

    A pending recommendation is held in memory only (prompt / agent / workspace);
    it is never written to disk and never restored across a Firefly restart.
    """

    send_requested = Signal(str, str, str)  # agent_id, workspace, original prompt
    open_native_requested = Signal(str, str)  # agent_id, workspace (Open only)
    plan_with_claude_requested = Signal(str, str)  # original prompt, workspace

    def __init__(self, *, width: int = theme.POPOVER_WIDTH, parent=None):
        super().__init__(width=width, parent=parent)
        self.setWindowTitle("Firefly Recommendation")
        self._prompt = ""
        self._workspace = ""
        self._agent_id = ""
        self._pending = False
        self._mode = "unavailable"  # "send" | "open" | "unavailable"
        self._secondary_action = ""  # "" | "open_only" | "plan_with_claude"
        self._plan_requested = False
        self._handoff: HandoffRequest | None = None
        self._handoff_state: HandoffState | None = None
        self._handoff_error = ""
        self._fade_timer: QTimer | None = None

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(theme.SPACE_SM)
        self._title = QLabel("")
        self._title.setStyleSheet(theme.primary_label_style(size=11))
        header.addWidget(self._title, 1, Qt.AlignVCenter)
        self._close_btn = QPushButton("×")
        self._close_btn.setObjectName("recClose")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFixedSize(20, 20)
        self._close_btn.setStyleSheet(theme.link_button_style("recClose"))
        self._close_btn.clicked.connect(self.dismiss)
        header.addWidget(self._close_btn, 0, Qt.AlignVCenter)
        self.content_layout.addLayout(header)

        self._status = QLabel("")
        self._status.setStyleSheet(theme.secondary_label_style())
        self._status.setWordWrap(True)
        self.content_layout.addWidget(self._status)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(theme.SPACE_XS)
        actions.addStretch(1)
        self._light_btn = QPushButton("")
        self._light_btn.setObjectName("recLight")
        self._light_btn.setCursor(Qt.PointingHandCursor)
        self._light_btn.setStyleSheet(theme.link_button_style("recLight"))
        self._light_btn.setVisible(False)
        actions.addWidget(self._light_btn)
        self._secondary_btn = QPushButton("")
        self._secondary_btn.setObjectName("recSecondary")
        self._secondary_btn.setCursor(Qt.PointingHandCursor)
        self._secondary_btn.setStyleSheet(theme.link_button_style("recSecondary"))
        self._secondary_btn.setVisible(False)
        actions.addWidget(self._secondary_btn)
        self._primary_btn = QPushButton("")
        self._primary_btn.setObjectName("recPrimary")
        self._primary_btn.setCursor(Qt.PointingHandCursor)
        self._primary_btn.setStyleSheet(theme.popover_button_style("recPrimary"))
        self._primary_btn.setVisible(False)
        actions.addWidget(self._primary_btn)
        self.content_layout.addLayout(actions)

        self._light_btn.clicked.connect(self._on_light)
        self._secondary_btn.clicked.connect(self._on_secondary)
        self._primary_btn.clicked.connect(self._on_primary)

    # -- public API -----------------------------------------------------

    @property
    def has_pending(self) -> bool:
        return self._pending

    @property
    def pending_agent(self) -> str:
        return self._agent_id

    @property
    def pending_workspace(self) -> str:
        return self._workspace

    @property
    def pending_prompt(self) -> str:
        return self._prompt

    @property
    def handoff(self) -> HandoffRequest | None:
        return self._handoff

    @property
    def handoff_state(self) -> HandoffState | None:
        return self._handoff_state

    @property
    def handoff_error(self) -> str:
        return self._handoff_error

    def show_recommendation(
        self, rec: AgentRecommendation, prompt: str, workspace: str
    ) -> None:
        """Lock prompt/agent/workspace and render the recommendation.

        Never launches anything: the primary/secondary actions wait for a click.
        """
        self._prompt = prompt
        self._workspace = workspace
        self._agent_id = rec.agent_id
        self._pending = True
        self._plan_requested = False
        self._handoff = None
        self._handoff_state = None
        self._handoff_error = ""
        self._primary_btn.setEnabled(True)
        self._secondary_btn.setEnabled(True)
        self._light_btn.setEnabled(True)
        if rec.handoff_mode == HandoffMode.OPEN_NATIVE:
            self._render_open_native(rec)
        else:
            self._render_unavailable(rec)
        self.adjustSize()
        self.show()
        self.raise_()

    def suspend(self) -> None:
        """Hide but keep the pending recommendation in memory (overlay priority)."""
        self.hide()

    def resume_show(self) -> None:
        self.adjustSize()
        self.show()
        self.raise_()

    def clear_pending(self) -> None:
        self._stop_fade()
        self._pending = False
        self._plan_requested = False
        self._prompt = ""
        self._workspace = ""
        self._agent_id = ""
        self._mode = "unavailable"
        self._secondary_action = ""
        self._handoff = None
        self._handoff_state = None
        self._handoff_error = ""
        self.hide()

    def dismiss(self) -> None:
        """User closed the card: drop the pending recommendation."""
        was_visible = self.isVisible()
        self.clear_pending()
        if was_visible:
            self.dismissed.emit()

    def on_handoff_result(self, ok: bool, message: str = "") -> None:
        """Report the native process launch result back to the card.

        Called by the shell after the user's ``Send to <agent>`` click. Success
        means the native process started and the task payload was handed over —
        never that the task is done. The card then fades shortly after.
        """
        if self._handoff is None or self._mode != "send":
            return
        display = AGENT_DISPLAY.get(self._agent_id, self._agent_id.title())
        if ok:
            self._handoff_state = HandoffState.HANDED_OFF
            self._status.setText(f"Sent to {display}")
            self._primary_btn.setEnabled(False)
            self._secondary_btn.setEnabled(False)
            self._light_btn.setEnabled(False)
            self._start_fade()
        else:
            self._handoff_state = HandoffState.FAILED
            self._handoff_error = message
            self._status.setText(f"Couldn't start {display}")
            self._primary_btn.setEnabled(True)

    # -- rendering ------------------------------------------------------

    def _render_open_native(self, rec: AgentRecommendation) -> None:
        display = AGENT_DISPLAY.get(rec.agent_id, rec.agent_id.title())
        self._title.setText(REASON_TITLE.get(rec.reason_code, "Task"))
        self._status.setText(f"Recommended · {display}")
        self._light_btn.setVisible(False)
        self._secondary_action = ""
        if rec.reason_code == ReasonCode.OPEN_REQUESTED:
            # User explicitly asked to *open* the agent: no task to hand over.
            self._mode = "open"
            self._primary_btn.setText(f"Open {display}")
            self._primary_btn.setVisible(True)
            self._secondary_btn.setVisible(False)
            return
        if (
            rec.agent_id in HANDOFF_TRANSPORT_VERIFIED
            and rec.handoff_mode == HandoffMode.OPEN_NATIVE
        ):
            self._mode = "send"
            self._primary_btn.setText(f"Send to {display}")
            self._primary_btn.setVisible(True)
            if rec.agent_id == "codex":
                # 9D.3: the Codex coding card offers the managed Plan route
                # alongside the direct handoff. "Plan with Claude" is the entry
                # into the PLAN_IMPLEMENT_REVIEW workflow; "Open only" keeps
                # the plain native open.
                self._secondary_action = "plan_with_claude"
                self._secondary_btn.setText("Plan with Claude")
                self._secondary_btn.setVisible(True)
                self._light_btn.setText("Open only")
                self._light_btn.setVisible(True)
            else:
                self._secondary_action = "open_only"
                self._secondary_btn.setText("Open only")
                self._secondary_btn.setVisible(True)
                self._light_btn.setVisible(False)
            return
        # Handoff not verified for this agent: keep the Phase 9B Open path.
        self._mode = "open"
        self._primary_btn.setText(f"Open {display}")
        self._primary_btn.setVisible(True)
        self._secondary_btn.setVisible(False)

    def _render_unavailable(self, rec: AgentRecommendation) -> None:
        self._mode = "unavailable"
        self._title.setText(REASON_TITLE.get(rec.reason_code, "Not available yet"))
        self._secondary_btn.setVisible(False)
        self._light_btn.setVisible(False)
        self._secondary_action = ""
        if rec.reason_code == ReasonCode.VISION_MANAGED_UNAVAILABLE:
            self._status.setText(_VISION_NOTE)
            self._primary_btn.setVisible(False)
            return
        self._status.setText(_CHATGPT_NOTE)
        if rec.agent_id == "chatgpt":
            self._primary_btn.setText("Open ChatGPT")
            self._primary_btn.setVisible(True)
        else:
            self._primary_btn.setVisible(False)

    # -- actions --------------------------------------------------------

    def _on_primary(self) -> None:
        if not self._pending or self._mode == "unavailable":
            return
        if self._handoff_state in (HandoffState.LAUNCHING, HandoffState.HANDED_OFF):
            return  # double-click / post-send guard: never launch twice per card
        agent = self._agent_id
        workspace = self._workspace
        if self._mode == "send":
            self._handoff = make_handoff_request(
                agent, workspace, self._prompt, requires_confirmation=True
            )
            self._handoff_state = HandoffState.LAUNCHING
            self._handoff_error = ""
            self._primary_btn.setEnabled(False)
            display = AGENT_DISPLAY.get(agent, agent.title())
            self._status.setText(f"Sending to {display}…")
            self.send_requested.emit(agent, workspace, self._prompt)
            return
        # "open" mode: plain native surface open, no task, no handoff.
        self.clear_pending()
        self.open_native_requested.emit(agent, workspace)

    def _on_secondary(self) -> None:
        if not self._pending or self._mode != "send":
            return
        if self._handoff_state in (HandoffState.LAUNCHING, HandoffState.HANDED_OFF):
            return
        if self._secondary_action == "plan_with_claude":
            if self._plan_requested:
                return  # double-click guard: never request twice per card
            self._plan_requested = True
            self._primary_btn.setEnabled(False)
            self._secondary_btn.setEnabled(False)
            self._light_btn.setEnabled(False)
            self._status.setText("Starting workflow…")
            self.plan_with_claude_requested.emit(self._prompt, self._workspace)
            return
        # Open only: reuse the Phase 9B launcher contract, no prompt, no handoff.
        agent = self._agent_id
        workspace = self._workspace
        self.clear_pending()
        self.open_native_requested.emit(agent, workspace)

    def _on_light(self) -> None:
        if not self._pending or self._mode != "send":
            return
        if self._handoff_state in (HandoffState.LAUNCHING, HandoffState.HANDED_OFF):
            return
        if self._plan_requested:
            return
        # "Open only": plain native surface open, no task, no handoff.
        agent = self._agent_id
        workspace = self._workspace
        self.clear_pending()
        self.open_native_requested.emit(agent, workspace)

    # -- fade -----------------------------------------------------------

    def _start_fade(self) -> None:
        self._stop_fade()
        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._fade_after_handoff)
        self._fade_timer.start(_HANDOFF_FADE_MS)

    def _stop_fade(self) -> None:
        if self._fade_timer is not None:
            self._fade_timer.stop()
            self._fade_timer.deleteLater()
            self._fade_timer = None

    def _fade_after_handoff(self) -> None:
        self._stop_fade()
        if self._handoff_state == HandoffState.HANDED_OFF:
            self.clear_pending()
