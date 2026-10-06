"""UI-only capability view, obtained AFTER the user confirms an operation."""
from copy import copy

from memory.access_mode import MemoryAccessMode, check_access


def confirmed_memory_handle(service):
    # A read-only capability must never be elevated by a UI action.
    check_access(service.access_mode, MemoryAccessMode.SAFE_WRITE, "user confirmation")
    handle = copy(service)
    handle.access_mode = MemoryAccessMode.CONFIRMED_WRITE
    return handle
