"""Multi-conversation session store.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 7 - see PROVENANCE.md.

Conversation state lives server-side (SPEC 7). v0.6.0 extends the single
implicit conversation to user-managed sessions: each session carries an id,
a title, created/updated timestamps, and the committed message history the
agent loop reads and writes. Sessions persist across Home Assistant restarts
and live until the user deletes them (the 30-minute auto-expiry was retired
with multi-session support - owner decision, 2026-07-03); a cap prunes the
oldest sessions, preferring empty ones, so the store cannot grow unbounded.

Pure filesystem logic - no Home Assistant imports; callers run it in an
executor (the event loop must not block on disk I/O). The store file is JSON:

    {"schema": 1, "sessions": {"<session id>": <record>}}

Writes are atomic (temp file + fsync + os.replace) preserving the original
file's permission bits. A corrupt or wrong-shape store file is rejected
untouched - conversations are never silently overwritten.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from ..versioning import utc_now_iso

SCHEMA_VERSION = 1
# Sessions live until deleted; past the cap the oldest are pruned, empty
# ones first (owner decision, 2026-07-03).
MAX_SESSIONS = 50
# Auto-titles come from the first user message, truncated (owner decision).
TITLE_AUTO_MAX_CHARS = 48
# Manual titles (rename) are bounded too.
TITLE_MAX_CHARS = 80
# Fallback title when a committed conversation has no user text.
DEFAULT_TITLE = "New conversation"

_SUMMARY_KEYS = ("id", "title", "created", "updated")
_ID_ATTEMPTS = 8


class SessionStoreError(ValueError):
    """The session store could not be read or written safely."""


class SessionNotFoundError(SessionStoreError):
    """The addressed session id is not in the store."""


class SessionStore:
    """User-managed conversation sessions (SPEC 7).

    One store instance manages one JSON file. Methods are synchronous and do
    disk I/O; Home Assistant callers must run them in an executor. ``clock``
    (returning an ISO 8601 string) and ``id_factory`` are injectable for
    deterministic tests.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_sessions: int = MAX_SESSIONS,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        self._path = Path(path)
        self._max_sessions = max_sessions
        self._clock = clock or utc_now_iso
        self._id_factory = id_factory or _new_id

    def list_sessions(self) -> list[dict]:
        """Message-less summaries, most recently updated first."""
        data = self._load()
        records = sorted(data["sessions"].values(), key=_recency, reverse=True)
        return [_summary(record) for record in records]

    def create_session(self, title: str = "") -> dict:
        """Create one empty session and return its summary.

        The cap is enforced here: past ``max_sessions`` the oldest sessions
        are pruned, empty ones first; the new session itself is never pruned.
        """
        data = self._load()
        session_id = self._unused_id(data)
        now = str(self._clock())
        data["sessions"][session_id] = {
            "id": session_id,
            "title": _clean_title(title),
            "created": now,
            "updated": now,
            "messages": [],
        }
        self._prune(data, keep=session_id)
        self._write(data)
        return _summary(data["sessions"][session_id])

    def rename_session(self, session_id: str, title: str) -> dict:
        """Set one session's title and return its summary.

        Renaming does not bump ``updated`` - that timestamp tracks
        conversation activity, so a rename never reorders the session list.
        """
        cleaned = _clean_title(title)
        if not cleaned:
            raise SessionStoreError("session title must not be empty")
        data = self._load()
        record = _get(data, session_id)
        record["title"] = cleaned
        self._write(data)
        return _summary(record)

    def delete_session(self, session_id: str) -> dict:
        """Remove one session (explicit user action) and return its summary."""
        data = self._load()
        record = _get(data, session_id)
        del data["sessions"][record["id"]]
        self._write(data)
        return _summary(record)

    def get_messages(self, session_id: str) -> list[dict]:
        """A copy of one session's committed message history."""
        data = self._load()
        return json.loads(json.dumps(_get(data, session_id)["messages"]))

    def commit_messages(self, session_id: str, messages: list[dict]) -> dict:
        """Replace one session's history after a completed turn.

        Bumps ``updated`` and, while the session is untitled, derives the
        title from the first user message (SPEC 7).
        """
        plain = _plain_json_messages(messages)
        data = self._load()
        record = _get(data, session_id)
        record["messages"] = plain
        record["updated"] = str(self._clock())
        if not record["title"]:
            record["title"] = _auto_title(plain)
        self._write(data)
        return _summary(record)

    def latest_session_id(self) -> str | None:
        """The most recently updated session's id, or None when empty."""
        data = self._load()
        records = sorted(data["sessions"].values(), key=_recency, reverse=True)
        return records[0]["id"] if records else None

    def _unused_id(self, data: dict) -> str:
        for _ in range(_ID_ATTEMPTS):
            session_id = str(self._id_factory())
            if session_id and session_id not in data["sessions"]:
                return session_id
        raise SessionStoreError("could not allocate an unused session id")

    def _prune(self, data: dict, *, keep: str) -> None:
        """Prune past the cap: oldest first, empty sessions before non-empty."""
        while len(data["sessions"]) > self._max_sessions:
            candidates = sorted(
                (r for r in data["sessions"].values() if r["id"] != keep),
                key=_recency,
            )
            if not candidates:
                return
            empty = [r for r in candidates if not r["messages"]]
            victim = (empty or candidates)[0]
            del data["sessions"][victim["id"]]

    def _load(self) -> dict:
        """Load and shape-check the store file; reject anything suspect.

        A missing file is an empty store. A file that cannot be parsed or
        has an unexpected shape raises rather than being overwritten -
        conversations are never silently discarded.
        """
        if not self._path.exists():
            return {"schema": SCHEMA_VERSION, "sessions": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise SessionStoreError(
                f"session store could not be parsed: {err}"
            ) from err
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != SCHEMA_VERSION
            or not isinstance(raw.get("sessions"), dict)
            or not all(_record_ok(r) for r in raw["sessions"].values())
        ):
            raise SessionStoreError(
                "session store file has an unexpected shape; refusing to overwrite it"
            )
        return raw

    def _write(self, data: dict) -> None:
        """Atomically replace the store file, preserving permission bits."""
        new_text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
        mode: int | None = None
        if self._path.exists():
            mode = stat.S_IMODE(self._path.stat().st_mode)
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".sessions_", suffix=".tmp"
            )
        except OSError as err:
            raise SessionStoreError(
                f"session store could not be written: {err}"
            ) from err
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_text)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(tmp_name, mode)
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _recency(record: dict) -> tuple[str, str, str]:
    """Sort key: updated, tie-broken by created then id (all strings)."""
    return (record["updated"], record["created"], record["id"])


