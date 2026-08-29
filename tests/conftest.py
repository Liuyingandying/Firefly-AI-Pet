"""Project-level pytest configuration with isolated repo-local temp roots."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QEventLoop
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent


def pytest_configure(config: pytest.Config) -> None:
    """Choose a fresh basetemp before pytest creates ``tmp_path_factory``.

    A unique directory avoids re-opening stale pytest roots whose Windows ACLs
    may no longer be readable. An explicit command-line ``--basetemp`` remains
    respected for callers that already provide their own isolated directory.
    """
    if config.option.basetemp is None:
        token = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        config.option.basetemp = str(PROJECT_DIR / f".pytest_tmp_{token}")


@pytest.fixture(scope="session", autouse=True)
def _pytest_local_temproot(
    tmp_path_factory: pytest.TempPathFactory,
):
    """Point child processes at this run's basetemp and restore each value."""
    base = tmp_path_factory.getbasetemp()
    previous = {
        name: os.environ.get(name)
        for name in ("PYTEST_DEBUG_TEMPROOT", "TEMP", "TMP")
    }
    for name in previous:
        os.environ[name] = str(base)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(scope="session")
def project_tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Return the unique repo-local temp directory for this test run."""
    return tmp_path_factory.getbasetemp()


def teardown_qt_widget(widget) -> None:
    """Close a Qt widget and drain the event loop so closeEvent handlers run.

    This prevents ``QThread: Destroyed while thread is still running`` errors
    at pytest teardown by ensuring that:
      1. closeEvent() fires and runs (e.g. _stop_ocr_thread, _stop_explain_thread)
      2. All queued signals are processed
      3. QApplication processes pending events
    """
    if widget is None:
        return
    widget.close()
    # Process closeEvent and any queued signal callbacks
    QApplication.processEvents()
    # Small yield to let thread.quit() / thread.wait() wiring finish
    QApplication.processEvents()
