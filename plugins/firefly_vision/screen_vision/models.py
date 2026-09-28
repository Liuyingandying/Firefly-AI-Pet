"""Shared data models for the Firefly Screen Vision PoC."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ScreenFrame:
    """An in-memory screenshot. Never persisted to disk.

    Mapping metadata (PDF OCR Overlay Phase 2-A), populated for screen
    captures: ``crop_offset`` is the absolute physical-screen coordinate of
    the image's top-left pixel, and ``scale_x``/``scale_y`` convert screen
    pixels into image pixels — ``image = (screen - crop_offset) * scale``.
    Non-screen frames (camera, attachments) keep the encoding resize factor,
    which is meaningless for screen mapping.
    """

    width: int
    height: int
    mime_type: str
    image_bytes: bytes
    captured_at: datetime
    crop_offset: tuple = (0, 0)  # (x, y) absolute physical screen px
    scale_x: float = 1.0
    scale_y: float = 1.0


@dataclass
class ScreenObservation:
    """Structured result of the vision layer describing what is on screen."""

    scene_summary: str = ""
    active_application: str = ""
    window_title: str = ""
    visible_text: list = field(default_factory=list)
    ui_elements: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    uncertain: list = field(default_factory=list)
    raw_model_text: str = ""


@dataclass
class ScreenVisionResult:
    """End-to-end result returned by ScreenVisionService.look."""

    observation: ScreenObservation
    answer: str
    timings: dict
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "observation": {
                key: value
                for key, value in self.observation.__dict__.items()
            },
            "answer": self.answer,
            "timings": self.timings,
            "meta": self.meta,
        }
