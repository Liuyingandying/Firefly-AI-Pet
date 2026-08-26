"""Firefly history migration narrative layer (Phase 4-C)."""

from .narrative_extractor import NarrativeExtractor
from .narrative_profile import build_profile, load, write

__all__ = ["NarrativeExtractor", "build_profile", "load", "write"]
