"""Memory access modes (M3B.1).

Every Memory mutation must flow through an entry that declares its access
mode.  The mode is a lightweight, deterministic capability gate — no LLM,
no heuristics:

- ``READ_ONLY``       — search / retrieve / export / consistency report only
- ``SAFE_WRITE``      — + explicit remember / normal creation (incl. the
                        lifecycle bookkeeping that creation flow performs:
                        vector_id linking and M2A supersede)
- ``CONFIRMED_WRITE`` — + edit / delete / clear / migration / index repair

Violations raise :class:`MemoryAccessViolation` — never a silent no-op.
"""

from __future__ import annotations

from enum import Enum

# Patch fields that belong to the normal creation/supersede flow.  A
# SAFE_WRITE service may set exactly these via ``repository.update`` —
# anything else (content edits, timestamps, weights) is CONFIRMED_WRITE.
SAFE_WRITE_PATCH_FIELDS = frozenset({
    "vector_id",
    "lifecycle_status",
    "superseded_by",
    "supersede_reason",
})


class MemoryAccessMode(str, Enum):
    """Capability tier of a Memory service/repository handle."""

    READ_ONLY = "read_only"
    SAFE_WRITE = "safe_write"
    CONFIRMED_WRITE = "confirmed_write"

    def allows(self, required: "MemoryAccessMode") -> bool:
        """True when this mode grants at least ``required``.

        Explicit rank lookup — the ``str`` mixin defines its own ordering
        dunders (lexicographic), so operator overloads must not be relied on.
        """
        return _RANK[self] >= _RANK[required]


_RANK: dict[MemoryAccessMode, int] = {
    MemoryAccessMode.READ_ONLY: 0,
    MemoryAccessMode.SAFE_WRITE: 1,
    MemoryAccessMode.CONFIRMED_WRITE: 2,
}


class MemoryAccessViolation(PermissionError):
    """Raised when an operation exceeds the handle's access mode.

    Deliberately loud: a READ_ONLY handle must never silently skip a
    mutation — the caller has to choose the right mode explicitly.
    """

    def __init__(self, action: str, mode: MemoryAccessMode,
                 required: MemoryAccessMode) -> None:
        self.action = action
        self.mode = mode
        self.required = required
        super().__init__(
            f"MemoryAccessViolation: '{action}' requires "
            f"{required.value}, handle is {mode.value}"
        )


def check_access(mode: MemoryAccessMode, required: MemoryAccessMode,
                 action: str, *, patch_keys: frozenset[str] | None = None,
                 allowed_patch_fields: frozenset[str] | None = None) -> None:
    """Raise :class:`MemoryAccessViolation` unless ``mode`` grants ``required``.

    When ``patch_keys`` is given (repository.update), a SAFE_WRITE handle may
    only touch ``allowed_patch_fields``; any other key escalates the
    requirement to CONFIRMED_WRITE.
    """
    required_eff = required
    if (
        patch_keys is not None
        and allowed_patch_fields is not None
        and mode is MemoryAccessMode.SAFE_WRITE
        and not patch_keys <= allowed_patch_fields
    ):
        required_eff = MemoryAccessMode.CONFIRMED_WRITE
    if not mode.allows(required_eff):
        raise MemoryAccessViolation(action, mode, required_eff)
