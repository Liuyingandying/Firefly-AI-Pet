"""Firefly memory suggestion (Phase 5-B)."""

from .candidate_extractor import ExtractionResult, MemoryCandidateExtractor
from .memory_candidate_detector import MemoryCandidateDetector, MemorySuggestion
from .suggestion_service import SuggestionService

__all__ = [
    "ExtractionResult",
    "MemoryCandidateDetector",
    "MemoryCandidateExtractor",
    "MemorySuggestion",
    "SuggestionService",
]
