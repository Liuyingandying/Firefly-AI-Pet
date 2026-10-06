"""Portable optional server lookup; no launch, credentials or real data."""
from pathlib import Path
import subprocess
import sys
from core.user_paths import UserDataPaths
from tools.teach_mcp_provider_launcher import resolve_teach_mcp_server


def test_explicit_server_environment_has_priority(tmp_path):
    server = tmp_path / "external" / "server.py"
    assert resolve_teach_mcp_server(
        {"FIREFLY_TEACH_MCP_SERVER": str(server)},
        user_paths=UserDataPaths(tmp_path / "user"),
    ) == server


def test_missing_server_uses_portable_optional_plugin_root(tmp_path):
    user = UserDataPaths(tmp_path / "user")
    server = resolve_teach_mcp_server({}, user_paths=user)
    assert server == user.plugins / "teach-mcp" / "server.py"
    assert not server.exists()
    assert not user.root.exists()


def test_direct_launcher_from_another_directory_reports_missing_server(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "tools" / "teach_mcp_provider_launcher.py"
    result = subprocess.run(
        [sys.executable, "-B", str(launcher), str(tmp_path / "missing-server.py")],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 2
    assert "server.py" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
