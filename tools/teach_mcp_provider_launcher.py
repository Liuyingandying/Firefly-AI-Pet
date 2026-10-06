"""Start the installed teach-mcp with Firefly's existing TJU credential.

This is a process-environment adapter. It does not alter teach-mcp or its
model request implementation. Keep stdout reserved for MCP JSON-RPC.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from core.user_paths import get_user_data_paths


def resolve_teach_mcp_server(source_env=None, *, user_paths=None) -> Path:
    """Resolve the optional external server without a machine-specific path.

    An explicit CLI argument still takes precedence in ``main``. The optional
    capability lives outside this repository and is never installed by lookup.
    """
    source = os.environ if source_env is None else source_env
    configured = (source.get("FIREFLY_TEACH_MCP_SERVER") or "").strip()
    if configured:
        return Path(configured).expanduser()
    paths = user_paths or get_user_data_paths()
    return paths.plugins / "teach-mcp" / "server.py"


DEFAULT_SERVER = resolve_teach_mcp_server()
SCHEMA_BOOTSTRAP = Path(__file__).with_name("teach_mcp_schema_bootstrap.py")
KEY_SLOTS = tuple(f"TJULLM_API_KEY_{index}" for index in (1, 2, 3))

from core.credential_store import default_store  # noqa: E402
from providers.base import read_env_file  # noqa: E402


class AuthoringCredentialMissing(RuntimeError):
    """The configured TJU credential is unavailable to the MCP process."""


def build_authoring_env(
    source_env: dict[str, str] | None = None,
    *,
    store=None,
    dotenv: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve three slots using Firefly's env > store > dotenv precedence.

    A single configured TJU account can populate empty slots. Existing
    numbered credentials, when configured, retain their own values.
    """
    env = dict(os.environ if source_env is None else source_env)
    dotenv = read_env_file() if dotenv is None else dotenv
    store = default_store() if store is None else store

    def configured(name: str) -> str:
        return (
            (env.get(name) or "").strip()
            or (store.get(name) or "").strip()
            or (dotenv.get(name) or "").strip()
        )

    shared = configured("TJULLM_API_KEY")
    for slot in KEY_SLOTS:
        value = configured(slot) or shared
        if not value:
            raise AuthoringCredentialMissing(
                "TJU AUTHORING 凭据缺失：请在 Firefly Provider Manager 配置 TJULLM_API_KEY。"
            )
        env[slot] = value
    return env


def main() -> int:
    server = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SERVER
    if not server.is_file():
        print("teach-mcp 启动失败：server.py 不存在。", file=sys.stderr)
        return 2
    try:
        env = build_authoring_env()
    except AuthoringCredentialMissing as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return subprocess.call([sys.executable, str(SCHEMA_BOOTSTRAP), str(server)], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
