"""Shared data models for the Firefly Screen Vision PoC."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ScreenFrame:
    """An in-memory screenshot. Never persisted to disk."""

    width: int
    height: int
    mime_type: str
    image_bytes: bytes
    captured_at: datetime


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
