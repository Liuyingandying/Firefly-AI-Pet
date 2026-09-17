"""Firefly Phase 8A.2 visual shell composition root.

The application keeps the proven state broker and animation contract while
presenting Firefly as a small constellation of character-anchored overlays.
The legacy CompanionPanel remains on disk but is not imported or shown.
"""

from __future__ import annotations

import logging
import ctypes
import json
import os
import sys
import time
from pathlib import Path

import keyboard

log = logging.getLogger("firefly.app")

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

# ---------------------------------------------------------------------------
# Global hotkey manager (keyboard hook)
# ---------------------------------------------------------------------------
_HOTKEY_SHORTCUT = "ctrl+alt+shift+l"
_HOTKEY_LABEL = "Ctrl+Alt+Shift+L"


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
from core.pagelens_bridge import PageLensBridge
from core.quick_ask_metrics import MetricsWriter
from core.quick_tools import QuickToolsRegistry
from core.routing_models import HandoffMode, TaskRequest
from core.session_manager import SessionManager
from core.session_store import SessionStore
from core.settings_manager import SettingsManager
from core.pet_state_resolver import PetStateResolver
from core.visual_state_filter import VisualStateFilter
from core.runtime_bus import RuntimeBus, RuntimeState
from core.runtime_state_aggregator import RuntimeStateAggregator
from core.zcode_state_poller import ZCodeStatePoller
from core.state_monitor import StateMonitor
from voice_client import VoiceAnnouncer  # Voice-1.2: 回复语音播报 (失败不影响聊天)
from core.screen_vision.screen.capture import ScreenCaptureService
from core.pdf_visual_region import (
    DEFAULT_UPSCALE,
    build_visual_prompt,
    classify_region,
    crop_region,
    gather_region_ocr,
)
from ui.runtime_state_debug import RuntimeStateVerifier
from core.pet_activity_controller import PetActivityController
from core.workflow_coordinator import WorkflowCoordinator, WorkflowTransitionError
from core.workflow_models import (
    ArtifactKind,
    WorkflowEventType,
    WorkflowState,
    WorkflowStepIntent,
)
from core.workspace_manager import WorkspaceManager
from core.plugin_loader import PluginLoader
from core.extension_api import ExtensionMemory
from core.user_paths import get_user_data_paths, initialize_user_data
from ui import theme
from ui.agent_dock import AgentDock
from ui.character_conversation_runner import CharacterConversationRunner
from ui.overlay_coordinator import OverlayCoordinator
from ui.permission_card import PermissionCard
from ui.pet_overlay import PetOverlay
from ui.process_launcher import ProcessLauncher, QuickAskRunner
from ui.quick_chat_protocol import classify_short_ask
from ui.recommendation_card import RecommendationCard
from ui.quick_tools_popover import QuickToolsPopover
from ui.settings_popover import SettingsPopover
from ui.short_ask import AskPill, ShortAskPanel
from ui.speech_bubble import SpeechBubble
from ui.system_tray import FireflySystemTray
from ui.vertical_toolbar import VerticalToolbar
from ui.workflow_card import WorkflowCard
from ui.workflow_executor import PlanStepExecutor
from ui.workflow_implement_executor import ImplementStepExecutor
from ui.workspace_store import WorkspaceStore
from ui.memory_panel import MemoryPanel
from ui.workflow_review_executor import ReviewStepExecutor
from ui.workspace_popover import WorkspacePopover
from ui.pagelens_panel import PageLensPanel
from ui.explain_box import ExplainBox
from ui.global_hotkey import (
    MOD_CONTROL,
    MOD_SHIFT,
    VK_E,
    VK_ESCAPE,
    GlobalHotkeyManager,
)
from ui.pdf_selection_overlay import OverlayMode, PdfSelectionOverlay, OverlayState
from ui.pdf_visual_worker import PdfVisualRegionRelay, PdfVisualRegionWorker
from ui.paperlens_reader import PdfReaderPanel
from core.paper_context import PaperContextStore
from core.pdf_ambient_context import PdfAmbientEngine
from core.pdf_viewport_context import PdfViewportContextBuilder, viewport_concepts
from ui.pdf_viewport_watcher import PdfViewportWatcher


class _HotkeyManager(QObject):
    """Register the PageLens toggle through a Python global keyboard hook."""

    triggered = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._hotkey_handle = None
        self.active_shortcut = _HOTKEY_LABEL

    def register(self) -> bool:
        if self._hotkey_handle is not None:
            return True
        self._hotkey_handle = keyboard.add_hotkey(
            _HOTKEY_SHORTCUT,
            self._on_hotkey,
            suppress=False,
            trigger_on_release=False,
        )
        print(
            f"[PageLens Hotkey] registered {self.active_shortcut} "
            "via keyboard hook",
            flush=True,
        )
        return True

    def _on_hotkey(self) -> None:
        print(
            f"[PageLens Hotkey] triggered {self.active_shortcut}",
            flush=True,
        )
        self.triggered.emit()

    def unregister(self) -> None:
        if self._hotkey_handle is None:
            return
        keyboard.remove_hotkey(self._hotkey_handle)
        self._hotkey_handle = None


PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets" / "animations"
ICON_FILE = PROJECT_DIR / "assets" / "firefly.ico"
USER_PATHS = get_user_data_paths()
RUNTIME_DIR = USER_PATHS.runtime
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


