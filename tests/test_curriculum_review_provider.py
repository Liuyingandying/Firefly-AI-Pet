"""Provider discovery and Authoring-compatible key rotation, with fake keys."""

from __future__ import annotations

import json

from curriculum_review.agent import CurriculumReviewAgent
from curriculum_review.provider import AllTJUKeysFailed, configured_tju_provider
from curriculum_review.schema import ReviewCase
from providers.base import ProviderHTTPError


class FakeManager:
    def get_provider_status(self, provider_id):
        assert provider_id == "tju"
        return {"configured": True, "source": "credential_store"}


class FakeStore:
    def get(self, name):
        return "test-slot-one" if name == "TJULLM_API_KEY" else None


USER_SLOTS = {"TJULLM_API_KEY_1": "test-slot-one",
              "TJULLM_API_KEY_2": "test-slot-two",
              "TJULLM_API_KEY_3": "test-slot-three"}


def _provider(transport):
    return configured_tju_provider(source_env={}, user_env=USER_SLOTS,
                                   store=FakeStore(), dotenv={},
                                   manager=FakeManager(), transport=transport)


def test_provider_config_discovery_uses_missing_user_slots() -> None:
    provider, source = _provider(lambda *args: None)
    assert provider.slot_count == 3
    assert provider.endpoint == "https://ai.tju.edu.cn/api/v3/chat/completions"
    assert provider.default_model == "deepseek-v4-flash"
    assert source == "authoring_env:credential_store+HKCU_user_environment"


def test_three_key_rotation_retries_429_then_401_then_succeeds() -> None:
    attempted = []

    def transport(endpoint, payload, headers, timeout):
        slot = headers["Authorization"].split()[-1]
        attempted.append(slot)
        if slot == "test-slot-one":
            raise ProviderHTTPError(429)
        if slot == "test-slot-two":
            raise ProviderHTTPError(401)
        return {"choices": [{"message": {"content": "OK"}}]}

    provider, _ = _provider(transport)
    response = provider.chat([{"role": "user", "content": "ping"}])
    assert response["choices"][0]["message"]["content"] == "OK"
    assert provider.attempted_slots == [1, 2, 3]
    assert provider.last_successful_slot == 3
    assert attempted == list(USER_SLOTS.values())


def test_review_smoke_returns_valid_structured_recommendation() -> None:
    def transport(endpoint, payload, headers, timeout):
        return {"choices": [{"message": {"content": json.dumps({
            "decision": "KEEP_ORIGINAL", "confidence": 0.72,
            "reason": "boundary needs checking", "impact": "no draft change",
            "recommended_assignment": "review queue",
            "human_confirmation_points": ["check question source"],
        })}}]}

    provider, _ = _provider(transport)
    case = ReviewCase("control_theory", "ch05", "1-7 exercise", {},
                      {"status": "REVIEW_REQUIRED"}, "BOUNDARY", [], [])
    recommendation, raw = CurriculumReviewAgent(provider).recommend(case)
    assert recommendation.decision == "KEEP_ORIGINAL"
    assert recommendation.confidence == 0.72
    assert json.loads(raw)["impact"] == "no draft change"


def test_all_slots_failed_exposes_only_safe_status_metadata() -> None:
    def transport(endpoint, payload, headers, timeout):
        raise ProviderHTTPError(429)

    provider, _ = _provider(transport)
    try:
        provider.chat([{"role": "user", "content": "ping"}])
    except AllTJUKeysFailed as exc:
        assert exc.attempted_slots == (1, 2, 3)
        assert exc.status_code == 429
        assert "test-slot" not in str(exc)
    else:
        raise AssertionError("expected all slots to fail")
