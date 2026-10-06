"""Authoring MCP startup credential adapter regression."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.teach_mcp_provider_launcher import (  # noqa: E402
    AuthoringCredentialMissing,
    KEY_SLOTS,
    build_authoring_env,
)


class FakeStore:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


def test_firefly_provider_credential_populates_authoring_slots():
    env = build_authoring_env(
        {}, store=FakeStore({"TJULLM_API_KEY": "test-shared"}), dotenv={}
    )
    assert [env[slot] for slot in KEY_SLOTS] == ["test-shared"] * 3


def test_numbered_credential_retains_precedence_over_shared():
    env = build_authoring_env(
        {"TJULLM_API_KEY_2": "test-env-two"},
        store=FakeStore({"TJULLM_API_KEY": "test-shared"}),
        dotenv={"TJULLM_API_KEY_3": "test-dotenv-three"},
    )
    assert [env[slot] for slot in KEY_SLOTS] == [
        "test-shared", "test-env-two", "test-dotenv-three"
    ]


def test_missing_credential_is_explicit_without_secret_text():
    with pytest.raises(AuthoringCredentialMissing) as caught:
        build_authoring_env({}, store=FakeStore({}), dotenv={})
    assert "TJULLM_API_KEY" in str(caught.value)
    assert "KeyError" not in str(caught.value)
