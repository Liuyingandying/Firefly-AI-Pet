"""Learning resource layer (Phase 7B).

Course-scoped references to trusted learning material, bound to Concepts and
Chapters. The layer only SAVES, READS and SHOWS sources — it never parses,
downloads, recommends, or touches mastery.

    from core.learning.resources import LearningResource, ResourceStore
"""

from .models import (
    LearningResource,
    ResourceOrigin,
    ResourceType,
)
from .store import ResourceStore, ResourceStoreError
from .textbook import record_textbook_resources
from .viewer import ResourceViewerAction, ViewerRequest

__all__ = [
    "LearningResource",
    "ResourceOrigin",
    "ResourceType",
    "ResourceStore",
    "ResourceStoreError",
    "record_textbook_resources",
]
