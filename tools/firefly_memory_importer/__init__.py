"""Firefly history migration importer (Stage 0: Doubao parser).

This package is standalone by design: it imports neither ``core``, ``memory``,
nor ``character``, and performs no memory writes, bond updates, character
changes, or LLM analysis.
"""

from .models import Conversation, ParsedHistory, Role, Turn
from .parser import parse_file, parse_text, write_parsed_history

__all__ = [
    "Conversation",
    "ParsedHistory",
    "Role",
    "Turn",
    "parse_file",
    "parse_text",
    "write_parsed_history",
]
