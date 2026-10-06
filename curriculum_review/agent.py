"""One advisory model call per review case; no curriculum mutation methods."""

from __future__ import annotations

from typing import Any

from .prompt import build_messages, parse_recommendation
from .schema import ReviewCase, ReviewRecommendation


class CurriculumReviewAgent:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def recommend(self, case: ReviewCase) -> tuple[ReviewRecommendation, str]:
        response = self._provider.chat(build_messages(case), temperature=0.1)
        content = response["choices"][0]["message"]["content"]
        return parse_recommendation(content), content
