"""Local long-term memory API for Firefly AI Pet."""

from .memory_manager import add_memory, get_memory_context, search_memory
from .mem0_adapter import Hit, Mem0Adapter, Mem0AdapterError
from .memory_prompt_builder import (
    MEMORY_CATEGORIES,
    MemoryContext,
    MemoryEntry,
    MemoryPromptBuilder,
    PromptLayers,
)
from .records import (
    CATEGORY_DEFAULTS,
    MemoryCategory,
    MemoryRecord,
    MemorySource,
    WritePolicy,
    validate_memory_record,
)
from .repository import (
    DEFAULT_REPOSITORY_PATH,
    STORE_VERSION,
    DuplicateMemoryRecordError,
    JsonMemoryRepository,
    MemoryRepository,
)
from .security_guard import (
    GuardDecision,
    MemorySecurityGuard,
    MemorySecurityViolation,
    NoopMemorySecurityGuard,
)
from .service import (
    MemoryService,
    MemorySynchronizationError,
    ReconcileResult,
    WriteOutcome,
    WriteResult,
)
from .write_guards import (
    ExplicitMemoryRequest,
    MemoryWriteDeniedError,
    RedLineCategory,
    RedLineViolation,
    RedLineViolationError,
    authorize_explicit_write,
    check_red_line,
    detect_red_line,
    extract_explicit_memory_request,
    infer_category,
)

__all__ = [
    "CATEGORY_DEFAULTS",
    "DEFAULT_REPOSITORY_PATH",
    "DuplicateMemoryRecordError",
    "ExplicitMemoryRequest",
    "Hit",
    "MEMORY_CATEGORIES",
    "MemoryCategory",
    "MemoryContext",
    "MemoryEntry",
    "MemoryPromptBuilder",
    "MemoryRecord",
    "MemoryRepository",
    "MemoryService",
    "MemorySecurityGuard",
    "MemorySecurityViolation",
    "GuardDecision",
    "NoopMemorySecurityGuard",
    "MemorySource",
    "MemorySynchronizationError",
    "ReconcileResult",
    "WriteOutcome",
    "WriteResult",
    "MemoryWriteDeniedError",
    "Mem0Adapter",
    "Mem0AdapterError",
    "PromptLayers",
    "JsonMemoryRepository",
    "RedLineCategory",
    "RedLineViolation",
    "RedLineViolationError",
    "STORE_VERSION",
    "WritePolicy",
    "add_memory",
    "authorize_explicit_write",
    "check_red_line",
    "detect_red_line",
    "extract_explicit_memory_request",
    "search_memory",
    "get_memory_context",
    "infer_category",
    "validate_memory_record",
]
