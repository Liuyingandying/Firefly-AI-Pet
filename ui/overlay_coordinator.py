"""Anchor the Phase 8A.1 visual islands around the Firefly pet."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from core.notification_manager import NotificationEvent
from . import theme
from .permission_card import PERMISSION_AGENTS


class PresentationState(Enum):
    """The three shell presentation states driven by the left-click sequence.

    PET_ONLY  -> character only (toolbar/dock/bubble hidden)
    CONTROLS  -> character + toolbar + dock (bubble hidden)
    CHAT      -> character + toolbar + dock + greeting/Ask bubble
    """

    PET_ONLY = "pet_only"
    CONTROLS = "controls"
    CHAT = "chat"


class OverlayCoordinator(QObject):
    permission_view_requested = Signal(str)
    short_ask_requested = Signal()

    def __init__(
        self,
        pet,
        dock,
        bubble,
        toolbar,
        parent=None,
        *,
        workspace_popover=None,
        session_popover=None,
        permission_card=None,
        settings_popover=None,
        settings_manager=None,
        ask_pill=None,
        short_ask=None,
        recommendation_card=None,
        workflow_card=None,
        pagelens_panel=None,
    ):
        super().__init__(parent)
        self.pet = pet
        self.dock = dock
        self.bubble = bubble
        self.toolbar = toolbar
        self.workspace_popover = workspace_popover
        self.session_popover = session_popover
        self.permission_card = permission_card
        self.settings_popover = settings_popover
        self.settings_manager = settings_manager
        self.ask_pill = ask_pill
        self.short_ask = short_ask
        self.recommendation_card = recommendation_card
        self.workflow_card = workflow_card
        self.pagelens_panel = pagelens_panel
        self._active_context = "workspace"
        self._waiting: dict[str, object] = {}
        self._pending_notification = None
        self._context_switching = False
        self._presentation_state = PresentationState.PET_ONLY
        self._pagelens_was_visible = True  # track bubble visibility before pagelens open

        self.pet.position_changed.connect(self.reposition)
        self.pet.left_clicked.connect(self.advance_presentation_state)
        self.pet.reset_requested.connect(self.reset_position)
        self.pet.drag_started.connect(self._on_drag_started)
        self.toolbar.action_requested.connect(self._toolbar_action)
        theme.on_scale_changed(self.apply_ui_scale)

        if self.workspace_popover is not None:
            self.workspace_popover.sessions_requested.connect(self._show_sessions)
            self.workspace_popover.dismissed.connect(self._flush_pending_notification)
        if self.session_popover is not None:
            self.session_popover.workspace_requested.connect(self._show_workspace)
            self.session_popover.dismissed.connect(self._flush_pending_notification)
        if self.permission_card is not None:
            self.permission_card.view_requested.connect(self.permission_view_requested.emit)
        if self.settings_popover is not None:
            self.settings_popover.dismissed.connect(self._flush_pending_notification)
            self.settings_popover.reset_position_requested.connect(self._on_settings_reset_position)
        if self.ask_pill is not None:
            self.ask_pill.ask_clicked.connect(self.short_ask_requested.emit)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def show_shell(self) -> None:
        self.pet.show()
        self.reset_position()
        self.dock.show()
        self.toolbar.show()
        if self.permission_card is None or not self.permission_card.isVisible():
            if self._greeting_on_startup():
                self.bubble.show()
                self._show_ask_pill()
        self.reposition()
        self.raise_shell()

    def show_shell_pet_only(self) -> None:
        """Startup entry: the character only, no chrome, no startup flash.

        The default presentation state is applied before any top-level chrome is
        shown, so the first painted frame is Firefly alone.
        """
        self.pet.show()
        self.reset_position()
        self.set_presentation_state(PresentationState.PET_ONLY)
        self.raise_shell()

    def _greeting_on_startup(self) -> bool:
        if self.settings_manager is not None:
            return bool(self.settings_manager.greeting_on_startup)
        return True

    def raise_shell(self) -> None:
        self.dock.raise_()
        self.toolbar.raise_()
        if self.bubble.isVisible():
            self.bubble.raise_()
        self.pet.raise_()
        if self.ask_pill is not None and self.ask_pill.isVisible():
            self.ask_pill.raise_()
        if self.short_ask is not None and self.short_ask.isVisible():
            self.short_ask.raise_()
        if self.recommendation_card is not None and self.recommendation_card.isVisible():
            self.recommendation_card.raise_()
        if self.workflow_card is not None and self.workflow_card.isVisible():
            self.workflow_card.raise_()
        if self.pagelens_panel is not None and self.pagelens_panel.isVisible():
            self.pagelens_panel.raise_()

    def reset_position(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = (
            available.right()
            - theme.scaled(theme.SCREEN_MARGIN)
            - theme.scaled(theme.CLUSTER_RIGHT_INSET)
            - self.toolbar.width()
            - theme.scaled(theme.TOOLBAR_ANCHOR_GAP)
            - self.pet.width()
            + 1
        )
        y = (
            available.bottom()
            - theme.scaled(theme.SCREEN_MARGIN)
            - theme.scaled(theme.CLUSTER_BOTTOM_INSET)
            - self.dock.height()
            - theme.scaled(theme.DOCK_ANCHOR_GAP)
            - self.pet.height()
            + 1
        )
        self.pet.move(self._clamp_point(QPoint(x, y), self.pet, available))
        self.reposition()

    def reposition(self) -> None:
        screen = QGuiApplication.screenAt(self.pet.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()

        dock_point = QPoint(
            pet_geo.center().x() - self.dock.width() // 2,
            pet_geo.bottom() + theme.scaled(theme.DOCK_ANCHOR_GAP),
        )
        if dock_point.y() + self.dock.height() > available.bottom() + 1:
            dock_point.setY(pet_geo.top() - theme.scaled(theme.DOCK_ANCHOR_GAP) - self.dock.height())
        self.dock.move(self._clamp_point(dock_point, self.dock, available))

        toolbar_point = QPoint(
            pet_geo.right() + theme.scaled(theme.TOOLBAR_ANCHOR_GAP),
            pet_geo.center().y() - self.toolbar.height() // 2,
        )
        if toolbar_point.x() + self.toolbar.width() > available.right() + 1:
            toolbar_point.setX(pet_geo.left() - theme.scaled(theme.TOOLBAR_ANCHOR_GAP) - self.toolbar.width())
        self.toolbar.move(self._clamp_point(toolbar_point, self.toolbar, available))

        bubble_point = QPoint(
            pet_geo.left() - self.bubble.width() + theme.scaled(theme.BUBBLE_PET_OVERLAP),
            pet_geo.top() - self.bubble.height() + theme.scaled(theme.BUBBLE_VERTICAL_OVERLAP),
        )
        if bubble_point.x() < available.left():
            bubble_point.setX(pet_geo.right() - theme.scaled(theme.BUBBLE_PET_OVERLAP))
        self.bubble.move(self._clamp_point(bubble_point, self.bubble, available))

        self._position_ask_pill()

        if self.permission_card is not None and self.permission_card.isVisible():
            self._position_permission_card()

        if self.pagelens_panel is not None and self.pagelens_panel.visible:
            self._position_pagelens()

    def apply_ui_scale(self, scale: float) -> None:
        """Resize the whole shell for a new ui_scale while holding the pet anchor.

        Keeping the pet's bottom-center fixed prevents the cluster from jumping
        when the character shrinks/grows; dock/toolbar/bubble re-anchor after.
        """
        geo = self.pet.frameGeometry()
        anchor_x = geo.center().x()
        anchor_bottom = geo.bottom()
        self.pet.apply_scale()
        self.dock.apply_scale()
        self.toolbar.apply_scale()
        self.bubble.apply_scale()
        if self.pagelens_panel is not None:
            self.pagelens_panel.apply_scale()
        new_geo = self.pet.frameGeometry()
        self.pet.move(
            new_geo.topLeft()
            + QPoint(anchor_x - new_geo.center().x(), anchor_bottom - new_geo.bottom())
        )
        self.reposition()

    def toggle_bubble(self) -> None:
        # Workflow recovery: when an active non-terminal workflow's card is
        # hidden (closed or suspended by a higher tier), clicking Firefly
        # re-shows it — unless the PermissionCard is still up, which stays the
        # highest priority (it never auto-repops the workflow card on clear).
        if self.workflow_card is not None and self.workflow_card.has_active_workflow:
            if not self.workflow_card.isVisible():
                permission_up = (
                    self.permission_card is not None and self.permission_card.isVisible()
                )
                if not permission_up:
                    self.show_workflow()
                    return
        if self.bubble.isVisible():
            self.bubble.hide()
            self._hide_ask_pill()
        else:
            self.reposition()
            self.bubble.show_greeting()
            self.bubble.show()
            self.bubble.raise_()
            self._show_ask_pill()
            self.pet.raise_()

    @property
    def presentation_state(self) -> PresentationState:
        return self._presentation_state

    def set_presentation_state(self, state: PresentationState) -> None:
        self._presentation_state = state
        if state == PresentationState.PET_ONLY:
            self._apply_pet_only()
        elif state == PresentationState.CONTROLS:
            self._apply_controls()
        elif state == PresentationState.CHAT:
            self._apply_chat()

    def advance_presentation_state(self) -> None:
        """Advance the three-state cycle: PET_ONLY -> CONTROLS -> CHAT -> PET_ONLY."""
        if self._presentation_state == PresentationState.PET_ONLY:
            self.set_presentation_state(PresentationState.CONTROLS)
        elif self._presentation_state == PresentationState.CONTROLS:
            self.set_presentation_state(PresentationState.CHAT)
        else:
            self.set_presentation_state(PresentationState.PET_ONLY)

    def _apply_pet_only(self) -> None:
        self.toolbar.hide()
        self.dock.hide()
        self._hide_bubble()
        # Close the attached transient overlays. Hiding is never a cancel: a
        # running Short Ask / Recommendation / Workflow keeps its backend state.
        self._dismiss_business_popovers()
        self.suspend_short_ask()
        self.suspend_recommendation()
        self.suspend_workflow()
        self.pet.raise_()

    def _apply_controls(self) -> None:
        self.dock.show()
        self.toolbar.show()
        self._hide_bubble()
        self.reposition()
        self.raise_shell()

    def _apply_chat(self) -> None:
        self.dock.show()
        self.toolbar.show()
        # CHAT always shows the bubble; the greeting preference only decides
        # whether to pre-fill it with the greeting copy, never its existence.
        if self._greeting_on_startup():
            self.bubble.show_greeting()
        else:
            self.bubble.show_blank()
        self.bubble.show()
        self.bubble.raise_()
        self._show_ask_pill()
        self.pet.raise_()
        self.reposition()
        self.raise_shell()

    def close_overlays(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.bubble.close()
        self.toolbar.close()
        self.dock.close()
        if self.ask_pill is not None:
            self.ask_pill.close()
        if self.short_ask is not None:
            self.short_ask.close()
        if self.recommendation_card is not None:
            self.recommendation_card.close()
        if self.workflow_card is not None:
            self.workflow_card.close()
        if self.workspace_popover is not None:
            self.workspace_popover.close()
        if self.session_popover is not None:
            self.session_popover.close()
        if self.permission_card is not None:
            self.permission_card.close()
        if self.settings_popover is not None:
            self.settings_popover.close()
        if self.pagelens_panel is not None:
            self.pagelens_panel.close()

    def _toolbar_action(self, action_id: str) -> None:
        if action_id == "companion":
            if not self.bubble.isVisible():
                self.toggle_bubble()
        elif action_id == "pagelens":
            self.toggle_pagelens()
        elif action_id == "workspace":
            self._toggle_context()
        elif action_id == "settings":
            self._toggle_settings()

    def _toggle_settings(self) -> None:
        if self.settings_popover is None:
            return
        if self.settings_popover.isVisible():
            self.settings_popover.dismiss()
        else:
            self._show_settings()

    def _show_settings(self) -> None:
        if self.settings_popover is None:
            return
        self._dismiss_business_popovers()
        self.suspend_short_ask()
        self.suspend_recommendation()
        self.suspend_workflow()
        self._hide_bubble()
        self.settings_popover.refresh()
        self._position_popover(self.settings_popover)

    def _on_settings_reset_position(self) -> None:
        self._dismiss_business_popovers()
        self.reset_position()

    def _on_drag_started(self) -> None:
        if self.workspace_popover is not None and self.workspace_popover.isVisible():
            self.workspace_popover.dismiss()
        if self.session_popover is not None and self.session_popover.isVisible():
            self.session_popover.dismiss()
        if self.settings_popover is not None and self.settings_popover.isVisible():
            self.settings_popover.dismiss()
        # PageLens is a persistent reading panel — do NOT dismiss on drag.

    # -- PageLens ---------------------------------------------------------

    def toggle_pagelens(self) -> None:
        if self.pagelens_panel is None:
            return
        if self.pagelens_panel.visible:
            self.hide_pagelens()
        else:
            self.show_pagelens()

    def show_pagelens(self) -> None:
        if self.pagelens_panel is None:
            return
        # Save bubble visibility state before opening PageLens
        self._pagelens_was_visible = self.bubble.isVisible()
        # Hide bubble while PageLens is open
        self._hide_bubble()
        # Dismiss higher-tier overlays
        self._dismiss_business_popovers()
        self.suspend_short_ask()
        self.suspend_recommendation()
        self.suspend_workflow()
        # Highlight the toolbar button
        self.toolbar.select_action("pagelens", emit_signal=False)
        # Show and position
        self.pagelens_panel.show_panel()
        self._position_pagelens()
        self.raise_shell()

    def hide_pagelens(self) -> None:
        if self.pagelens_panel is None:
            return
        self.pagelens_panel.hide_panel()
        # Restore toolbar selection to companion
        self.toolbar.select_action("companion", emit_signal=False)
        # Restore bubble if it was visible before PageLens opened
        if self._pagelens_was_visible:
            self.reposition()
            self.bubble.show()
            self.bubble.raise_()

    def _position_pagelens(self) -> None:
        panel = self.pagelens_panel
        if panel is None or not panel.visible:
            return
        screen = (
            QGuiApplication.screenAt(self.pet.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()
        toolbar_geo = self.toolbar.frameGeometry()

        # Decide anchor side: prefer left of toolbar, flip if not enough space
        gap = theme.scaled(theme.PAGELENS_ANCHOR_GAP)
        panel_width = panel.width()
        panel_height = panel.height()

        # Default: place panel to the left of the toolbar/cluster
        # Vertical center: align panel center with cluster center
        cluster_left = min(pet_geo.left(), toolbar_geo.left())
        cluster_center_y = (toolbar_geo.top() + toolbar_geo.bottom()) // 2
        point = QPoint(
            cluster_left - gap - panel_width,
            cluster_center_y - panel_height // 2,
        )

        # Check if left side has enough space
        if point.x() < available.left() + 10:
            # Flip to the right side
            cluster_right = max(pet_geo.right(), toolbar_geo.right())
            point = QPoint(
                cluster_right + gap,
                cluster_center_y - panel_height // 2,
            )
            panel.set_anchor_side("right")
        else:
            panel.set_anchor_side("left")

        # Clamp vertically with margin
        margin = theme.scaled(theme.SPACE_MD)
        if point.y() < available.top() + margin:
            point.setY(available.top() + margin)
        if point.y() + panel_height > available.bottom() - margin:
            point.setY(available.bottom() - panel_height - margin)

        panel.move(self._clamp_point(point, panel, available))

    # -- lifecycle ------------------------------------------------------

    def on_agent_state(self, agent_id: str, state) -> None:
        """Track per-agent waiting state for the permission card.

        Consumes the same structured AgentState the app already receives from
        StateMonitor. No second permission system, no JSON reads here.
        """
        if agent_id not in PERMISSION_AGENTS:
            return
        if state.state.value == "waiting":
            self._waiting[agent_id] = state
        else:
            self._waiting.pop(agent_id, None)
        self._refresh_permission_card()

    def _refresh_permission_card(self) -> None:
        if self.permission_card is None:
            return
        if not self._waiting:
            self.permission_card.hide_card()
            self._flush_pending_notification()
            return
        primary = max(self._waiting, key=lambda a: self._waiting[a].timestamp)
        agents = sorted(self._waiting)
        self._show_permission_card(agents, primary)

    def _show_permission_card(self, agents, primary) -> None:
        if self.permission_card is None:
            return
        self._hide_bubble()
        self._dismiss_business_popovers()
        self.suspend_short_ask()
        self.suspend_recommendation()
        self.suspend_workflow()
        self.permission_card.show_for(agents, primary)
        self._position_permission_card()

    def _dismiss_business_popovers(self) -> None:
        if self.workspace_popover is not None and self.workspace_popover.isVisible():
            self.workspace_popover.dismiss()
        if self.session_popover is not None and self.session_popover.isVisible():
            self.session_popover.dismiss()
        if self.settings_popover is not None and self.settings_popover.isVisible():
            self.settings_popover.dismiss()

    def _position_permission_card(self) -> None:
        card = self.permission_card
        if card is None:
            return
        screen = (
            QGuiApplication.screenAt(self.pet.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()
        point = QPoint(
            pet_geo.left() - card.width() + theme.BUBBLE_PET_OVERLAP,
            pet_geo.top() - card.height() + theme.BUBBLE_VERTICAL_OVERLAP,
        )
        if point.x() < available.left():
            point.setX(pet_geo.right() - theme.BUBBLE_PET_OVERLAP)
        card.move(self._clamp_point(point, card, available))

    # -- Notifications ---------------------------------------------------

    def on_notification(self, event: NotificationEvent) -> None:
        """Route a structured notification against transient overlay priority.

        PermissionCard and business popovers outrank ordinary notifications:
        nothing is shown while they are up; at most one notification is held
        pending and flushed once the higher-priority overlay clears.
        """
        if self.permission_card is not None and self.permission_card.isVisible():
            self._queue_notification(event)
            return
        if self._visible_popover() is not None:
            self._queue_notification(event)
            return
        self._show_notification(event)

    def _queue_notification(self, event: NotificationEvent) -> None:
        pending = self._pending_notification
        if pending is None or event.priority >= pending.priority:
            self._pending_notification = event

    def _show_notification(self, event: NotificationEvent) -> None:
        self._pending_notification = None
        self.reposition()
        if event.kind == "error":
            self.bubble.show_message(
                event.title,
                event.message,
                duration_ms=4_500,
                accent=theme.ERROR_STATUS,
            )
        else:
            self.bubble.show_message(event.title, event.message, duration_ms=3_000)

    def _flush_pending_notification(self) -> None:
        pending = self._pending_notification
        if pending is None or self._context_switching:
            return
        if self.permission_card is not None and self.permission_card.isVisible():
            return
        if self._visible_popover() is not None:
            return
        self._show_notification(pending)

    # -- Workspace / Sessions shared entry ------------------------------

    def _active_popover(self):
        if self._active_context == "sessions" and self.session_popover is not None:
            return self.session_popover
        return self.workspace_popover

    def _hide_bubble(self) -> None:
        if self.bubble.isVisible():
            self.bubble.hide()
        self._hide_ask_pill()

    def _show_ask_pill(self) -> None:
        if self.ask_pill is None or not self.bubble.isVisible():
            return
        self._position_ask_pill()
        self.ask_pill.show()
        self.ask_pill.raise_()

    def _hide_ask_pill(self) -> None:
        if self.ask_pill is not None and self.ask_pill.isVisible():
            self.ask_pill.hide()

    def _position_ask_pill(self) -> None:
        if self.ask_pill is None:
            return
        bubble_geo = self.bubble.frameGeometry()
        point = QPoint(
            bubble_geo.right() - self.ask_pill.width() - 14,
            bubble_geo.top() + 8,
        )
        screen = QGuiApplication.screenAt(point) or QApplication.primaryScreen()
        if screen is None:
            return
        self.ask_pill.move(self._clamp_point(point, self.ask_pill, screen.availableGeometry()))

    # -- Short Ask panel ------------------------------------------------

    def show_short_ask(self) -> None:
        """Open the Short Ask panel (input or resumed turn) at transient priority."""
        if self.short_ask is None:
            return
        self._dismiss_business_popovers()
        self._hide_bubble()
        self.suspend_recommendation()
        self.suspend_workflow()
        if self.permission_card is not None and self.permission_card.isVisible():
            return
        if self.short_ask.has_pending_state():
            self.short_ask.resume_show()
        else:
            self.short_ask.show()
            self.short_ask.raise_()
        self._position_short_ask()

    def suspend_short_ask(self) -> None:
        """Hide Short Ask. A live/pending turn is preserved, never killed.

        Business popovers and PermissionCard outrank Short Ask: closing the
        panel is only allowed when it is idle; a running turn keeps its state
        in memory so the user can resume it (see Phase 8C.3 section 17/18).
        """
        if self.short_ask is None or not self.short_ask.isVisible():
            return
        if self.short_ask.running or self.short_ask.has_pending_state():
            self.short_ask.suspend()
        else:
            self.short_ask.dismiss()

    def dismiss_short_ask(self) -> None:
        self.suspend_short_ask()

    def _position_short_ask(self) -> None:
        panel = self.short_ask
        if panel is None:
            return
        screen = (
            QGuiApplication.screenAt(self.pet.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()
        point = QPoint(
            pet_geo.left() - panel.width() + theme.BUBBLE_PET_OVERLAP,
            pet_geo.top() - panel.height() + theme.BUBBLE_VERTICAL_OVERLAP,
        )
        if point.x() < available.left():
            point.setX(pet_geo.right() - theme.BUBBLE_PET_OVERLAP)
        panel.move(self._clamp_point(point, panel, available))

    # -- Recommendation card -------------------------------------------

    def show_recommendation(self) -> None:
        """Raise the recommendation card at interactive priority (same tier as
        Short Talk). Higher tiers hide it via suspend_recommendation."""
        if self.recommendation_card is None:
            return
        self._dismiss_business_popovers()
        self._hide_bubble()
        self.suspend_short_ask()
        self.suspend_workflow()
        if self.permission_card is not None and self.permission_card.isVisible():
            return
        self.recommendation_card.resume_show()
        self._position_recommendation()

    def suspend_recommendation(self) -> None:
        """Hide the card but keep the pending recommendation in memory.

        The card is never cancelled by overlay priority; the user restores it by
        clicking Ask again (Phase 9B section 21/22)."""
        if self.recommendation_card is None or not self.recommendation_card.isVisible():
            return
        self.recommendation_card.suspend()

    def _position_recommendation(self) -> None:
        card = self.recommendation_card
        if card is None:
            return
        screen = (
            QGuiApplication.screenAt(self.pet.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()
        point = QPoint(
            pet_geo.left() - card.width() + theme.BUBBLE_PET_OVERLAP,
            pet_geo.top() - card.height() + theme.BUBBLE_VERTICAL_OVERLAP,
        )
        if point.x() < available.left():
            point.setX(pet_geo.right() - theme.BUBBLE_PET_OVERLAP)
        card.move(self._clamp_point(point, card, available))

    # -- Workflow card ---------------------------------------------------

    def show_workflow(self) -> None:
        """Raise the workflow card at interactive priority.

        Same tier as Short Ask / Recommendation: a running workflow card is
        suspended (hidden, never cancelled) by higher tiers and restored on the
        next Firefly click.
        """
        if self.workflow_card is None:
            return
        self._dismiss_business_popovers()
        self._hide_bubble()
        self.suspend_short_ask()
        self.suspend_recommendation()
        if self.permission_card is not None and self.permission_card.isVisible():
            return
        self.workflow_card.resume_show()
        self._position_workflow()

    def suspend_workflow(self) -> None:
        """Hide the workflow card but keep its state (hide, never cancel)."""
        if self.workflow_card is None or not self.workflow_card.isVisible():
            return
        self.workflow_card.hide_card()

    def _position_workflow(self) -> None:
        card = self.workflow_card
        if card is None:
            return
        screen = (
            QGuiApplication.screenAt(self.pet.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()
        point = QPoint(
            pet_geo.left() - card.width() + theme.BUBBLE_PET_OVERLAP,
            pet_geo.top() - card.height() + theme.BUBBLE_VERTICAL_OVERLAP,
        )
        if point.x() < available.left():
            point.setX(pet_geo.right() - theme.BUBBLE_PET_OVERLAP)
        card.move(self._clamp_point(point, card, available))

    def _toggle_context(self) -> None:
        popover = self._active_popover()
        if popover is None:
            return
        if popover.isVisible():
            popover.dismiss()
        else:
            self._show_context(self._active_context)

    def _show_context(self, context: str) -> None:
        popover = self.session_popover if context == "sessions" else self.workspace_popover
        if popover is None:
            return
        if self.settings_popover is not None and self.settings_popover.isVisible():
            self.settings_popover.dismiss()
        self._active_context = context
        self._hide_bubble()
        self.suspend_short_ask()
        self.suspend_recommendation()
        self.suspend_workflow()
        popover.refresh()
        self._position_popover(popover)

    def _show_workspace(self) -> None:
        self._context_switching = True
        try:
            if self.session_popover is not None and self.session_popover.isVisible():
                self.session_popover.dismiss()
            self._show_context("workspace")
        finally:
            self._context_switching = False

    def _show_sessions(self) -> None:
        self._context_switching = True
        try:
            if self.workspace_popover is not None and self.workspace_popover.isVisible():
                self.workspace_popover.dismiss()
            self._show_context("sessions")
        finally:
            self._context_switching = False

    def _position_popover(self, popover) -> None:
        screen = (
            QGuiApplication.screenAt(self.toolbar.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        available = screen.availableGeometry()
        pet_geo = self.pet.frameGeometry()
        toolbar_geo = self.toolbar.frameGeometry()
        gap = theme.POPOVER_ANCHOR_GAP

        cluster_left = min(pet_geo.left(), toolbar_geo.left())
        cluster_right = max(pet_geo.right(), toolbar_geo.right())

        x = cluster_left - gap - popover.width()
        if x < available.left():
            x = cluster_right + gap

        y = toolbar_geo.center().y() - popover.height() // 2
        point = self._clamp_point(QPoint(x, y), popover, available)
        popover.show_at(point)

    def _visible_popover(self):
        if self.workspace_popover is not None and self.workspace_popover.isVisible():
            return self.workspace_popover
        if self.session_popover is not None and self.session_popover.isVisible():
            return self.session_popover
        if self.settings_popover is not None and self.settings_popover.isVisible():
            return self.settings_popover
        return None

    def eventFilter(self, watched, event) -> bool:
        visible = self._visible_popover()
        if visible is not None:
            if event.type() == QEvent.MouseButtonPress:
                global_pos = event.globalPosition().toPoint()
                inside_popover = visible.frameGeometry().contains(global_pos)
                inside_toolbar = self.toolbar.frameGeometry().contains(global_pos)
                if not inside_popover and not inside_toolbar:
                    visible.dismiss()
        if self.short_ask is not None and self.short_ask.isVisible():
            if event.type() == QEvent.MouseButtonPress:
                global_pos = event.globalPosition().toPoint()
                inside_panel = self.short_ask.frameGeometry().contains(global_pos)
                inside_toolbar = self.toolbar.frameGeometry().contains(global_pos)
                if not inside_panel and not inside_toolbar:
                    self.suspend_short_ask()
        if self.recommendation_card is not None and self.recommendation_card.isVisible():
            if event.type() == QEvent.MouseButtonPress:
                global_pos = event.globalPosition().toPoint()
                if not self.recommendation_card.frameGeometry().contains(global_pos):
                    # Clicking away declines the recommendation.
                    self.recommendation_card.dismiss()
        # PageLens is a persistent reading panel — outside-click does NOT
        # close it. The user must explicitly toggle the toolbar button or
        # press Esc (handled by the panel itself if needed).
        return super().eventFilter(watched, event)

    @staticmethod
    def _clamp_point(point: QPoint, widget: QWidget, available: QRect) -> QPoint:
        maximum_x = max(available.left(), available.right() - widget.width() + 1)
        maximum_y = max(available.top(), available.bottom() - widget.height() + 1)
        return QPoint(
            min(max(point.x(), available.left()), maximum_x),
            min(max(point.y(), available.top()), maximum_y),
        )
