"""Project-level pytest configuration.

Ensures tmp_path / tmpdir always land under a project-local temp directory
to avoid Windows PermissionError [WinError 5] on shared system temp roots.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# Project-local temp root — avoids D:\DevCache\Temp permission issues on Windows
_PROJECT_TMP = Path(__file__).resolve().parent.parent / ".pytest_tmp"


@pytest.fixture(scope="session", autouse=True)
def _pytest_local_temproot():
    """Redirect Python / pytest temp root to project-local directory."""
    _PROJECT_TMP.mkdir(exist_ok=True)
    old = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_PROJECT_TMP)
    os.environ["TEMP"] = str(_PROJECT_TMP)
    os.environ["TMP"] = str(_PROJECT_TMP)
    yield
    # Restore
    if old is not None:
        os.environ["PYTEST_DEBUG_TEMPROOT"] = old
    else:
        os.environ.pop("PYTEST_DEBUG_TEMPROOT", None)
    os.environ["TEMP"] = old or os.environ.get("TEMP", "")
    os.environ["TMP"] = old or os.environ.get("TMP", "")


@pytest.fixture(scope="session")
def project_tmp_root() -> Path:
    """Return the project-local temp directory path."""
    return _PROJECT_TMP
