from voice_client.config import load_voice_config


def test_missing_voice_config_is_disabled(tmp_path):
    config = load_voice_config(tmp_path / "missing-voice-config.yaml")

    assert config.enabled is False
    assert config.source == "defaults(missing)"
