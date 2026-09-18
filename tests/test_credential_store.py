"""Credential Store + resolve_setting precedence tests (Provider Manager Phase 1).

Covers: create / read / delete, env > store > .env > default precedence,
legacy behavior when no source is registered, repo-directory containment,
corruption quarantine, and the no-value-export guarantee of list_sources.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import user_paths as user_paths_module
from core.credential_store import CredentialStore, default_store_path
from providers import base as provider_base

# Names deliberately unlikely to exist in any developer environment; the
# store key pattern requires UPPER_SNAKE.
KEY_A = "PHASE1_TEST_KEY_A"
KEY_B = "PHASE1_TEST_KEY_B"
ENV_KEY = "PHASE1_TEST_ENV_ONLY"


@pytest.fixture()
def store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(path=tmp_path / "credentials" / "credentials.json")


@pytest.fixture()
def registered(store: CredentialStore):
    provider_base.register_credential_source(store.get)
    yield store
    provider_base.register_credential_source(None)


@pytest.fixture(autouse=True)
def _no_env_interference(monkeypatch: pytest.MonkeyPatch):
    """Keep the precedence tests hermetic against developer environments."""
    for name in (KEY_A, KEY_B, ENV_KEY, "TJULLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# store basics: create / read / delete
# ---------------------------------------------------------------------------


def test_save_creates_store_file_and_value(store: CredentialStore, tmp_path: Path):
    store.save(KEY_A, "value-a")
    raw = json.loads((tmp_path / "credentials" / "credentials.json").read_text("utf-8"))
    assert raw["credentials"][KEY_A]["value"] == "value-a"
    assert store.get(KEY_A) == "value-a"


def test_get_missing_returns_none(store: CredentialStore):
    assert store.get(KEY_A) is None
    assert store.list_sources() == []


def test_delete_removes_credential(store: CredentialStore):
    store.save(KEY_A, "value-a")
    store.save(KEY_B, "value-b")
    store.delete(KEY_A)
    assert store.get(KEY_A) is None
    assert store.get(KEY_B) == "value-b"
    assert store.list_sources() == [KEY_B]


def test_delete_absent_key_is_noop(store: CredentialStore):
    store.delete(KEY_A)
    assert store.list_sources() == []


def test_save_overwrites_existing_value(store: CredentialStore):
    store.save(KEY_A, "old")
    store.save(KEY_A, "new")
    assert store.get(KEY_A) == "new"


def test_invalid_inputs_rejected(store: CredentialStore):
    with pytest.raises(ValueError):
        store.save(KEY_A, "   ")
    with pytest.raises(ValueError):
        store.save(KEY_A, "line1\nline2")
    with pytest.raises(ValueError):
        store.save("lowercase-key", "value")
    assert store.list_sources() == []


def test_corrupt_store_quarantined_not_crashing(store: CredentialStore):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not valid json", encoding="utf-8")
    assert store.get(KEY_A) is None  # treated as empty, no crash
    store.save(KEY_A, "value-a")  # store recovers on next write
    assert store.get(KEY_A) == "value-a"
    assert any(
        p.name.startswith("credentials.corrupt-")
        for p in store.path.parent.iterdir()
    )


def test_atomic_write_leaves_no_tmp_files(store: CredentialStore):
    store.save(KEY_A, "value-a")
    store.save(KEY_A, "value-a2")
    assert list(store.path.parent.glob("*.tmp")) == []


def test_list_sources_returns_names_only(store: CredentialStore):
    store.save(KEY_A, "secret-value")
    assert store.list_sources() == [KEY_A]
    assert "secret-value" not in json.dumps(store.list_sources())


# ---------------------------------------------------------------------------
# repo containment: the store must never live inside the repository
# ---------------------------------------------------------------------------


def test_default_store_path_follows_user_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    custom = UserDataPathsProxy(tmp_path)
    monkeypatch.setattr(user_paths_module, "DEFAULT_USER_PATHS", custom)
    assert default_store_path() == tmp_path / "credentials" / "credentials.json"


def test_store_never_writes_into_repo_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo_root = Path(__file__).resolve().parents[1]
    custom = UserDataPathsProxy(tmp_path / "userdata")
    monkeypatch.setattr(user_paths_module, "DEFAULT_USER_PATHS", custom)

    store = CredentialStore()  # default path, not injected
    store.save(KEY_A, "value-a")

    assert store.path == tmp_path / "userdata" / "credentials" / "credentials.json"
    assert (tmp_path / "userdata" / "credentials" / "credentials.json").is_file()
    # nothing inside the repository tree
    assert not (repo_root / "credentials.json").exists()
    assert not (repo_root / "credentials").exists()
    assert not (repo_root / "config" / "credentials.json").exists()


class UserDataPathsProxy:
    """Minimal stand-in exposing only .credentials (duck-typed path source)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def credentials(self) -> Path:
        return self._root / "credentials"


# ---------------------------------------------------------------------------
# resolve_setting precedence: env > store > .env > default
# ---------------------------------------------------------------------------


def test_env_priority_beats_store(
    registered: CredentialStore, monkeypatch: pytest.MonkeyPatch
):
    registered.save(KEY_A, "store-value")
    monkeypatch.setenv(KEY_A, "env-value")
    assert provider_base.resolve_setting(KEY_A) == "env-value"


def test_store_beats_env_file(
    registered: CredentialStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    registered.save(KEY_A, "store-value")
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY_A}=dotenv-value\n", encoding="utf-8")
    monkeypatch.delenv(KEY_A, raising=False)
    assert provider_base.resolve_setting(KEY_A, env_file=env_file) == "store-value"


def test_env_file_fallback_preserved_when_store_empty(
    registered: CredentialStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # source registered but the key is not stored → .env must still resolve
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY_A}=dotenv-value\n", encoding="utf-8")
    monkeypatch.delenv(KEY_A, raising=False)
    assert provider_base.resolve_setting(KEY_A, env_file=env_file) == "dotenv-value"


def test_default_returned_when_all_layers_miss(
    registered: CredentialStore, tmp_path: Path
):
    env_file = tmp_path / "missing.env"
    assert (
        provider_base.resolve_setting(KEY_A, "fallback-default", env_file=env_file)
        == "fallback-default"
    )


def test_unregistered_source_keeps_legacy_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # no source registered at all: a "stored" value must be invisible, and a
    # store crash cannot leak into resolution — legacy .env/default semantics
    provider_base.register_credential_source(None)
    monkeypatch.delenv(KEY_A, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"{KEY_A}=dotenv-value\n", encoding="utf-8")
    assert provider_base.resolve_setting(KEY_A, env_file=env_file) == "dotenv-value"
    assert (
        provider_base.resolve_setting(KEY_B, "legacy-default", env_file=env_file)
        == "legacy-default"
    )


def test_store_lookup_failure_degrades_to_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def broken_source(name: str) -> str | None:
        raise RuntimeError("damaged store backend")

    provider_base.register_credential_source(broken_source)
    try:
        env_file = tmp_path / ".env"
        env_file.write_text(f"{KEY_A}=dotenv-value\n", encoding="utf-8")
        monkeypatch.delenv(KEY_A, raising=False)
        assert provider_base.resolve_setting(KEY_A, env_file=env_file) == "dotenv-value"
    finally:
        provider_base.register_credential_source(None)
