"""Automation versioning. See SPEC.md 12 and PROVENANCE.md."""

from __future__ import annotations

from .store import (
    MAX_VERSIONS_PER_AUTOMATION,
    VersionStore,
    VersionStoreError,
    utc_now_iso,
)

__all__ = [
    "MAX_VERSIONS_PER_AUTOMATION",
    "VersionStore",
    "VersionStoreError",
    "utc_now_iso",
]
