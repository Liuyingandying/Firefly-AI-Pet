from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.bili_insight_client import BiliInsightClient, BiliServiceError
from core.path_config import load_path_config
from core.plugin_loader import PluginLoader
from core.quick_tools import QuickToolsRegistry
from core.user_paths import UserDataPaths


def test_environment_has_priority_over_user_config(tmp_path: Path) -> None:
    config = tmp_path / "path_config.yaml"
    config.write_text(
        "paths:\n  plugin_root: configured/plugins\n"
        "  bili_insight_root: configured/bili\n",
        encoding="utf-8",
    )
    resolved = load_path_config(
        config,
        environ={
            "FIREFLY_PLUGIN_ROOT": str(tmp_path / "env-plugins"),
            "FIREFLY_BILI_INSIGHT_ROOT": str(tmp_path / "env-bili"),
        },
        user_paths=UserDataPaths(tmp_path / "user"),
    )
    assert resolved.plugin_root == tmp_path / "env-plugins"
    assert resolved.bili_insight_root == tmp_path / "env-bili"


def test_user_config_has_priority_over_portable_default(tmp_path: Path) -> None:
    config = tmp_path / "path_config.yaml"
    config.write_text(
        "paths:\n  plugin_root: external/plugins\n"
        "  bili_insight_root: external/bili\n",
        encoding="utf-8",
    )
    resolved = load_path_config(
        config, environ={}, user_paths=UserDataPaths(tmp_path / "user")
    )
    project = Path(__file__).resolve().parent.parent
    assert resolved.plugin_root == project / "external" / "plugins"
    assert resolved.bili_insight_root == project / "external" / "bili"


def test_missing_or_bad_config_uses_user_data_defaults(tmp_path: Path) -> None:
    paths = UserDataPaths(tmp_path / "user")
    missing = load_path_config(tmp_path / "missing.yaml", environ={}, user_paths=paths)
    assert missing.plugin_root == paths.plugins
    assert missing.bili_insight_root == paths.plugins / "bili-insight"

    bad = tmp_path / "bad.yaml"
    bad.write_text("paths: [not, a, mapping]", encoding="utf-8")
    fallback = load_path_config(bad, environ={}, user_paths=paths)
    assert fallback.plugin_root == paths.plugins
    assert fallback.bili_insight_root == paths.plugins / "bili-insight"


def test_missing_plugin_root_keeps_core_available(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "no-external-plugins"
    monkeypatch.setattr("core.plugin_loader.DEFAULT_PLUGIN_ROOT", missing)
    monkeypatch.delenv("FIREFLY_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("FIREFLY_PLUGIN_PATH", raising=False)

    registry = QuickToolsRegistry()
    loader = PluginLoader(registry)
    assert loader.discover_roots() == [missing]
    assert loader.load() == []
    assert registry.all() == ()
    loader.shutdown()


def test_missing_bili_service_gracefully_reports_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    missing = tmp_path / "no-bili-service"
    monkeypatch.setattr("core.bili_insight_client.DEFAULT_SERVICE_DIR", missing)
    monkeypatch.delenv("FIREFLY_BILI_INSIGHT_ROOT", raising=False)
    monkeypatch.delenv("FIREFLY_BILI_SERVICE_DIR", raising=False)

    with pytest.raises(BiliServiceError) as excinfo:
        BiliInsightClient().health()
    assert excinfo.value.kind == "env_dependency"
    assert "service.py not found" in str(excinfo.value)


def test_legacy_bili_environment_variable_remains_supported(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("FIREFLY_BILI_INSIGHT_ROOT", raising=False)
    monkeypatch.setenv("FIREFLY_BILI_SERVICE_DIR", str(tmp_path))
    client = BiliInsightClient()
    with pytest.raises(BiliServiceError) as excinfo:
        client.health()
    assert excinfo.value.kind == "env_dependency"
    assert str(tmp_path) in str(excinfo.value)


def test_target_production_files_contain_no_personal_absolute_paths() -> None:
    project = Path(__file__).resolve().parent.parent
    targets = (
        project / "core" / "plugin_loader.py",
        project / "core" / "plugin_api.py",
        project / "core" / "bili_insight_client.py",
    )
    forbidden = ("E:" + os.sep, "C:" + os.sep + "Users" + os.sep)
    for target in targets:
        text = target.read_text(encoding="utf-8")
        assert all(value not in text for value in forbidden), target
