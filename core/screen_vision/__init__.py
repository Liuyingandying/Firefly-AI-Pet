# -*- coding: utf-8 -*-
"""Host-side vision reserved interface (degradation facade when the
vision plugin is not installed).

The vision engine implementation has moved to the plugin
``plugins/firefly_vision/screen_vision``. This package only forwards and
guarantees the implementation module is a SINGLE instance process-wide:

- ``__path__`` contains only this directory: host data contracts
  (models.py) come from the host copy;
- other submodules (config/service/vision/brain/trigger/...) forward via
  ``__getattr__`` to the plugin implementation package ``screen_vision.*``
  -- the plugin loader has registered the implementation root in
  sys.path, so implementation classes are process-wide singletons with
  no cross-path duplicate-class isinstance splits.

When the vision plugin is not installed, ANY attribute/submodule access
raises CapabilityMissingError (host entries show the top banner
"plugin not installed"), never silently degrading.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SELF = Path(__file__).resolve().parent
_IMPL = _SELF.parent.parent / "plugins" / "firefly_vision" / "screen_vision"

if _IMPL.is_dir():
    _impl_root = str(_IMPL.parent)
    if _impl_root not in sys.path:
        sys.path.insert(0, _impl_root)

__path__ = [str(_SELF)]


def _available() -> bool:
    from core.capabilities import is_available

    return is_available("vision")


def _missing():
    from core.capabilities import CapabilityMissingError, missing_message

    raise CapabilityMissingError("vision", missing_message("vision"))


def __getattr__(name: str):
    if not _available():
        _missing()
    import importlib

    try:
        return importlib.import_module("screen_vision." + name)
    except ImportError:
        pass
    impl = importlib.import_module("screen_vision")
    try:
        return getattr(impl, name)
    except AttributeError as exc:
        raise AttributeError("screen_vision has no attribute " + repr(name)) from exc