def _pdf_window_needles(pdf) -> list[str]:
    """Title needles identifying the browser window showing the open PDF —
    the same fields the viewport watcher matches (title, file name, URL
    host + last path segment). The browser window title carries the ACTIVE
    tab's title, so an inactive paper tab correctly fails to match."""
    needles: list[str] = []
    for value in (getattr(pdf, "title", ""), getattr(pdf, "file_name", "")):
        if value:
            needles.append(str(value).lower())
    url = getattr(pdf, "url", "") or ""
    if url:
        from urllib.parse import unquote, urlparse

        try:
            parsed = urlparse(url)
            if parsed.netloc:
                needles.append(parsed.netloc.lower())
            last_segment = unquote(parsed.path.rsplit("/", 1)[-1]).lower()
            if last_segment:
                needles.append(last_segment)
        except ValueError:
            pass
    return [needle for needle in needles if needle]


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
        self._exiting = False

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
        # Resident Windows tray: left-click toggle, dynamic plugin submenu, the
        # only 退出 path. Created with the shell; removed in shutdown().
        self.system_tray = FireflySystemTray(
            controller=self,
            icon_path=str(ICON_FILE),
        )
        self.system_tray.show()
        self.quick_tools_registry = QuickToolsRegistry()
        self.plugin_loader = PluginLoader(
            self.quick_tools_registry, parent=self, persistence=self.settings
        )
        self.plugin_loader.load()
        # Quick Tools is the plugin enable/disable manager: the popover's
        # On/Off toggles drive the loader's persisted enabled state (the
        # single source of truth), never an Open/navigate action.
        self.quick_tools_popover = QuickToolsPopover(
            self.quick_tools_registry,
            plugin_is_enabled=self.plugin_loader.is_plugin_enabled,
            plugin_set_enabled=self.plugin_loader.set_plugin_enabled,
        )
        self.permission_card = PermissionCard()
        self.settings_popover = SettingsPopover(self.settings)
        self.ask_pill = AskPill()
        self.short_ask = ShortAskPanel()
        self.character_conversation = CharacterConversationRunner(
            parent=self,
            screen_vision_settings=self.settings,
            camera_vision_enabled=lambda: self.plugin_loader.is_plugin_enabled(
                "firefly-camera-vision"
            ),
            video_analysis_enabled=lambda: self.plugin_loader.is_plugin_enabled(
                "firefly-video"
            ),
            tju_retrieval_enabled=lambda: self.plugin_loader.is_plugin_enabled(
                "tju-info-retrieval"
            ),
            tju_retrieval_search=getattr(
                self.plugin_loader.plugin("tju-info-retrieval"), "search", None
            ),
            tju_retrieval_open_login=getattr(
                self.plugin_loader.plugin("tju-info-retrieval"), "open_login", None
            ),
            tju_retrieval_open_ui=getattr(
                self.plugin_loader.plugin("tju-info-retrieval"), "open_ui", None
            ),
        )
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
        self.pagelens = PageLensPanel()
        # P0.3: retain ExplainBox as the proven consent/worker controller,
        # but forbid its top-level window in the production reading flow.
        self.explain_box = ExplainBox()
        self.explain_box.enable_single_surface_mode(True)
        # PDF OCR Overlay Phase 3-B: one-shot selection overlay over the
        # browser PDF viewer; its fresh capture→OCR snapshot feeds the same
        # ExplainBox entry the OCR concept chips use.
        self.pdf_selection_overlay = PdfSelectionOverlay()
        self._pdf_capture_service = ScreenCaptureService()
        # Phase 3-C: minimal global hotkeys (RegisterHotKey + Qt native
        # event filter). "PDF 划词" entry shortcut; Escape is registered only
        # while the overlay mode is active and unregistered on exit.
        self._global_hotkeys = GlobalHotkeyManager()
        _app = QApplication.instance()
        if _app is not None:
            _app.installNativeEventFilter(self._global_hotkeys)
        self._hotkey_enter_id = self._global_hotkeys.register(
            MOD_CONTROL | MOD_SHIFT, VK_E, self._on_global_hotkey
        )
        self._hotkey_cancel_id = None
        # PaperLens 2.2: independent paper reading mode (left render, right
        # page concept); selection forwards to the existing explain entry.
        self.reader_panel = PdfReaderPanel()
        # Which surface produced the PageLens chips last: "extension" (HTML
        # concepts -> browser sidebar explain) or "viewport" (OCR concepts ->
        # desktop explain entry).
        self._pagelens_concepts_source = "extension"
        self._active_workflow_id: str | None = None
        # PageLens Bridge (Phase 9B)
        self.pagelens_bridge = PageLensBridge(parent=self)
        self.pagelens_bridge.start()
        # Explain gate: PageLens may only enter the explanation surface after
        # an explicit user action (Explain entry, 授权并解释, concept chip).
        # Ambient/background updates (plain highlights, reading context, OCR,
        # camera vision, provider info) stay compact. Set to True by the
        # user-action entry points, reset to False by the next ambient
        # selection; bridge concept results are dropped while the gate is off.
        self._explain_gate_user_action = False
        # Connect bridge signals to panel
        self.pagelens_bridge.concept_loading.connect(self._on_bridge_concept_loading)
        self.pagelens_bridge.concept_card.connect(self._on_bridge_concept_card)
        self.pagelens_bridge.concept_error.connect(self._on_bridge_concept_error)
        self.pagelens_bridge.explain_intent.connect(self._on_bridge_explain_intent)
        self.pagelens_bridge.concepts.connect(self._on_bridge_concepts)
        self.pagelens_bridge.question_loading.connect(
            lambda payload: self.pagelens.show_question_loading(
                payload.get("parent_term", ""), payload.get("question", "")
            )
        )
        self.pagelens_bridge.question_delta.connect(self.pagelens.append_question_delta)
        self.pagelens_bridge.question_done.connect(self.pagelens.finish_question)
        self.pagelens_bridge.question_error.connect(self.pagelens.show_question_error)
        self.pagelens_bridge.bridge_connected.connect(self.pagelens.set_bridge_connected)
        self.pagelens_bridge.bridge_disconnected.connect(self.pagelens.set_bridge_connected)
        # Connect panel signals to bridge. PDF viewport concepts (Ambient PDF
        # Vision) cannot use this hop — the sealed viewer has no content
        # script — so their clicks route to the desktop explain entry instead
        # (see _on_pagelens_concept and friends).
        self.pagelens.related_requested.connect(self._on_pagelens_related)
        self.pagelens_bridge.action_open_related.connect(self.pagelens.related_requested.emit)
        self.pagelens.question_requested.connect(self._on_pagelens_question)
        self.pagelens_bridge.action_open_question.connect(self.pagelens.question_requested.emit)
        self.pagelens.concept_requested.connect(self._on_pagelens_concept)
        self.pagelens_bridge.action_open_concept.connect(self.pagelens.concept_requested.emit)
        self.pagelens.back_requested.connect(self.pagelens_bridge.send_back)
        self.pagelens_bridge.action_back.connect(self.pagelens.back_requested.emit)
        self.agent_router = AgentRouter()
        self.coordinator = OverlayCoordinator(
            self.pet,
            self.dock,
            self.bubble,
            self.toolbar,
            self,
            workspace_popover=self.workspace_popover,
            # OverlayCoordinator retains its legacy parameter name so existing
            # Workspace switching behavior stays stable; the visual panel is
            # now the Quick Tools popover, not Session management.
            session_popover=self.quick_tools_popover,
            permission_card=self.permission_card,
            settings_popover=self.settings_popover,
            settings_manager=self.settings,
            ask_pill=self.ask_pill,
            short_ask=self.short_ask,
            recommendation_card=self.recommendation_card,
            workflow_card=self.workflow_card,
            pagelens_panel=self.pagelens,
            explain_box=self.explain_box,
            reader_panel=self.reader_panel,
            single_reading_surface=True,
        )
        # Close button → coordinator.hide_pagelens()
        self.pagelens.close_requested.connect(self.coordinator.hide_pagelens)
        # ExplainBox now acts only as a controller. Its worker results and
        # existing consent gate are rendered by the one visible PageLens.
        self.explain_box.surface_loading.connect(self._on_explain_surface_loading)
        self.explain_box.surface_consent_required.connect(
            self._on_explain_surface_consent_required
        )
        self.explain_box.surface_card.connect(self._on_explain_surface_card)
        self.explain_box.surface_error.connect(self._on_explain_surface_error)
        self.pagelens.consent_requested.connect(
            self.explain_box.grant_pending_selection_consent
        )
        # PDF OCR Overlay Phase 3-B: toolbar "PDF 划词" → fresh capture →
        # OCR → arm the overlay; a completed gesture feeds the same
        # ExplainBox entry as the OCR concept chips. Cancel/empty/failed
        # paths never open the ExplainBox.
        self.coordinator.pdf_select_requested.connect(self._on_pdf_overlay_requested)
        self.pdf_selection_overlay.selection_completed.connect(
            self._on_pdf_overlay_selection
        )
        self.pdf_selection_overlay.selection_cancelled.connect(
            self._on_pdf_overlay_cancelled
        )
        self.pdf_selection_overlay.snapshot_failed.connect(
            self._on_pdf_overlay_snapshot_failed
        )
        # Phase 4-B: visual region explain — toolbar "区域解释" reuses the
        # same fresh snapshot pipeline in VISUAL_REGION mode; the region is
        # cropped in memory, explained by the shared vision provider off-GUI,
        # and routed into the ExplainBox.
        self.coordinator.pdf_visual_requested.connect(self._on_pdf_visual_requested)
        # UI Consolidation P0: the PDF selection/region handlers stay intact,
        # but their user-facing entry points now live inside PageLens.
        self.pagelens.selection_requested.connect(self._on_pdf_overlay_requested)
        self.pagelens.visual_region_requested.connect(self._on_pdf_visual_requested)
        self.pdf_selection_overlay.visual_region_completed.connect(
            self._on_visual_region_selected
        )
        self.pdf_visual_relay = PdfVisualRegionRelay(parent=self)
        self.pdf_visual_relay.finished.connect(
            self._on_visual_region_finished, Qt.QueuedConnection
        )
        self.pdf_visual_relay.error.connect(
            self._on_visual_region_error, Qt.QueuedConnection
        )
        self._visual_generation = 0
        self._visual_region_busy = False
        self._visual_kind_label = "区域"
        # PaperLens 2.2: reader selection → existing ExplainBox explain entry
        # (consent gate + async worker + PdfQa) — no AI here.
        self.reader_panel.explain_requested.connect(self._on_reader_explain)
        # Ambient Reading Mode: the page_context/selection messages from the
        # browser feed the in-RAM Context Layer (single current page) and the
        # status strip. No Memory / database / long-term storage.
        self.paper_context = PaperContextStore()
        # P0.3: ambient sensing remains active, but there is no second visible
        # reading-status surface. PageLens compact context is the only UI.
        self.ambient_status = None
        self.pagelens_bridge.page_context.connect(self._on_browser_page_context)
        # Ambient PDF v1.0: pdf_opened (tabs.url/title) feeds the engine;
        # pdf_view_state is the reserved page interface (page may be null).
        self.pdf_ambient_engine = PdfAmbientEngine()
        self.pagelens_bridge.pdf_opened.connect(self._on_pdf_opened)
        self.pagelens_bridge.pdf_view_state.connect(self._on_pdf_view_state)
        # Ambient PDF Vision v1: low-frequency viewport OCR (2s fingerprint
        # gate, local RapidOCR, no VLM, no disk) — see the audit.
        self.pdf_viewport_builder = PdfViewportContextBuilder()
        self.pdf_viewport_watcher = PdfViewportWatcher(
            self.paper_context,
            builder=self.pdf_viewport_builder,
            parent=self,
        )
        self.pdf_viewport_watcher.viewport_context.connect(
            self._on_pdf_viewport_context
        )
        # PaperLens Bridge: browser selections (extension → WebSocket) surface
        # into ExplainBox for the explain entry. No AI here — the panel owns
        # the consent gate and the existing PdfQa.explain_selection path.
        self.pagelens_bridge.selection.connect(self._on_browser_selection)
        # Global keyboard hook → queued coordinator.toggle_pagelens().
        self.hotkey_manager = _HotkeyManager(self)
        self.hotkey_manager.triggered.connect(
            self.coordinator.toggle_pagelens,
            Qt.ConnectionType.QueuedConnection,
        )
        self.hotkey_manager.register()
        self.state_monitor = StateMonitor(
            RUNTIME_DIR / "sources",
            STATE_FILE,
            parent=self,
        )
        # Runtime bus (Phase 1A): pure fan-out. StateMonitor stays the sole
        # state authority; the bus never reads sources or resolves states.
        self.runtime_bus = RuntimeBus(parent=self)
        # Vision-1B: attach the bus to the character runner so it can publish
        # session events (camera.observed) after camera vision turns.
        self.character_conversation.set_runtime_bus(self.runtime_bus)
        # Extension API v2 (Phase 1): inject runtime services into the shared
        # plugin context. The bus exists now and the companion's MemoryService
        # was constructed with character_conversation above, so both are safe
        # to hand to plugins here. Plugins only use them from start()/open().
        self.plugin_loader.sync_services(
            bus=self.runtime_bus,
            memory=ExtensionMemory(self.character_conversation.memory_service),
            settings=self.settings,
        )
        # Phase 3B-2: connect existing coordinators to the bus.
        self.workflow_coordinator.set_bus(self.runtime_bus)
        # Phase 5F: aggregate bus events into runtime.activity — closes the
        # Phase 5E T3 gap (module existed but was never composed here).
        self.runtime_state_aggregator = RuntimeStateAggregator(
            self.runtime_bus, parent=self
        )
        # Phase 5D: read-only Z Code Harness adapter; started with the shell,
        # closed before the bus on shutdown.
        self.zcode_poller = ZCodeStatePoller(self.runtime_bus, parent=self)
        # Phase 1B shadow verifier: compares old (resolved_state_changed) and
        # new (RuntimeBus) paths. Temporary — remove after validation passes.
        self._state_verifier = RuntimeStateVerifier(parent=self)
        # Phase 2B activity controller: maps RuntimeActivityState to pet
        # display strings.  Shadow mode — the old StateMonitor → PetOverlay
        # path below stays intact.
        self._activity_controller = PetActivityController(self.runtime_bus, parent=self)
        # Phase 2C resolver: arbitrates between old StateMonitor and new
        # RuntimeActivity paths. The resolver's output is the single source
        # of truth for pet.apply_state.
        self._state_resolver = PetStateResolver(parent=self)
        # Detailed six-state feed (Phase 4): carries TOOL_RUNNING /
        # WAITING_INPUT to the resolver so the LED detail output sees the
        # true activity; the four-state display path is unchanged.
        self._activity_controller.pet_detail_state_changed.connect(self._state_resolver.update_detail_state)
        self._activity_controller.pet_state_changed.connect(self._state_resolver.update_activity_state)
        self._state_resolver.pet_display_state_changed.connect(self.pet.apply_state)
        # Hardware display (ESP32 Prism HUD): write resolver's final state
        # to runtime/hardware_state.json for the serial bridge to consume.
        self._state_resolver.pet_display_state_changed.connect(self._write_hardware_state)
        # LED node (ESP32-S3 WS2812B): write the resolver's final detailed
        # six-state to runtime/led_state.json for the LED bridge to consume.
        # VisualStateFilter (Phase 4.5) debounces short TOOL_RUNNING blips so
        # the LEDs only switch on tools that actually persist.
        self._visual_state_filter = VisualStateFilter(parent=self)
        self._state_resolver.pet_detail_state_changed.connect(self._visual_state_filter.offer)
        self._visual_state_filter.state_filtered.connect(self._write_led_state)
        self.notification_manager = NotificationManager()
        self.keep_awake = KeepAwakeService()
        self.notification_manager.set_enabled(self.settings.notifications_enabled)
        self.keep_awake.set_enabled(self.settings.keep_awake_enabled)
        self.settings.connect(self._on_settings_changed)
        self.notification_manager.connect(self.coordinator.on_notification)
        # Phase 3B-3: forward notification events to RuntimeBus.
        self.notification_manager.set_bus(self.runtime_bus)

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

        self.pet.hide_requested.connect(self.hide_firefly)
        self.pet.scale_mode_toggled.connect(self._on_scale_mode_toggled)
        self.pet.scale_wheel.connect(self._on_scale_wheel)
        self.pet.scale_exit_requested.connect(self._on_scale_exit)
        self.dock.launch_agent.connect(self._on_dock_agent_selected)
        self.state_monitor.agent_state_changed.connect(self._on_agent_state_changed)
        self.state_monitor.resolved_state_changed.connect(self._on_resolved_state_changed)
        # Additive bus feed only — the legacy connects above stay authoritative.
        self.state_monitor.resolved_state_changed.connect(self._publish_runtime_state)
        # Phase 1B shadow: records old-path + new-path for consistency check.
        self.state_monitor.resolved_state_changed.connect(self._state_verifier.on_old_path)
        self.runtime_bus.subscribe_state(self._state_verifier.on_new_path)
        self.coordinator.permission_view_requested.connect(self._on_permission_view)
        self.coordinator.short_ask_requested.connect(self._on_short_ask_requested)
        self.short_ask.send_requested.connect(self._on_short_ask_send)
        self.short_ask.force_send_requested.connect(self._on_short_ask_force_send)
        self.short_ask.retry_requested.connect(self._on_short_ask_retry)
        self.short_ask.stop_requested.connect(self._on_short_ask_stop)
        self.short_ask.open_agent_requested.connect(self._on_short_ask_open_agent)
        self.character_conversation.agent_event.connect(self.short_ask.on_agent_event)
        # Phase 3B-1: forward AgentEvents to RuntimeBus for the aggregator.
        self.character_conversation.agent_event.connect(self._publish_agent_event_from_character)
        # Voice-1.2: FINAL 回复交给 Voice Module 朗读 (后台线程, 失败仅日志)
        self.voice_announcer = VoiceAnnouncer()
        self.voice_announcer.attach(self.character_conversation)
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
        self.quick_ask.agent_event.connect(self._publish_agent_event_from_quick_ask)
        # Phase 4C: workflow executor AgentEvent → RuntimeBus bridge.
        self.plan_executor.agent_event.connect(self._publish_agent_event_from_plan_executor)
        self.implement_executor.agent_event.connect(self._publish_agent_event_from_implement_executor)
        self.review_executor.agent_event.connect(self._publish_agent_event_from_review_executor)
        self.quick_ask.finished.connect(self._on_short_ask_finished)
        self.quick_ask.failed.connect(self._on_short_ask_failed)
        self.quick_ask.telemetry.connect(self._on_quick_ask_telemetry)
        if self._server is not None:
            self._server.newConnection.connect(self._on_control_connection)
        # MemoryPanel — lazy singleton, wired to toolbar "memory" action.
        self._memory_panel: MemoryPanel | None = None
        self.coordinator.memory_panel_requested.connect(self._ensure_memory_panel)
        # Scratchpad v1 — lazy window + pet drop handler (temporary inbox;
        # never touches Memory / Conversation / Learning stores).  One shared
        # service instance backs both the pet drop path and the window, so
        # deletes performed in the window are visible to subsequent drops.
        self._scratchpad_service = None
        self._scratchpad_window: "ScratchpadWindow | None" = None
        self.coordinator.scratchpad_requested.connect(self._ensure_scratchpad_window)
        self.pet.set_drop_handler(self._scratchpad_drop_controller())
        self.settings_popover.memory_requested.connect(self._open_memory_from_settings)
        # UI V2 CompanionConsole — toolbar "console" action opens the singleton.
        self.coordinator.console_requested.connect(self._ensure_companion_console)

    def _ensure_companion_console(self) -> None:
        """Open (or focus) the AI Pet console bound to the character runner."""
        from ui.v2.console import open_singleton as _open_console

        console = _open_console(
            runner=self.character_conversation,
            runtime_bus=getattr(self, "runtime_bus", None),
        )
        try:
            console.settings_requested.disconnect()
        except (RuntimeError, TypeError):
            pass
        try:
            console.research_requested.disconnect()
        except (RuntimeError, TypeError):
            pass
        console.settings_requested.connect(self._on_console_settings)
        console.research_requested.connect(self._on_console_research)

    def _on_console_settings(self) -> None:
        self.coordinator._toggle_settings()

    def _on_console_research(self) -> None:
        # Quick Tools live behind the coordinator's "sessions" popover slot.
        if getattr(self, "coordinator", None) is not None:
            self.coordinator._show_context("sessions")

    def start(self) -> None:
        self._write_pid()
        # Restore persisted native sessions so Short Talk resumes across a
        # Firefly restart. Safe even before the UI is shown.
        self.session_manager.load()
        self.state_monitor.start()
        # Phase 5D: start observing Z Code Harness runtime state (read-only).
        self.zcode_poller.start()
        # Extension API v2 (Phase 1): start background work on loaded
        # extensions now that all services are wired and the bus is live.
        self.plugin_loader.start_all()
        # Apply the persisted ui_scale before the first paint (no 100% flash).
        theme.set_ui_scale(self.settings.ui_scale)
        self.coordinator.show_shell_pet_only()

    # -------------------------------------------------------- Firefly lifecycle
    #
    # hide != exit. show/hide/toggle only ever touch the Firefly pet body
    # (PetOverlay); plugin windows, plugin background tasks and QApplication
    # are never affected. exit_application() is the only path to QApplication.
    # Cleanup runs through aboutToQuit -> shutdown(); it is never copied here.

    def show_firefly(self) -> None:
        """Show the Firefly pet body only."""
        self.pet.show()

    def hide_firefly(self) -> None:
        """Hide the Firefly pet body only. Never quits, never shuts down."""
        self.pet.hide()

    def toggle_firefly(self) -> None:
        """Toggle the Firefly pet body visibility (single source of truth)."""
        if self.pet.isVisible():
            self.hide_firefly()
        else:
            self.show_firefly()
            self.pet.raise_()

    def show_settings(self) -> None:
        """Reuse the existing settings popover toggle."""
        self.coordinator._toggle_settings()

    def exit_application(self) -> None:
        """The only real-exit trigger: flag + QApplication.quit().

        All cleanup continues through aboutToQuit -> shutdown(); nothing is
        duplicated here. Guarded so a second exit request is a no-op.
        """
        if self._shutting_down:
            return
        self._exiting = True
        QApplication.quit()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.state_monitor.stop()
        # Phase 3-C: release all global hotkeys (incl. the temporary Escape)
        # so no key stays swallowed after shutdown.
        hotkeys = getattr(self, "_global_hotkeys", None)
        if hotkeys is not None:
            hotkeys.unregister_all()
        # Phase 5D: stop Z Code observation before tearing down the bus.
        self.zcode_poller.close()
        # Phase 5F: stop the aggregator after producers, before the bus.
        self.runtime_state_aggregator.close()
        # Extension API v2 (Phase 1): stop extension background work BEFORE the
        # bus closes so their final events are still deliverable.
        self.plugin_loader.stop_all()
        self.runtime_bus.close()
        self._activity_controller.close()
        self.keep_awake.shutdown()
        self.quick_ask.shutdown()
        self.character_conversation.stop()
        self._persist_ui_scale()
        if self.plan_executor.running:
            self.plan_executor.stop()
        if self.review_executor.running:
            self.review_executor.stop()
        if self.implement_executor.running:
            self.implement_executor.stop()
        self.implement_executor.reset()
        self.plugin_loader.shutdown()
        self.coordinator.close_overlays()
        if getattr(self, "ambient_status", None) is not None:
            self.ambient_status.close()
        # Stop PageLens bridge before closing overlays (panel needs it)
        self.pagelens_bridge.stop()
        # Unregister global hotkey
        if hasattr(self, "hotkey_manager"):
            self.hotkey_manager.unregister()
        self.pet.shutdown()
        # Remove the tray icon so it disappears from the notification area.
        if self.system_tray is not None:
            self.system_tray.hide()
            self.system_tray.deleteLater()
            self.system_tray = None
        if self._server is not None:
            self._server.close()
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

        # Close MemoryPanel on shutdown.
        if self._memory_panel is not None:
            self._memory_panel.close()

    def _scratchpad_drop_controller(self):
        if self._scratchpad_service is None:
            from core.scratchpad.service import ScratchpadService
            from core.scratchpad.store import ScratchpadStore

            self._scratchpad_service = ScratchpadService(
                ScratchpadStore(USER_PATHS.scratchpad)
            )
        from ui.scratchpad_drop import ScratchpadDropController

        return ScratchpadDropController(self._scratchpad_service)

    def _ensure_scratchpad_window(self) -> None:
        """Open (or focus) the 临时记事本 (Scratchpad v1 temporary inbox)."""
        if self._scratchpad_window is None:
            from ui.scratchpad_window import ScratchpadWindow

            self._scratchpad_window = ScratchpadWindow(
                self._scratchpad_drop_controller()
            )
        self._scratchpad_window.refresh()
        self._scratchpad_window.show()
        self._scratchpad_window.raise_()
        self._scratchpad_window.activateWindow()

    def _ensure_memory_panel(self) -> None:
        """Lazy-create MemoryPanel with the same services used by CompanionRuntime."""
        if self._memory_panel is None:
            memory_svc = getattr(self.character_conversation, "memory_service", None)
            suggestion_svc = getattr(self.character_conversation, "suggestion_service", None)
            self._memory_panel = MemoryPanel(
                memory_service=memory_svc,
                bond_state_engine=(
                    getattr(
                        getattr(self.character_conversation, "runtime", None),
                        "_runtime", None
                    )
                ).bond_state_engine
                if getattr(self.character_conversation, "runtime", None) is not None
                else None,
                suggestion_service=suggestion_svc,
            )
            # Narrative provider: read from the memory repository.
            _ms = memory_svc
            if _ms is not None:
                _repo = getattr(_ms, "repository", None)
                if _repo is not None:
                    _records = getattr(_repo, "records", None)
                    if callable(_records):
                        self._memory_panel.narrative_provider = _records
            close_requested = getattr(self._memory_panel, "close_requested", None)
            if close_requested is not None:
                close_requested.connect(self._on_memory_panel_close)
        # Refresh data before showing so recent chat results are visible.
        self._memory_panel.refresh()
        self._memory_panel.show()
        self._memory_panel.raise_()
        self._memory_panel.activateWindow()

    def _open_memory_from_settings(self) -> None:
        """Open the existing MemoryPanel from the consolidated Settings entry."""
        if self.settings_popover.isVisible():
            self.settings_popover.dismiss()
        self._ensure_memory_panel()

    def _on_memory_panel_close(self) -> None:
        if self._memory_panel is not None:
            self._memory_panel.hide()

    def _on_agent_state_changed(self, agent_id: str, state: AgentState) -> None:
        # The launcher dock has no per-agent state display anymore.
        self.coordinator.on_agent_state(agent_id, state)
        self.notification_manager.on_agent_state(state)
        self.keep_awake.on_agent_state(state)

    def _on_dock_agent_selected(self, agent_id: str) -> None:
        # The dock is now a launcher: each click opens the CLI / app for the
        # CURRENTLY SELECTED workspace. Failures show a lightweight message
        # and never crash Firefly.
        from ui.agent_launcher import (
            launch_claude,
            launch_codex,
            launch_qwen_yolo,
            launch_zcode,
        )

        launchers = {
            "claude": launch_claude,
            "codex": launch_codex,
            "qwen": launch_qwen_yolo,
            "zcode": launch_zcode,
        }
        launcher = launchers.get(agent_id)
        if launcher is None:
            return
        ok, message = launcher(self.workspace_manager.current())
        if not ok:
            try:
                self.bubble.show_message(
                    agent_id.title(),
                    message,
                    duration_ms=3_200,
                    accent=theme.ERROR_STATUS,
                )
            except Exception:
                pass

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

    def _publish_runtime_state(self, state: ResolvedState) -> None:
        self.runtime_bus.publish_state(RuntimeState.from_resolved(state))

    # -- Hardware display (ESP32 Prism HUD) --------------------------------

    def _write_hardware_state(self, display: str) -> None:
        """Atomically write the resolver's final display state for the bridge."""
        path = RUNTIME_DIR / "hardware_state.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({"state": display, "timestamp": int(time.time() * 1000)}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            pass  # bridge will keep last known state

    def _write_led_state(self, detail: str) -> None:
        """Atomically write the resolver's final six-state for the LED bridge."""
        path = RUNTIME_DIR / "led_state.json"
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({"state": detail, "timestamp": int(time.time() * 1000)}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            pass  # LED bridge will keep last known state

    # -- AgentEvent → RuntimeBus bridge (Phase 3B-1) ---------------------

    def _publish_agent_event_from_quick_ask(self, event) -> None:
        self.runtime_bus.publish_event(
            RuntimeEvent(
                kind="agent.event",
                source="quick_ask",
                timestamp=int(time.time() * 1000),
                payload=event,
            )
        )

    def _publish_agent_event_from_character(self, event) -> None:
        self.runtime_bus.publish_event(
            RuntimeEvent(
                kind="agent.event",
                source="character_conversation",
                timestamp=int(time.time() * 1000),
                payload=event,
            )
        )

    # -- Workflow executor AgentEvent → RuntimeBus (Phase 4C) ------------

    def _publish_agent_event_from_plan_executor(self, event) -> None:
        self.runtime_bus.publish_event(
            RuntimeEvent(
                kind="agent.event",
                source="plan_executor",
                timestamp=int(time.time() * 1000),
                payload=event,
            )
        )

    def _publish_agent_event_from_implement_executor(self, event) -> None:
        self.runtime_bus.publish_event(
            RuntimeEvent(
                kind="agent.event",
                source="implement_executor",
                timestamp=int(time.time() * 1000),
                payload=event,
            )
        )

    def _publish_agent_event_from_review_executor(self, event) -> None:
        self.runtime_bus.publish_event(
            RuntimeEvent(
                kind="agent.event",
                source="review_executor",
                timestamp=int(time.time() * 1000),
                payload=event,
            )
        )

    def _on_resolved_state_changed(self, state: ResolvedState) -> None:
        self._state_resolver.update_legacy_state(state)

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

    def _on_bridge_concepts(self, items: list) -> None:
        """Extension concepts arrived (HTML page) — record the source and
        refresh the PageLens chips exactly as the direct connect did."""
        self._pagelens_concepts_source = "extension"
        self.pagelens.set_top_concepts(items)

    def _on_bridge_concept_loading(self, term: str) -> None:
        """Bridge concept-loading may only open the explain surface after an
        explicit user action; ambient results stay compact."""
        if not self._explain_gate_user_action:
            log.info("[App] concept_loading dropped (no user action): %r", term[:60])
            return
        self.pagelens.show_loading(term)

    def _on_bridge_concept_card(self, card: dict) -> None:
        """Bridge concept-card results are gated the same way: a card that
        arrives without a user action must not expand PageLens."""
        if not self._explain_gate_user_action:
            log.info("[App] concept_card dropped (no user action): %r", (card.get("term") or "")[:60])
            return
        self.pagelens.set_concept(card)

    def _on_bridge_concept_error(self, message: str) -> None:
        if not self._explain_gate_user_action:
            log.info("[App] concept_error dropped (no user action): %s", message[:80])
            return
        self.pagelens.show_error(message)

    def _on_bridge_explain_intent(self, source: str) -> None:
        """The browser's own Explain UI asked the desktop AI for a concept
        (ai_chat_request, explain-whitelisted source). That is a user click
        on the browser side: open the gate so the concept result that
        follows is rendered instead of being dropped. Ambient/background
        messages never reach this signal, so the compact-only rule holds.
        """
        self._explain_gate_user_action = True
        log.info("[App] bridge explain intent (gate open): source=%s", source)

    def _ensure_pagelens_reading_surface(self) -> None:
        """Make PageLens the one visible reading surface and keep it anchored."""
        self.explain_box.hide_panel()
        if not self.pagelens.visible:
            self.coordinator.show_pagelens()
        else:
            self.coordinator._position_pagelens()
            self.pagelens.raise_()

    def _on_explain_surface_loading(self, payload: dict) -> None:
        self.pagelens.show_surface_loading(payload)
        self._ensure_pagelens_reading_surface()

    def _on_explain_surface_consent_required(self, payload: dict) -> None:
        self.pagelens.show_consent_request(payload)
        self._ensure_pagelens_reading_surface()

    def _on_explain_surface_card(self, card: dict) -> None:
        self.pagelens.show_surface_card(card)
        self._ensure_pagelens_reading_surface()

    def _on_explain_surface_error(self, message: str) -> None:
        self.pagelens.show_error(message)
        self._ensure_pagelens_reading_surface()

    # -- PaperLens Bridge: browser selection ----------------------------

    def _on_pagelens_concept(self, term: str) -> None:
        """A PageLens concept chip was clicked.

        HTML concepts route to the browser sidebar (original chain). PDF
        viewport concepts route to the desktop explain entry — the sealed
        viewer has no content script to receive desktop actions.
        """
        # A chip click is an explicit user action: it opens the gate for the
        # bridge concept round-trip that follows.
        self._explain_gate_user_action = True
        if self._pagelens_concepts_source == "viewport" and self.paper_context.pdf is not None:
            page = self.paper_context.pdf.current_page
            log.info("[App] PDF concept explain (viewport): %r page=%s", term[:60], page)
            self.explain_box.show_browser_selection(term, page)
            return
        self.pagelens_bridge.send_open_concept(term)

    def _on_pagelens_related(self, term: str) -> None:
        # User action: keep the gate open for the related-concept round trip.
        self._explain_gate_user_action = True
        if self._pagelens_concepts_source == "viewport" and self.paper_context.pdf is not None:
            self._on_pagelens_concept(term)
            return
        self.pagelens_bridge.send_open_related(term)

    def _on_pagelens_question(self, question: str) -> None:
        # User action: keep the gate open for the question round trip.
        self._explain_gate_user_action = True
        if self._pagelens_concepts_source == "viewport" and self.paper_context.pdf is not None:
            self._on_pagelens_concept(question)
            return
        self.pagelens_bridge.send_open_question(question)

    def _on_browser_selection(self, text: str, page: int, source: str = "ambient") -> None:
        """A selection arrived from the browser extension via WebSocket.

        Logs the event (acceptance: Firefly 收到 selection event), feeds the
        Ambient Context Layer, advances the ambient PDF page when the
        selection carries a real page. ONLY an explicit ``source ==
        "user_action"`` selection (e.g. the right-click explain entry) routes
        the explain entry into ExplainBox/PageLens; plain highlights and
        unknown senders are ambient and stay compact (current context +
        chips only). No AI, no PDF parsing here.
        """
        log.info("[App] 收到 selection event: text=%r page=%s source=%s", text[:120], page, source)
        try:
            self.paper_context.update_selection(text, page)
            if page >= 1:
                self.paper_context.update_pdf_page(page)  # 最近解释页 → current_page
            self._refresh_ambient_status()
            pdf = self.paper_context.pdf
            if pdf is not None:
                self.pagelens.set_current_context(
                    pdf.section or pdf.title or pdf.file_name,
                    self._format_pagelens_page(pdf.current_page, pdf.total_pages),
                )
            if source != "user_action":
                # Ambient highlight / background update: compact only. The
                # next bridge concept result must not expand PageLens.
                self._explain_gate_user_action = False
                log.info(
                    "[App] ambient selection (no explain): text=%r page=%s",
                    text[:120], page,
                )
                return
            # Explicit user explain action: open the gate and route the
            # existing explain entry (consent gate + async worker + PdfQa).
            self._explain_gate_user_action = True
            if page < 1 and self._pagelens_concepts_source == "extension":
                # HTML has no PDF page number. Reuse its existing PageLens
                # concept explain flow instead of fabricating a PDF page.
                self.pagelens.show_loading(text)
                self._ensure_pagelens_reading_surface()
                self.pagelens_bridge.send_open_concept(text)
            else:
                self.explain_box.show_browser_selection(text, page)
        except Exception as exc:  # the selection must never crash the shell
            log.warning("[App] browser selection handling failed: %s", type(exc).__name__)

    # -- PDF OCR Overlay (Phase 3-B): toolbar "PDF 划词" ------------------

    def _on_global_hotkey(self, hotkey_id: int) -> None:
        """Phase 3-C: global shortcut dispatch (Ctrl+Shift+E entry, and the
        temporary Escape registered only while the overlay mode is active)."""
        if hotkey_id == self._hotkey_enter_id:
            self._on_pdf_overlay_requested()
        elif hotkey_id == self._hotkey_cancel_id:
            self.pdf_selection_overlay.cancel()

    def _register_pdf_overlay_esc(self) -> None:
        """Temporary global Escape while the 划词 mode is active; removed on
        exit so Esc is never swallowed permanently."""
        if self._hotkey_cancel_id is not None:
            return
        self._hotkey_cancel_id = self._global_hotkeys.register(
            0, VK_ESCAPE, self._on_global_hotkey
        )

    def _unregister_pdf_overlay_esc(self) -> None:
        if self._hotkey_cancel_id is None:
            return
        self._global_hotkeys.unregister(self._hotkey_cancel_id)
        self._hotkey_cancel_id = None

    def _hide_pdf_overlay_ui(self) -> None:
        """Phase 3-C P2: before the Edge capture, temporarily hide Firefly's
        own overlay UIs that would pollute the OCR frame; record what was
        visible so the exact state can be restored."""
        candidates = (
            self.pet,
            self.toolbar,
            self.pagelens,
            self.explain_box,
            self.ambient_status,
            self.reader_panel,
        )
        hidden: list = []
        for widget in candidates:
            if widget is not None and widget.isVisible():
                hidden.append(widget)
                widget.hide()
        self._pdf_overlay_hidden = hidden
        # A short GUI settle so the capture sees a clean browser window.
        QApplication.processEvents()

    def _restore_pdf_overlay_ui(self) -> None:
        """Phase 3-C P2: restore whatever was hidden before the capture.
        Runs after the frame is in hand (or on any failure path) — never
        leaves the pet/toolbar hidden."""
        hidden = getattr(self, "_pdf_overlay_hidden", None) or []
        self._pdf_overlay_hidden = []
        for widget in hidden:
            widget.show()

    def _on_pdf_overlay_requested(self) -> None:
        """Toolbar 'PDF 划词' OR the Ctrl+Shift+E global shortcut: take a
        FRESH snapshot of the browser PDF window and arm the one-shot
        selection overlay. Phase 3-C P4: re-triggering while armed cancels
        the active mode instead of spawning a second overlay/OCR worker."""
        overlay = self.pdf_selection_overlay
        if overlay.state in (
            OverlayState.ARMED,
            OverlayState.SELECTING,
            OverlayState.COMPLETED,
        ):
            overlay.cancel()
            return
        if overlay.state is not OverlayState.IDLE:
            return  # CAPTURING/OCR already running — ignore
        if self.paper_context.pdf is None:
            log.info("[App] pdf overlay: no PDF context, ignored")
            return
        self._register_pdf_overlay_esc()
        overlay.set_mode(OverlayMode.TEXT)
        overlay.request_arm(
            capture_fn=self._capture_pdf_overlay_frame,
            ocr_fn=self._run_pdf_overlay_ocr,
            keep_above=[
                window
                for window in (self.pet, self.toolbar)
                if window is not None
            ],
            before_capture=self._hide_pdf_overlay_ui,
            after_capture=self._restore_pdf_overlay_ui,
        )

    def _capture_pdf_overlay_frame(self):
        """Resolve the browser PDF window by the open PDF's title/file name
        and capture exactly that window. Raises when the window cannot be
        identified (wrong active tab, viewer closed) — never falls back to a
        full-screen capture that would include Firefly's own windows."""
        from core.screen_vision.foreground_tracker import find_window_hwnd

        needles = _pdf_window_needles(self.paper_context.pdf)
        hwnd = find_window_hwnd(needles)
        if not hwnd:
            raise RuntimeError("未找到 PDF 浏览器窗口（论文标签页未激活或已关闭）")
        return self._pdf_capture_service.capture_window_hwnd(hwnd)

    def _run_pdf_overlay_ocr(self, image_bytes: bytes):
        from core.pdf_processor import _get_shared_ocr_backend

        backend = _get_shared_ocr_backend()
        if backend is None or not backend.available:
            raise RuntimeError("OCR 后端不可用")
        return backend.engine(image_bytes)

    def _on_pdf_overlay_selection(self, text: str) -> None:
        """A completed overlay gesture: feed the same ExplainBox entry the
        OCR concept clicks use (consent gate + async worker + PdfQa). An
        empty selection never opens the ExplainBox."""
        self._unregister_pdf_overlay_esc()
        text = (text or "").strip()
        if not text:
            log.info("[App] pdf overlay selection empty, ignored")
            return
        pdf = self.paper_context.pdf
        page = pdf.current_page if pdf else -1
        log.info("[App] pdf overlay selection: page=%s text=%r", page, text[:120])
        try:
            self.explain_box.show_browser_selection(text, page)
        except Exception as exc:  # the selection must never crash the shell
            log.warning("[App] pdf overlay selection handling failed: %s", type(exc).__name__)

    def _on_pdf_overlay_cancelled(self) -> None:
        self._unregister_pdf_overlay_esc()
        log.info("[App] pdf overlay selection cancelled")

    def _on_pdf_overlay_snapshot_failed(self, message: str) -> None:
        # OCR failure / window-not-found: keep the ExplainBox closed.
        self._unregister_pdf_overlay_esc()
        log.warning("[App] pdf overlay snapshot failed: %s", message)

    # -- PDF Visual Region Explain (Phase 4-B) ---------------------------

    def _on_pdf_visual_requested(self) -> None:
        """Toolbar '区域解释': the SAME fresh snapshot pipeline as 'PDF 划词',
        just in VISUAL_REGION mode. Retrigger while armed cancels; a visual
        request in flight blocks a second one (no queueing)."""
        overlay = self.pdf_selection_overlay
        if overlay.state in (
            OverlayState.ARMED,
            OverlayState.SELECTING,
            OverlayState.COMPLETED,
        ):
            overlay.cancel()
            return
        if overlay.state is not OverlayState.IDLE:
            return
        if self._visual_region_busy:
            log.info("[App] pdf visual region busy, ignored")
            return
        if self.paper_context.pdf is None:
            log.info("[App] pdf visual: no PDF context, ignored")
            return
        self._register_pdf_overlay_esc()
        overlay.set_mode(OverlayMode.VISUAL_REGION)
        overlay.request_arm(
            capture_fn=self._capture_pdf_overlay_frame,
            ocr_fn=self._run_pdf_overlay_ocr,
            keep_above=[
                window
                for window in (self.pet, self.toolbar)
                if window is not None
            ],
            before_capture=self._hide_pdf_overlay_ui,
            after_capture=self._restore_pdf_overlay_ui,
        )

    def _on_visual_region_selected(self, frame, image_rect, lines) -> None:
        """A VISUAL_REGION gesture completed: in-memory crop (2× upscale) →
        cheap kind classification → bounded context + OCR grounding →
        off-GUI VLM request → ExplainBox. Failures never open an EMPTY
        ExplainBox (only the provider-error path shows a brief error state)."""
        self._unregister_pdf_overlay_esc()
        if self._visual_region_busy:
            return
        pdf = self.paper_context.pdf
        page = pdf.current_page if pdf else -1
        try:
            crop = crop_region(frame, image_rect, DEFAULT_UPSCALE)
        except Exception as exc:  # noqa: BLE001 - crop failure is silent
            log.warning("[App] visual region crop failed: %s", type(exc).__name__)
            return
        grounding = gather_region_ocr(lines, image_rect)
        # Classification uses the UNTRUNCATED region text (the Figure/Fig.
        # caption sits at the end); the prompt keeps the bounded version.
        kind = classify_region(
            gather_region_ocr(lines, image_rect, char_limit=4000),
            crop.width, crop.height,
        )
        self._visual_kind_label = {
            "formula": "公式",
            "figure": "图表",
            "general": "区域",
        }.get(kind, "区域")
        context = self._visual_region_context(page)
        prompt = build_visual_prompt(kind, context=context, grounding=grounding)
        log.info(
            "[App] visual region: kind=%s crop=%sx%s page=%s grounded=%s",
            kind, crop.width, crop.height, page, bool(grounding),
        )
        self._visual_generation += 1
        generation = self._visual_generation
        self._visual_region_busy = True
        self.explain_box.show_visual_explanation_loading(
            kind_label=self._visual_kind_label,
            page=page,
        )
        worker = self._new_visual_worker(crop, prompt, generation)
        self._start_visual_worker(worker)

    def _visual_region_context(self, page: int) -> str:
        pdf = self.paper_context.pdf
        pieces: list[str] = []
        title = getattr(pdf, "title", "") or ""
        section = getattr(pdf, "section", "") or ""
        if title:
            pieces.append(f"标题：{title[:120]}")
        if page and page >= 1:
            pieces.append(f"页码：{page}")
        if section:
            pieces.append(f"章节：{section[:120]}")
        if not pieces:
            return ""
        return "页面/章节上下文（仅作参考）：" + "；".join(pieces)

    def _new_visual_worker(
        self, crop, prompt: str, generation: int
    ) -> PdfVisualRegionWorker:
        return PdfVisualRegionWorker(crop, prompt, generation)

    def _start_visual_worker(self, worker: PdfVisualRegionWorker) -> None:
        from PySide6.QtCore import QThread

        thread = QThread(self)
        worker.moveToThread(thread)
        # Keep the worker/thread wrappers alive: the worker has no C++
        # parent, so dropping the last Python reference would destroy it
        # before the queued started→run metacall executes.
        self._visual_worker = worker
        self._visual_worker_thread = thread
        worker.finished.connect(
            self.pdf_visual_relay.forward_finished, Qt.QueuedConnection
        )
        worker.error.connect(
            self.pdf_visual_relay.forward_error, Qt.QueuedConnection
        )
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.started.connect(worker.run)
        thread.start()

    def _on_visual_region_finished(self, generation: int, text: str) -> None:
        if generation != self._visual_generation:
            return
        self._visual_region_busy = False
        self._visual_worker = None
        self._visual_worker_thread = None
        pdf = self.paper_context.pdf
        page = pdf.current_page if pdf else -1
        self.explain_box.show_visual_explanation(
            text, kind_label=self._visual_kind_label, page=page
        )

    def _on_visual_region_error(self, generation: int, message: str) -> None:
        if generation != self._visual_generation:
            return
        self._visual_region_busy = False
        self._visual_worker = None
        self._visual_worker_thread = None
        # Brief error state only; the overlay is already IDLE.
        self.explain_box.show_visual_error(message)

    def _on_pdf_opened(self, payload: dict) -> None:
        """A PDF was opened in the browser (extension tabs.url/title).

        The engine parses local file:// PDFs (lazy index, cached) and fills
        the ambient PDF context; remote/unreadable URLs degrade to
        url/title-only (page 未知). In-RAM only.
        """
        try:
            url = str(payload.get("url") or "")
            title = str(payload.get("title") or "")
            ctx = self.pdf_ambient_engine.context_for(url, title=title)
            self.paper_context.set_pdf(ctx)
            log.info(
                "[App] ambient pdf_opened: file=%r pages=%s page=%s section=%r",
                ctx.file_name[:60], ctx.total_pages, ctx.current_page,
                ctx.section[:60],
            )
            # Ambient PDF Vision v1: one viewport look shortly after the PDF
            # opens (T1, event-driven; further looks are low-frequency).
            if not self.pdf_viewport_watcher.is_active():
                self.pdf_viewport_watcher.start()
            QTimer.singleShot(1000, self.pdf_viewport_watcher.capture_now)
            self._refresh_ambient_status()
            self.pagelens.set_current_context(
                ctx.section or ctx.title or ctx.file_name,
                self._format_pagelens_page(ctx.current_page, ctx.total_pages),
            )
        except Exception as exc:  # pdf event must never crash the shell
            log.warning("[App] pdf_opened handling failed: %s", type(exc).__name__)

    def _on_pdf_view_state(self, payload: dict) -> None:
        """Reserved PDF view-state interface (page:null until a real source).

        When a future source supplies a page, advance current_page; otherwise
        the event is only logged.
        """
        page = payload.get("page")
        try:
            if isinstance(page, int) and page >= 1:
                self.paper_context.update_pdf_page(page)
                log.info("[App] ambient pdf page -> %s", page)
                self._refresh_ambient_status()
                pdf = self.paper_context.pdf
                if pdf is not None:
                    self.pagelens.set_current_context(
                        pdf.section or pdf.title or pdf.file_name,
                        self._format_pagelens_page(pdf.current_page, pdf.total_pages),
                    )
            else:
                log.info("[App] pdf_view_state (no page yet): url=%r", payload.get("url", "")[:120])
        except Exception as exc:  # reserved event must never crash the shell
            log.warning("[App] pdf_view_state handling failed: %s", type(exc).__name__)

    def request_pdf_viewport_refresh(self) -> None:
        """User-initiated viewport look (T4): capture + OCR immediately."""
        self.pdf_viewport_watcher.capture_now()

    def _on_pdf_viewport_context(self, context) -> None:
        """A viewport observation arrived (Ambient PDF Vision v1).

        Merges it into the Context Layer, refreshes the Ambient strip and
        feeds the PageLens 本页概念 chips for the PDF scenario (the viewer
        is sealed to content scripts, so extension concepts never fire).
        In-RAM only — no Memory, no database, no disk.
        """
        try:
            merged = self.paper_context.update_pdf_viewport(
                page=context.page,
                total_pages=context.total_pages,
                section=context.section,
                keywords=context.keywords,
                visible_text=context.visible_text,
            )
            concepts = viewport_concepts(context) if merged else []
            self._pagelens_concepts_source = "viewport"
            log.info(
                "[App] ambient pdf viewport: page=%s/%s section=%r keywords=%s concepts=%s",
                context.page, context.total_pages, context.section[:60],
                context.keywords[:5], concepts[:5],
            )
            self._refresh_ambient_status()
            pdf = self.paper_context.pdf
            if pdf is not None:
                self.pagelens.set_current_context(
                    pdf.section or pdf.title or pdf.file_name,
                    self._format_pagelens_page(pdf.current_page, pdf.total_pages),
                )
            if merged and concepts:
                # PDF scenario local concepts; HTML pages keep extension concepts.
                self.pagelens.set_top_concepts(concepts)
        except Exception as exc:  # vision result must never crash the shell
            log.warning("[App] pdf viewport handling failed: %s", type(exc).__name__)

    def _on_browser_page_context(self, payload: dict) -> None:
        """A page_context arrived from the browser (url/title/heading/text).

        Updates the in-RAM Context Layer and the ambient status strip only.
        """
        try:
            self.paper_context.update_page(payload)
            log.info(
                "[App] ambient page_context: title=%r section=%r url=%s",
                self.paper_context.current.title[:60],
                self.paper_context.current.section[:60],
                self.paper_context.current.url[:120],
            )
            self._refresh_ambient_status()
            current = self.paper_context.current
            self.pagelens.set_current_context(
                current.section or current.title,
                current.title if current.section and current.title != current.section else "",
            )
        except Exception as exc:  # context must never crash the shell
            log.warning("[App] page_context handling failed: %s", type(exc).__name__)

    def _refresh_ambient_status(self) -> None:
        status = self.ambient_status
        if status is None:
            return
        if self.paper_context.pdf is not None:
            status.show_pdf(self.paper_context.pdf)
        else:
            status.show_context(self.paper_context.snapshot())
        if status.isVisible():
            self._position_ambient_status()

    @staticmethod
    def _format_pagelens_page(page: int, total_pages: int) -> str:
        if page < 1:
            return ""
        return f"第 {page} / {total_pages} 页" if total_pages > 0 else f"第 {page} 页"

    def _position_ambient_status(self) -> None:
        status = self.ambient_status
        if status is None:
            return
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        status.adjustSize()
        x = available.right() - status.width() - theme.scaled(theme.SCREEN_MARGIN)
        y = (
            available.bottom()
            - theme.scaled(theme.SCREEN_MARGIN)
            - theme.scaled(60)
            - status.height()
        )
        status.move(x, y)

    def _on_reader_explain(self, text: str, page: int) -> None:
        """PaperLens 2.2 reader selection → existing ExplainBox explain entry.

        The reader never calls a model; it forwards (text, page) and the
        ExplainBox consent gate + async worker + PdfQa.explain_selection
        handle the explanation.
        """
        log.info("[App] reader explain_requested page=%s text=%r", page, text[:120])
        try:
            if page >= 1:
                self.paper_context.update_pdf_page(page)  # 最近解释页 → current_page
            self.explain_box.show_browser_selection(text, page)
        except Exception as exc:  # explain entry must never crash the shell
            log.warning("[App] reader explain handling failed: %s", type(exc).__name__)

    # -- Short Talk -----------------------------------------------------

    def _on_short_ask_requested(self) -> None:
        # "Ask…" means "I want to talk to Firefly": open the UI V2 console
        # (singleton, input focused) as the default chat entry. It must work
        # regardless of any visible approval card or pending popover, and it
        # never captures the screen or calls a provider by itself. The legacy
        # CompanionChatWindow stays intact and is the fallback if the console
        # ever fails to open.
        from core.screen_vision.foreground_tracker import foreground_tracker

        foreground_tracker.remember_current_external_window()
        try:
            from ui.v2.console import open_singleton as open_console

            open_console(runner=self.character_conversation)
        except Exception as exc:  # console failure must not break Ask…
            self._log_console_open_failure(exc)
            from ui.companion_chat_window import CompanionChatWindow

            CompanionChatWindow.open_singleton(runner=self.character_conversation)

    def _log_console_open_failure(self, exc: Exception) -> None:
        """Best-effort diagnostics; never raises."""
        try:
            log.warning("CompanionConsole open failed, falling back: %s", exc)
        except Exception:
            pass

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
        # Ambient PDF Vision: an explicit "look at this page" phrase triggers
        # a viewport capture (T4) in addition to the normal ask flow.
        if self.paper_context.pdf is not None and any(
            phrase in prompt for phrase in ("看看这一页", "这一页讲什么", "看看当前页")
        ):
            self.request_pdf_viewport_refresh()
        if self.short_ask.agent == "firefly":
            self._do_character_ask(prompt)
            return
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
        if self.short_ask.agent == "firefly":
            self._do_character_ask(prompt)
            return
        self._resume_fallbacks_this_cycle = 0
        self._do_short_ask(self.short_ask.agent, prompt)

    def _on_short_ask_retry(self) -> None:
        """Retry the last prompt after a Short Talk hard timeout."""
        prompt = self._last_short_ask_prompt
        if not prompt or self.short_ask.running:
            return
        if self.short_ask.agent == "firefly":
            self._do_character_ask(prompt)
            return
        self._do_short_ask(self.short_ask.agent, prompt)

    def _do_character_ask(self, prompt: str) -> None:
        self._last_short_ask_prompt = prompt
        self.short_ask.set_running("Thinking…")
        if not self.character_conversation.ask(prompt):
            self.short_ask.reset_with_note("Firefly is already responding.")

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
        if self.short_ask.agent == "firefly":
            self.character_conversation.stop()
            self.short_ask.begin_cancel()
            return
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
        if agent == "firefly":
            self.short_ask.show_input(
                "firefly",
                resume=self.character_conversation.has_history,
            )
            return
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

    def _handle_control(self, socket) -> None:
        """Control channel keeps its semantics: "quit" really exits the pet."""
        data = bytes(socket.readAll()).decode("utf-8", "ignore")
        if "quit" in data:
            socket.disconnectFromServer()
            self.exit_application()

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
    # 科研级字体系统：注册 assets/fonts/ 应用级字体（STIX/Noto/DejaVu…），
    # 早于 setFont 与任何窗口创建；缺失时静默跳过。
    theme.load_application_fonts()
    app.setFont(QFont(theme.FONT_FAMILY, theme.FONT_SIZE_BODY))

    if not _acquire_single_instance():
        return 0

    # Migration must finish before any runtime-backed service is constructed.
    # It copies legacy data only, never deletes or overwrites it.  Running it
    # after the process mutex also prevents two simultaneous starts racing.
    initialize_user_data(legacy_project_dir=PROJECT_DIR, paths=USER_PATHS)

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
