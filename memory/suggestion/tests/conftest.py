"""Suggestion unit tests never load real embeddings or send HTTP requests."""
from urllib.parse import urlparse

import pytest
import requests


@pytest.fixture(autouse=True)
def isolated_semantic_backend(monkeypatch):
    from memory.suggestion import semantic_dedup

    def unavailable(_text):
        raise semantic_dedup._EmbeddingUnavailable("isolated unit test backend")

    # Semantic tests explicitly inject _compute_embedding. Other tests exercise
    # the production exact-dedup degradation without loading a real model.
    monkeypatch.setattr(semantic_dedup, "_fastembed_embedding", unavailable)


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    original = requests.sessions.Session.request

    def guarded(session, method, url, *args, **kwargs):
        if (urlparse(str(url)).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}:
            return original(session, method, url, *args, **kwargs)
        raise RuntimeError("unit test requires an injected transport")

    monkeypatch.setattr(requests.sessions.Session, "request", guarded)
