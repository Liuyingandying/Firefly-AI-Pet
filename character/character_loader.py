"""Load and validate Firefly's versioned character prompt files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CHARACTER_DIR = Path(__file__).resolve().parent / "firefly"


@dataclass(frozen=True)
class CharacterProfile:
    """Immutable prompt layers loaded from the Firefly character domain."""

    character_id: str
    identity_prompt: str
    personality_prompt: str
    dialogue_policy_prompt: str
    relationship_policy_prompt: str

    def to_system_messages(self) -> list[dict[str, str]]:
        """Return identity and personality/policy/relationship as two messages."""
        identity = (
            "--- BEGIN CHARACTER IDENTITY ---\n"
            f"{self.identity_prompt}\n"
            "--- END CHARACTER IDENTITY ---"
        )
        personality_policy = (
            "--- BEGIN CHARACTER PERSONALITY AND POLICY ---\n"
            "[Personality]\n"
            f"{self.personality_prompt}\n\n"
            "[Dialogue Policy]\n"
            f"{self.dialogue_policy_prompt}\n\n"
            "[Relationship Policy]\n"
            f"{self.relationship_policy_prompt}\n"
            "--- END CHARACTER PERSONALITY AND POLICY ---"
        )
        return [
            {"role": "system", "content": identity},
            {"role": "system", "content": personality_policy},
        ]


class CharacterLoader:
    """Load the local Firefly character without involving memory or providers."""

    def __init__(self, character_dir: str | Path | None = None) -> None:
        self.character_dir = Path(character_dir or DEFAULT_CHARACTER_DIR).resolve()

    def load(self) -> CharacterProfile:
        """Load all required Firefly layers or fail before provider execution."""
        identity = self._load_prompt_file("identity.yaml")
        personality = self._load_prompt_file("personality.yaml")
        dialogue_policy = self._load_prompt_file("dialogue_policy.yaml")
        relationship_policy = self._load_prompt_file("relationship_policy.yaml")
        return CharacterProfile(
            character_id="firefly",
            identity_prompt=identity,
            personality_prompt=personality,
            dialogue_policy_prompt=dialogue_policy,
            relationship_policy_prompt=relationship_policy,
        )

    def _load_prompt_file(self, filename: str) -> str:
        path = self.character_dir / filename
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"character file is unavailable: {filename}") from exc
        except yaml.YAMLError as exc:
            raise ValueError(f"character file is invalid YAML: {filename}") from exc
        if not isinstance(document, dict):
            raise ValueError(f"character file must contain a mapping: {filename}")
        version = document.get("version")
        if version != 1:
            raise ValueError(f"character file has unsupported version: {filename}")
        prompt = document.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"character file has no prompt: {filename}")
        return prompt.strip()
