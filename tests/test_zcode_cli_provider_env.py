"""Z Code CLI provider-env regression (provider audit, 2026-09-27).

Locks in the fix for "无法定位 CLI ZCode Built-in Provider Config":
- clean environment  -> launcher injects the CLI's two OFFICIAL env vars,
  derived from the install layout (never copied, never hardcoded)
- ZCode-aware parent env (both vars already set) -> passed through untouched
- official wrapper (if the install ever ships one) takes priority in argv
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from learning.launcher import (  # noqa: E402
    find_official_wrapper,
    resolve_zcode_cli,
)

BUILTIN = "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"
PERSONAL = "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"


@pytest.fixture()
def fake_install(tmp_path, monkeypatch):
    """A ZCode-like install layout inside tmp: resources/glm/zcode.cjs."""
    cli = tmp_path / "install" / "resources" / "glm" / "zcode.cjs"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fake cli\n", encoding="utf-8")
    monkeypatch.setenv("FIREFLY_ZCODE_CLI", str(cli))
    monkeypatch.setenv("FIREFLY_NODE", "node-fake")
    return cli


def _clean_provider_env(monkeypatch):
    for name in (
        BUILTIN,
        PERSONAL,
        "ZCODE_DATA_BASE_DIR",
        "ZCODE_APP_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)


def test_injects_official_provider_env_from_install_layout(
    tmp_path, monkeypatch, fake_install
):
    from learning.launcher import _ensure_zcode_provider_env

    _clean_provider_env(monkeypatch)
    builtin = (
        fake_install.parent.parent / "config" / "provider" / "zcode-builtin.json"
    )
    builtin.parent.mkdir(parents=True)
    builtin.write_text("{}", encoding="utf-8")
    data_base = tmp_path / "databasedir"
    personal = data_base / ".zcode" / "v2" / "provider_config.json"
    personal.parent.mkdir(parents=True)
    personal.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(data_base))

    env = dict(os.environ)
    _ensure_zcode_provider_env(env, fake_install)

    assert env[BUILTIN] == str(builtin)
    assert env[PERSONAL] == str(personal)
    # only official names injected; nothing else mutated
    assert env.get("ZCODE_DATA_BASE_DIR") == str(data_base)


def test_inherits_when_parent_env_already_provides(monkeypatch, fake_install):
    from learning.launcher import _ensure_zcode_provider_env

    marker_builtin = r"D:\desktop\builtin.json"
    marker_personal = r"D:\desktop\personal.json"
    monkeypatch.setenv(BUILTIN, marker_builtin)
    monkeypatch.setenv(PERSONAL, marker_personal)

    env = dict(os.environ)
    _ensure_zcode_provider_env(env, fake_install)

    assert env[BUILTIN] == marker_builtin  # untouched pass-through
    assert env[PERSONAL] == marker_personal


def test_no_injection_when_layout_incomplete_leaves_cli_error_intact(
    tmp_path, monkeypatch, fake_install
):
    from learning.launcher import _ensure_zcode_provider_env

    _clean_provider_env(monkeypatch)
    data_base = tmp_path / "databasedir"
    monkeypatch.setenv("ZCODE_DATA_BASE_DIR", str(data_base))
    # no builtin file, no personal file anywhere

    env = dict(os.environ)
    _ensure_zcode_provider_env(env, fake_install)

    assert BUILTIN not in env
    assert PERSONAL not in env


def test_real_install_layout_yields_existing_files():
    """On THIS machine the derived paths must actually exist (audit proof)."""
    cli = resolve_zcode_cli()
    builtin = cli.parent.parent / "config" / "provider" / "zcode-builtin.json"
    assert builtin.is_file(), f"官方 builtin provider config 应存在: {builtin}"


def test_official_wrapper_from_path_takes_priority(monkeypatch, tmp_path, fake_install):
    """Priority A: a real PATH-resolved wrapper wins when present."""
    from learning import launcher

    fake = tmp_path / "tools" / "zcode-cli.exe"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"fake wrapper")
    monkeypatch.setattr(
        launcher, "shutil_which", lambda name: str(fake) if name == "zcode-cli" else None
    )
    assert find_official_wrapper(fake_install) == fake


def test_desktop_shell_is_never_mistaken_for_a_wrapper(monkeypatch, fake_install):
    """Regression (task-injection fix): the Electron desktop shell
    ``ZCode.exe`` must NEVER be picked as a CLI wrapper — spawning it just
    opens the GUI's latest conversation (the original bug). Detection is
    PATH-only now, so even a file named zcode.exe at the install root is
    ignored."""
    from learning import launcher

    desktop_shell = fake_install.parent.parent.parent / "zcode.exe"
    desktop_shell.write_bytes(b"MZ fake shell")
    try:
        monkeypatch.setattr(launcher, "shutil_which", lambda name: None)
        assert find_official_wrapper(fake_install) is None
    finally:
        desktop_shell.unlink()
