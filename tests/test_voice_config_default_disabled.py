import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "firefly_voice_chat"))

from voice_client.config import load_voice_config


def test_missing_voice_config_is_disabled(tmp_path):
    config = load_voice_config(tmp_path / "missing-voice-config.yaml")

    assert config.enabled is False
    assert config.source == "defaults(missing)"
