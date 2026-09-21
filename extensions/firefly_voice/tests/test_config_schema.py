"""config_schema 单元自测（插件内；仓库级验收在 tests/voice_plugin/）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # extensions/

from firefly_voice import config_schema as cs  # noqa: E402


def test_defaults_shape():
    assert cs.DEFAULTS["enabled"] is True          # 语音能力默认存在
    assert cs.DEFAULTS["auto_play"] is False       # v1.3
    assert cs.DEFAULTS["url"].startswith("http://127.0.0.1:8300")


def test_config_path_under_user_plugins():
    path = str(cs.config_path())
    assert "plugins" in path.replace("\\", "/")
    assert "firefly_voice" in path
    assert path.endswith("config.yaml")


def test_env_names_frozen():
    assert cs.ENV_ENABLED == "FIREFLY_VOICE_ENABLED"
    assert cs.ENV_AUTO_PLAY == "FIREFLY_VOICE_AUTO_PLAY"
    assert cs.ENV_URL == "FIREFLY_VOICE_URL"
