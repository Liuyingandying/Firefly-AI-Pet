"""TeachingContext (Phase 3 Teaching Strategy MVP).

A READ-ONLY teaching posture derived deterministically from learning facts.
It answers "how should the companion teach right now?" — never "how well does
the learner know this?" (that is the Rule Engine's mastery, which this layer
only reads).

Frozen rules:

- the decision inputs are exactly ``(review_due, mastery)`` — no LLM, no
  heuristic scoring, no inference of mastery;
- ``review_due`` takes priority over the mastery mapping;
- only an ACTIVE curriculum's concept (the current focus) is described;
- nothing here writes: no mastery, no concept, no assessment, no session.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LearningStage(str, Enum):
    """Where the learner is on ONE concept (deterministic, mastery-driven)."""

    BEGINNER = "beginner"
    INTRODUCED = "introduced"
    PRACTICE = "practice"
    TRANSFER = "transfer"
    REVIEW = "review"


class TeachingSource(str, Enum):
    """Why the stage was chosen (provenance, not a second decision)."""

    REVIEW_DUE = "review_due"
    MASTERY = "mastery"
    MASTERY_WITH_EVIDENCE = "mastery_with_evidence"
    NO_CONCEPT = "no_concept"


#: Deterministic mastery -> stage mapping (frozen by the phase spec).
STAGE_BY_MASTERY: dict[int, LearningStage] = {
    0: LearningStage.BEGINNER,
    1: LearningStage.INTRODUCED,
    2: LearningStage.PRACTICE,
    3: LearningStage.PRACTICE,
    4: LearningStage.TRANSFER,
    5: LearningStage.TRANSFER,
}

#: One strategy line per stage (single source; the prompt and the UI share it).
STRATEGY_BY_STAGE: dict[LearningStage, str] = {
    LearningStage.BEGINNER: "先解释概念，再给一个例子，然后检查是否理解",
    LearningStage.INTRODUCED: "先让学习者回忆，再问一个简单问题",
    LearningStage.PRACTICE: "出一道练习题，让学习者实际应用",
    LearningStage.TRANSFER: "给一个应用场景，检验能否举一反三",
    LearningStage.REVIEW: "先做一次复习回顾，再检查是否还记得",
}


@dataclass(frozen=True, slots=True)
class TeachingContext:
    """Read-only teaching posture for the current concept."""

    concept_id: str | None = None
    concept_name: str | None = None
    mastery: int | None = None
    learning_stage: str = LearningStage.BEGINNER.value
    recommended_strategy: str = ""
    source: str = TeachingSource.NO_CONCEPT.value
    # Phase 7B: read-only resource references for the current concept
    # (LearningResource objects, injected by the caller). Display data only —
    # their presence never changes the stage or the strategy.
    resources: tuple = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "learning_stage",
            _stage_value(self.learning_stage),
        )
        if self.mastery is not None:
            mastery = int(self.mastery)
            if not (0 <= mastery <= 5):
                raise ValueError(f"mastery must be 0..5 (got {mastery})")
            object.__setattr__(self, "mastery", mastery)
        for field in ("concept_id", "concept_name"):
            value = getattr(self, field)
            object.__setattr__(self, field, str(value).strip() if value else None)
        object.__setattr__(self, "resources", tuple(self.resources))

    # -- projections -------------------------------------------------------

    @property
    def stage(self) -> LearningStage:
        return LearningStage(self.learning_stage)

    @property
    def is_review(self) -> bool:
        return self.stage is LearningStage.REVIEW

    @property
    def has_concept(self) -> bool:
        return self.concept_id is not None

    def resource_lines(self) -> tuple[str, ...]:
        """参考材料 lines for the prompt/UI (empty when there are none)."""
        lines: list[str] = []
        for resource in self.resources:
            label = getattr(resource, "label", None)
            text = label() if callable(label) else str(resource)
            if text:
                lines.append(f"参考材料：{text}")
        return tuple(lines)

    def lines(self) -> tuple[str, ...]:
        """UI/prompt-safe lines; no mastery is claimed when it is unknown."""
        lines: list[str] = []
        if self.concept_name:
            lines.append(f"当前概念：{self.concept_name}")
        if self.mastery is not None:
            lines.append(f"掌握度：{self.mastery}/5（由规则引擎维护，只读）")
        lines.append(f"教学阶段：{self.learning_stage.upper()}")
        if self.recommended_strategy:
            lines.append(f"教学策略：{self.recommended_strategy}")
        lines.extend(self.resource_lines())
        return tuple(lines)

    def context_block(self) -> str | None:
        """Teaching block for the chat prompt; None when there is no concept."""
        if not self.has_concept:
            return None
        return "\n".join(
            ["当前教学（由确定性策略给出，不要自行判定掌握程度）：", *[f"- {l}" for l in self.lines()]]
        )


def _stage_value(value) -> str:
    if isinstance(value, LearningStage):
        return value.value
    text = str(value or "").strip().lower()
    for member in LearningStage:
        if text in (member.value, member.name.lower()):
            return member.value
    raise ValueError(f"invalid learning stage: {value!r}")


__all__ = [
    "LearningStage",
    "TeachingSource",
    "STAGE_BY_MASTERY",
    "STRATEGY_BY_STAGE",
    "TeachingContext",
]