def _summary(record: dict) -> dict:
    summary = {key: record[key] for key in _SUMMARY_KEYS}
    summary["message_count"] = len(record["messages"])
    return summary


def _get(data: dict, session_id: str) -> dict:
    record = data["sessions"].get(str(session_id))
    if record is None:
        raise SessionNotFoundError(f"no session {session_id!r}")
    return record


def _record_ok(record) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("id"), str)
        and isinstance(record.get("title"), str)
        and isinstance(record.get("created"), str)
        and isinstance(record.get("updated"), str)
        and isinstance(record.get("messages"), list)
    )


def _clean_title(title: str) -> str:
    """Collapse whitespace and bound the length (manual titles)."""
    return " ".join(str(title).split())[:TITLE_MAX_CHARS]


def _auto_title(messages: list[dict]) -> str:
    """Title from the first user message, truncated (owner decision)."""
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        text = " ".join(content.split())
        if not text:
            continue
        if len(text) > TITLE_AUTO_MAX_CHARS:
            return text[: TITLE_AUTO_MAX_CHARS - 1].rstrip() + "…"
        return text
    return DEFAULT_TITLE


def _plain_json_messages(messages: list[dict]) -> list[dict]:
    """Deep-normalize a history to plain JSON types; refuse anything else."""
    if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
        raise SessionStoreError("session history must be a list of mappings")
    try:
        return json.loads(json.dumps(messages))
    except (TypeError, ValueError) as err:
        raise SessionStoreError(
            f"session history is not JSON-serializable: {err}"
        ) from err
