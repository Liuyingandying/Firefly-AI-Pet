"""Fail-closed question schema adapter for teach-mcp package construction.

The authoring draft stores a selected option as answer text. The package
validator additionally requires the option letter. Recover that letter only
from a unique, exact option match or an explicit model-provided letter.
"""

from __future__ import annotations

import re
from typing import Any, Callable

LETTERS = "ABCD"


class QuestionSchemaError(ValueError):
    """A question's selected option cannot be identified unambiguously."""


def normalize_question(
    question: dict[str, Any], index: int, *, normalize: Callable[[str], str]
) -> dict[str, Any]:
    options = question.get("options")
    if not isinstance(options, list) or not 3 <= len(options) <= len(LETTERS):
        raise QuestionSchemaError(f"questions[{index}]: options_count_invalid")
    answer = str(question.get("answer", "") or "").strip()
    explicit = str(question.get("correct_option", "") or "").strip().upper()
    if explicit and explicit not in LETTERS[: len(options)]:
        raise QuestionSchemaError(f"questions[{index}]: correct_option_invalid")

    prefixed = re.match(r"^([A-Da-d])\s*[.、)）：:]\s*\S", answer)
    if prefixed:
        if explicit and explicit != prefixed.group(1).upper():
            raise QuestionSchemaError(f"questions[{index}]: correct_option_conflict")
        # This is already an accepted validator representation.
        return dict(question)
    if re.fullmatch(r"[A-Da-d]", answer):
        selected = LETTERS.index(answer.upper())
        if selected >= len(options):
            raise QuestionSchemaError(f"questions[{index}]: answer_out_of_range")
        answer = str(options[selected]).strip()
    else:
        normalized_answer = normalize(answer)
        matches = [
            option_index
            for option_index, option in enumerate(options)
            if normalized_answer and normalize(str(option)) == normalized_answer
        ]
        if len(matches) != 1 and explicit and not matches:
            # Preserve older, validator-accepted explicit packages. The
            # adapter only derives absent fields from exact option matches.
            return dict(question)
        if len(matches) != 1:
            raise QuestionSchemaError(
                f"questions[{index}]: answer_option_match_{'missing' if not matches else 'ambiguous'}"
            )
        selected = matches[0]

    canonical = LETTERS[selected]
    if explicit and explicit != canonical:
        raise QuestionSchemaError(f"questions[{index}]: correct_option_conflict")
    return {**question, "answer": answer, "correct_option": canonical}


def normalize_spec(spec: dict[str, Any], *, normalize: Callable[[str], str]) -> dict[str, Any]:
    return {
        **spec,
        "questions": [
            normalize_question(item, index, normalize=normalize)
            for index, item in enumerate(spec.get("questions", []))
        ],
    }


def install_package_adapter() -> None:
    """Attach at the MCP process entry without editing teach-mcp source files."""
    from knowledge_authoring import builders

    if getattr(builders.build_package, "_firefly_schema_adapter", False):
        return
    original = builders.build_package

    def adapted_build_package(domain: str, slug: str, spec: dict) -> dict:
        try:
            adapted = normalize_spec(spec, normalize=builders._norm)
        except QuestionSchemaError as exc:
            return {"status": "error", "domain": domain, "slug": slug,
                    "errors": [str(exc)], "validation": {"valid": False}}
        return original(domain, slug, adapted)

    adapted_build_package._firefly_schema_adapter = True  # type: ignore[attr-defined]
    builders.build_package = adapted_build_package
