"""Safety invariants shared by all providers.

Nothing in this PoC may ever leak credentials or image data into error
messages or logs; the reasoning layer must never receive image data.
"""

import json

# Markers that indicate image data is trying to reach the reasoning layer.
FORBIDDEN_IMAGE_MARKERS = ("base64", "image_url", "data:image", "image_bytes")


def assert_text_only_payload(payload: dict) -> None:
    """Raise if a payload destined for the reasoning layer contains image data."""
    # default=repr keeps bytes/objects from crashing the check; they serialize
    # to their repr, which still matches the forbidden markers when relevant.
    serialized = json.dumps(payload, ensure_ascii=False, default=repr).lower()
    for marker in FORBIDDEN_IMAGE_MARKERS:
        if marker in serialized:
            raise ValueError(
                f"Refusing to send payload containing {marker!r} to the reasoning layer."
            )


def sanitize_error_text(text: str, *secrets: str) -> str:
    """Strip credentials and auth headers from text destined for logs/errors."""
    text = str(text or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<REDACTED>")
    # Remove Authorization header values wholesale (case-insensitive). The
    # replacement label deliberately avoids the word itself so the scan
    # terminates; the counter is a belt-and-braces bound.
    import re

    for _ in range(16):
        idx = text.lower().find("authorization")
        if idx == -1:
            break
        end = text.find("\n", idx)
        text = text[:idx] + "[auth-credentials removed]" + (text[end:] if end != -1 else "")
    # Remove bare auth schemes and common credential fields. Replacement text
    # intentionally contains none of the sensitive field/scheme names.
    text = re.sub(r"Bearer\s+\S+", "[auth-credentials removed]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|token|cookie)\s*[:=]\s*[^,;\s}\]]+",
        "[auth-credentials removed]",
        text,
    )
    return text
