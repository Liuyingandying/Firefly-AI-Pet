"""Real local Mem0 smoke test (skipped when mem0 is not installed)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

# Must be set before mem0 is imported, otherwise mem0 initializes telemetry.
os.environ["MEM0_TELEMETRY"] = "false"

import pytest

pytest.importorskip("mem0")

from memory.mem0_adapter import Mem0Adapter  # noqa: E402


MODEL_CACHE = (
    Path(__file__).resolve().parent.parent / "runtime" / "memory" / "models"
)


def test_real_mem0_add_restart_search() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mem0-smoke-"))
    model_cache = MODEL_CACHE if MODEL_CACHE.exists() else None
    try:
        adapter = Mem0Adapter(tmp, user_id="smoke-user", model_cache_dir=model_cache)
        vector_id = adapter.add("用户正在开发 Firefly AI Pet，希望它成为长期 AI Companion。")
        assert vector_id
        adapter.close()

        # Reinstate a fresh adapter against the same storage (simulate restart).
        adapter2 = Mem0Adapter(tmp, user_id="smoke-user", model_cache_dir=model_cache)
        hits = adapter2.search("用户最近在开发什么项目？", limit=5)
        adapter2.close()

        assert hits
        assert any("Firefly" in (hit.text or "") for hit in hits)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
