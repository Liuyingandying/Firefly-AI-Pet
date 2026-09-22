"""Load and validate Firefly's versioned character prompt files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CHARACTER_DIR = Path(__file__).resolve().parent / "firefly"
DEFAULT_CHARACTER_ID = "firefly"
DEFAULT_DISPLAY_NAME = "流萤"
DEFAULT_BRAND_NAME = "流萤 AI Pet"
DEFAULT_ASSISTANT_NAME = "流萤"


@dataclass(frozen=True)
class CharacterDisplayNames:
    """User-visible names for one configured character."""

    display_name: str = DEFAULT_DISPLAY_NAME
    brand_name: str = DEFAULT_BRAND_NAME
    assistant_name: str = DEFAULT_ASSISTANT_NAME

    @classmethod
    def from_character(cls, character: Any) -> "CharacterDisplayNames":
        if character is None:
            return cls()
        return cls(
            display_name=getattr(character, "display_name", DEFAULT_DISPLAY_NAME),
            brand_name=getattr(character, "brand_name", DEFAULT_BRAND_NAME),
            assistant_name=getattr(character, "assistant_name", DEFAULT_ASSISTANT_NAME),
        )


@dataclass(frozen=True)
class CharacterProfile:
    """Immutable prompt layers loaded from the Firefly character domain."""

    character_id: str
    identity_prompt: str
    personality_prompt: str
    dialogue_policy_prompt: str
    relationship_policy_prompt: str
    display_name: str = DEFAULT_DISPLAY_NAME
    brand_name: str = DEFAULT_BRAND_NAME
    assistant_name: str = DEFAULT_ASSISTANT_NAME

    @property
    def display_names(self) -> CharacterDisplayNames:
        return CharacterDisplayNames.from_character(self)

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

    def __init__(
        self,
        character_dir: str | Path | None = None,
        *,
        character_id: str | None = None,
    ) -> None:
        self.character_dir = Path(character_dir or DEFAULT_CHARACTER_DIR).resolve()
        self.character_id = character_id or (
            self.character_dir.name if character_dir is not None else DEFAULT_CHARACTER_ID
        )

    def load(self) -> CharacterProfile:
        """Load all required Firefly layers or fail before provider execution."""
        identity_document = self._load_document("identity.yaml")
        identity = self._prompt_from_document(identity_document, "identity.yaml")
        personality = self._load_prompt_file("personality.yaml")
        dialogue_policy = self._load_prompt_file("dialogue_policy.yaml")
        relationship_policy = self._load_prompt_file("relationship_policy.yaml")
        return CharacterProfile(
            character_id=self.character_id,
            identity_prompt=identity,
            personality_prompt=personality,
            dialogue_policy_prompt=dialogue_policy,
            relationship_policy_prompt=relationship_policy,
            display_name=self._display_value(
                identity_document, "display_name", DEFAULT_DISPLAY_NAME
            ),
            brand_name=self._display_value(
                identity_document, "brand_name", DEFAULT_BRAND_NAME
            ),
            assistant_name=self._display_value(
                identity_document, "assistant_name", DEFAULT_ASSISTANT_NAME
            ),
        )

    def _load_prompt_file(self, filename: str) -> str:
        return self._prompt_from_document(self._load_document(filename), filename)

    def _load_document(self, filename: str) -> dict[str, Any]:
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
        return document

    @staticmethod
    def _prompt_from_document(document: dict[str, Any], filename: str) -> str:
        prompt = document.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"character file has no prompt: {filename}")
        return prompt.strip()

    @staticmethod
    def _display_value(
        document: dict[str, Any], key: str, default: str
    ) -> str:
        value = document.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"character identity has invalid {key}")
        return value.strip()


# ---------------------------------------------------------------------------
# Character registry & selection persistence (v1.1)
# ---------------------------------------------------------------------------

CHARACTER_BASE_DIR = Path(__file__).resolve().parent      # character/
CHARACTER_FALLBACK_ID = "firefly"


def resolve_animations_dir(character_id: str, repo_root: str | Path | None = None) -> Path | None:
    """Resolve the animation directory for a character.

    Priority: ``assets/skins/<id>/animations`` (skin packs), then
    ``character/<id>/animations``; ``None`` = fall back to the legacy
    ``assets/animations`` (the caller's choice for the default character).
    """
    root = Path(repo_root or Path(__file__).resolve().parents[1])
    for candidate in (
        root / "assets" / "skins" / character_id / "animations",
        root / "character" / character_id / "animations",
    ):
        if candidate.is_dir():
            return candidate
    return None


@dataclass(frozen=True)
class CharacterMeta:
    """One discoverable character: id, display name, optional animation dir."""

    character_id: str
    display_name: str
    animations_dir: Path | None = None


def list_characters(base_dir: str | Path | None = None) -> list[CharacterMeta]:
    """Scan character/<id>/ subdirectories for identity.yaml.

    A directory without a valid identity.yaml is skipped, so adding
    ``character/custom_xxx/`` needs no code change.
    """
    base = Path(base_dir or CHARACTER_BASE_DIR)
    metas: list[CharacterMeta] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        identity_file = child / "identity.yaml"
        if not identity_file.is_file():
            continue
        display_name = child.name
        try:
            document = yaml.safe_load(identity_file.read_text(encoding="utf-8")) or {}
            raw = document.get("display_name") if isinstance(document, dict) else None
            if isinstance(raw, str) and raw.strip():
                display_name = raw.strip()
        except Exception:  # noqa: BLE001 - 坏文件按目录名回退, 不阻断扫描
            pass
        own_animations = child / "animations"
        animations_dir = (
            own_animations if own_animations.is_dir()
            else resolve_animations_dir(child.name, base.parent)
        )
        metas.append(
            CharacterMeta(
                character_id=child.name,
                display_name=display_name,
                animations_dir=animations_dir,
            )
        )
    return metas


def current_character_file() -> Path:
    """Selection persistence location (user data root; config/ fallback)."""
    try:
        from core.user_paths import get_user_data_paths

        return get_user_data_paths().root / "current_character.yaml"
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[1] / "config" / "current_character.yaml"


def load_current_character(default: str = CHARACTER_FALLBACK_ID) -> str:
    """Return the persisted character_id; anything invalid -> default."""
    try:
        document = yaml.safe_load(
            current_character_file().read_text(encoding="utf-8")
        ) or {}
        value = document.get("character_id") if isinstance(document, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:  # noqa: BLE001 - 读取失败一律回默认
        pass
    return default


def save_current_character(character_id: str) -> bool:
    """Persist the selection atomically; never raises."""
    path = current_character_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(
                {"character_id": character_id}, allow_unicode=True, sort_keys=False
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return True
    except Exception:  # noqa: BLE001 - 写失败不影响运行
        return False
