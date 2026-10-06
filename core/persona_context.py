"""Read-only per-turn persona views; no Memory, persistence, model or tool IO.

Public vision/structured-teaching projections never export Bond details or
previous messages. Full chat retains its existing permission-filtered Memory.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from typing import Any, Sequence
from core.bond_context_builder import BondContextBuilder


@dataclass(frozen=True)
class PersonaContext:
    character_id: str
    character_style: str
    relationship_context: str
    conversation_context: tuple[tuple[str, str], ...]
    current_task: str
    affectionate_attention: bool = False

    def to_prompt(self, *, public: bool = False) -> str:
        """Render the original Character and bounded owner data only.

        Public task routes receive no private Bond facts or past messages.
        Ordinary chat never calls this renderer.
        """
        data = {
            "character_id": self.character_id,
            "current_task": self.current_task[:800],
            "affectionate_attention": self.affectionate_attention,
        }
        if not public:
            data["bond_context"] = self.relationship_context
            data["conversation_context"] = self.conversation_context
        return (
            "--- BEGIN PERSONA CONTEXT READ LAYER ---\n"
            + self.character_style + "\n"
            + "只读任务上下文数据：\n"
            + json.dumps(data, ensure_ascii=False) + "\n"
            + "--- END PERSONA CONTEXT READ LAYER ---"
        )


class PersonaContextReadLayer:
    """Narrow read capabilities only; never owns or advances source state."""
    def __init__(self, character: Any, *, bond_reader=None, conversation_reader=None):
        self.character = character
        self.bond_reader = bond_reader
        self.conversation_reader = conversation_reader

    def read(self, user_text: str = "", *, history=None, current_task="chat") -> PersonaContext:
        bond = None
        if self.bond_reader is not None:
            try:
                bond = self.bond_reader.read()
            except Exception:
                pass
        if history is None and self.conversation_reader is not None:
            try:
                history = [t.to_chat_message() for t in self.conversation_reader.load_working_window()]
            except Exception:
                history = ()
        return self.snapshot(self.character, user_text, bond=bond, history=history or (),
                             current_task=current_task)

    @staticmethod
    def snapshot(character, user_text="", *, bond=None, history: Sequence=(),
                 current_task="chat") -> PersonaContext:
        style = "\n".join(m["content"] for m in character.to_system_messages())
        recent = tuple(
            (m["role"], m["content"][:320]) for m in list(history)[-4:]
            if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
            and isinstance(m.get("content"), str)
        )
        return PersonaContext(
            character_id=getattr(character, "character_id", "unknown"),
            character_style=style,
            relationship_context=BondContextBuilder().build(bond).to_prompt(),
            conversation_context=recent,
            current_task=str(current_task),
            affectionate_attention=(
                any(word in user_text for word in ("亲爱的", "想你", "陪陪我"))
                and not any(word in user_text for word in ("别叫", "不要叫", "不许叫", "别喊"))
            ),
        )
