"""Quick Tools adapter for Firefly's existing one-shot Camera Vision.

This plugin owns no camera, worker, frame, provider, prompt, or persistence.
It exposes product metadata and device availability only; Companion retains
the production CameraCapture -> ScreenVisionService route.
"""

from __future__ import annotations

from PySide6.QtMultimedia import QMediaDevices

from core.plugin_api import PluginContext, QuickToolPlugin
from core.quick_tools import QuickToolManifest


PLUGIN_ID = "firefly-camera-vision"
PLUGIN_VERSION = "0.1.0"


class FireflyCameraVision(QuickToolPlugin):
    """Resource-free capability adapter; start/stop never open the camera."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.context: PluginContext | None = None
        self.capability_exposed = False

    @property
    def manifest(self) -> QuickToolManifest:
        return QuickToolManifest(
            id=PLUGIN_ID,
            name="Camera Vision",
            description="On-demand, single-frame camera vision for Companion requests",
            icon="eye",
            version=PLUGIN_VERSION,
            capabilities=("camera", "vision"),
            min_api="2",
            author="Firefly",
        )

    @property
    def capability(self) -> str:
        return "Explicit request · single frame · in memory"

    @property
    def capability_note(self) -> str:
        return "The camera is never opened by plugin start or in the background."

    def initialize(self, context: PluginContext) -> None:
        self.context = context

    @staticmethod
    def _availability() -> str:
        try:
            return "READY" if QMediaDevices.videoInputs() else "UNAVAILABLE"
        except Exception:
            return "UNAVAILABLE"

    def status(self) -> str:
        return self._availability()

    def start(self) -> None:
        self.capability_exposed = True
        self._set_status(self._availability())

    def stop(self) -> None:
        self.capability_exposed = False
        # Availability remains a device fact, independent of enabled state.
        self._set_status(self._availability())

    def open(self) -> None:
        return None

    def shutdown(self) -> None:
        self.capability_exposed = False


def create_plugin(parent=None) -> FireflyCameraVision:
    return FireflyCameraVision(parent)
