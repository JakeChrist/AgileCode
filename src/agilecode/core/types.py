"""Compatibility exports for definitions now owned by :mod:`agilecode.domain`.

New definition consumers should import from ``agilecode.domain.types``. Runtime
context remains available here for existing callers, but is loaded only when
requested so importing project definitions cannot load the execution layer.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agilecode.domain.types import *
from agilecode.domain.types import __all__ as _definition_exports

if TYPE_CHECKING:
    from agilecode.runtime.context import StepContext

__all__ = [*_definition_exports, "StepContext"]


def __getattr__(name: str):
    if name == "StepContext":
        from agilecode.runtime.context import StepContext
        return StepContext
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
