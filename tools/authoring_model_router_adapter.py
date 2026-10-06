"""Firefly-owned model binding for existing Fullbook authoring call sites.

No external teach-mcp source is edited. Installed only in Firefly's authoring
processes, not in the external MCP server or Z Code process.
"""
from __future__ import annotations

from functools import lru_cache
import time


@lru_cache(maxsize=1)
def _provider():
    from curriculum_review.provider import configured_tju_provider
    return configured_tju_provider()[0]


def authoring_chat(messages, tools, max_tokens=800):
    payload = {"messages": messages, "max_tokens": max_tokens, "tools": tools, "tool_choice": "auto"}
    started = time.monotonic()
    response = _provider().complete(payload, task="authoring")
    usage = response.get("usage", {})
    return {"calls": 1, "elapsed": round(time.monotonic()-started, 1),
            "usage": {"input_tokens": usage.get("prompt_tokens", "unavailable"),
                      "output_tokens": usage.get("completion_tokens", "unavailable")}, **response}


def install_authoring_model_router(book_pipeline=None):
    if book_pipeline is None:
        import book_pipeline
    if not hasattr(book_pipeline, "_firefly_original_model_chat"):
        book_pipeline._firefly_original_model_chat = book_pipeline._tjullm_chat
    book_pipeline._tjullm_chat = authoring_chat


def uninstall_authoring_model_router(book_pipeline):
    original = getattr(book_pipeline, "_firefly_original_model_chat", None)
    if original is not None:
        book_pipeline._tjullm_chat = original
        del book_pipeline._firefly_original_model_chat
    _provider.cache_clear()
