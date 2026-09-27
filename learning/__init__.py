"""Firefly Learning Bridge v0.1.

Bridges Firefly AI Pet to a Z Code session running the ``firefly-learning``
skill, which orchestrates teach-mcp as the ONLY source of learning state.

Ownership contract (docs/learning/MEMORY_OWNERSHIP.md, frozen):
- Firefly owns: learner_id, course/resource ids, managed resources, manifests.
- Learning workspace owns: raw resources + opaque binding ids from teach-mcp.
- teach-mcp owns: mastery, misconceptions, quiz results, sessions, progress.
- Z Code is an agent host only; deleting its conversations must not lose
  learning state.
"""

from __future__ import annotations

__all__ = [
    "bridge_state",
    "course_binding",
    "identity",
    "launcher",
    "learning_context",
    "resource_manager",
    "skill_installer",
]
