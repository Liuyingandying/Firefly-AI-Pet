"""ReasoningProvider abstraction.

Implementations receive the user question plus a structured observation and
return answer text. They must never receive screenshot bytes or base64 data;
this is enforced by ``assert_text_only_payload``.
"""

from abc import ABC, abstractmethod

from core.screen_vision.safety import assert_text_only_payload


class ReasoningProvider(ABC):
    """Turns (question, observation) into a final text answer."""

    @abstractmethod
    def answer(self, question: str, observation: dict) -> str:
        """Answer using only the question and the text-only observation."""
        raise NotImplementedError


__all__ = ["ReasoningProvider", "assert_text_only_payload"]
