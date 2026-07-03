"""Pattern detection & suggestions. See SPEC.md §11 and PROVENANCE.md."""

from __future__ import annotations

from .engine import (
    KIND_CORRELATION,
    KIND_SEQUENCE,
    KIND_TIME_OF_DAY,
    PatternEngine,
    StateEvent,
)

__all__ = [
    "KIND_CORRELATION",
    "KIND_SEQUENCE",
    "KIND_TIME_OF_DAY",
    "PatternEngine",
    "StateEvent",
]
