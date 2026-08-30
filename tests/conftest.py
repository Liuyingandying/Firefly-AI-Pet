"""Project-level pytest configuration with isolated repo-local temp roots."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlparse
import urllib.request

import pytest
import requests
from PySide6.QtCore import QEvent, QEventLoop
from PySide6.QtWidgets import QApplication

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _is_loopback_url(value) -> bool:
    url = getattr(value, "full_url", value)
    try:
        host = (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def _block_unstubbed_external_http(monkeypatch: pytest.MonkeyPatch):
    """Fail closed before an unstubbed pytest request can leave the machine.

    Provider tests must inject/monkeypatch their transport. Loopback remains
    available for tests that deliberately own a local test server.
    """
    original_request = requests.sessions.Session.request
    original_urlopen = urllib.request.urlopen

    def guarded_request(session, method, url, *args, **kwargs):
        if _is_loopback_url(url):
            return original_request(session, method, url, *args, **kwargs)
        raise RuntimeError(
            "pytest blocked an unstubbed external HTTP request; inject a fake transport"
        ) from None

    def guarded_urlopen(url, *args, **kwargs):
        if _is_loopback_url(url):
            return original_urlopen(url, *args, **kwargs)
        raise RuntimeError(
            "pytest blocked an unstubbed external HTTP request; inject a fake transport"
        ) from None

    monkeypatch.setattr(requests.sessions.Session, "request", guarded_request)
    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)


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
