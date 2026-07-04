"""Conversation sessions. See SPEC.md 7 and PROVENANCE.md."""

from __future__ import annotations

from .store import (
    MAX_SESSIONS,
    SessionNotFoundError,
    SessionStore,
    SessionStoreError,
)

__all__ = [
    "MAX_SESSIONS",
    "SessionNotFoundError",
    "SessionStore",
    "SessionStoreError",
]
