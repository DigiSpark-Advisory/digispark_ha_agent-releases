"""Automation version history + diff.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md 12 - see PROVENANCE.md.

Every agent-managed automation gets an immutable, append-only version history:
one record per draft/accept/discard/update/rollback carrying a timestamp, the
author, a short change note, and the full automation body. Scoped to
agent-prefixed ids only - history is never written for user-hand-authored
automations (SPEC 12).

Pure filesystem logic - no Home Assistant imports; callers run it in an
executor (the event loop must not block on disk I/O). The store file is JSON:

    {"schema": 1, "automations": {"<agent id>": [<records, oldest first>]}}

Writes are atomic (temp file + fsync + os.replace) preserving the original
file's permission bits. A corrupt or wrong-shape store file is rejected
untouched - history is never silently overwritten. Each history is pruned to
the newest ``max_versions`` records; version numbers stay monotonic across
pruning so a record's identity never changes.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable
from difflib import unified_diff
from pathlib import Path

from ..automations.writer import AGENT_ID_PREFIX

SCHEMA_VERSION = 1
MAX_VERSIONS_PER_AUTOMATION = 50

# The recordable lifecycle actions (SPEC 12).
ACTIONS: frozenset[str] = frozenset(
    {"draft", "accept", "discard", "update", "rollback"}
)

_SUMMARY_KEYS = ("version", "timestamp", "author", "action", "note")


class VersionStoreError(ValueError):
    """The version store could not be read or written safely."""


def utc_now_iso() -> str:
    """The current UTC time in ISO 8601 (seconds precision)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class VersionStore:
    """Append-only version history for agent-managed automations (SPEC 12).

    One store instance manages one JSON file. Methods are synchronous and do
    disk I/O; Home Assistant callers must run them in an executor. ``clock``
    is injectable for deterministic tests and must return an ISO 8601 string.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_versions: int = MAX_VERSIONS_PER_AUTOMATION,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if max_versions < 1:
            raise ValueError("max_versions must be >= 1")
        self._path = Path(path)
        self._max_versions = max_versions
        self._clock = clock or utc_now_iso

    def record(
        self,
        automation_id: str,
        *,
        action: str,
        body: dict,
        author: str,
        note: str = "",
    ) -> dict:
        """Append one immutable version record and return a copy of it.

        Refuses non-agent automation ids and unknown actions. The body is
        normalized to plain JSON types so ruamel/YAML node types can never
        leak into the store.
        """
        automation_id = _agent_id(automation_id)
        if action not in ACTIONS:
            raise VersionStoreError(f"unknown version action {action!r}")
        body = _plain_json_mapping(body)

        data = self._load()
        history = data["automations"].setdefault(automation_id, [])
        version = history[-1]["version"] + 1 if history else 1
        record = {
            "version": version,
            "timestamp": str(self._clock()),
            "author": str(author),
            "action": action,
            "note": str(note),
            "body": body,
        }
        history.append(record)
        # Prune oldest records past the cap; numbering stays monotonic.
        del history[: max(0, len(history) - self._max_versions)]
        self._write(data)
        return json.loads(json.dumps(record))

    def list_versions(self, automation_id: str) -> list[dict]:
        """Body-less summaries of one automation's history, oldest first.

        An agent id with no recorded history yields an empty list; non-agent
        ids are refused, consistent with every agent-scoped operation.
        """
        automation_id = _agent_id(automation_id)
        history = self._load()["automations"].get(automation_id, [])
        return [{key: record[key] for key in _SUMMARY_KEYS} for record in history]

    def get_version(self, automation_id: str, version: int) -> dict:
        """The full record (including body) for one version number."""
        automation_id = _agent_id(automation_id)
        history = self._load()["automations"].get(automation_id, [])
        for record in history:
            if record["version"] == version:
                return json.loads(json.dumps(record))
        raise VersionStoreError(
            f"no version {version!r} recorded for {automation_id!r}"
        )

    def latest_body(self, automation_id: str) -> dict | None:
        """The most recently recorded body, or None with no history."""
        automation_id = _agent_id(automation_id)
        history = self._load()["automations"].get(automation_id, [])
        if not history:
            return None
        return json.loads(json.dumps(history[-1]["body"]))

    def diff(self, automation_id: str, old: int, new: int) -> list[str]:
        """Unified-diff lines between two recorded bodies (for the panel).

        Bodies are rendered as stable, sorted-key JSON so the diff reflects
        content changes, not serialization noise. Identical bodies yield an
        empty list.
        """
        old_record = self.get_version(automation_id, old)
        new_record = self.get_version(automation_id, new)
        return list(
            unified_diff(
                _body_lines(old_record["body"]),
                _body_lines(new_record["body"]),
                fromfile=f"version {old}",
                tofile=f"version {new}",
                lineterm="",
            )
        )

    def _load(self) -> dict:
        """Load and shape-check the store file; reject anything suspect.

        A missing file is an empty store. A file that cannot be parsed or has
        an unexpected shape raises rather than being overwritten - history is
        never silently discarded.
        """
        if not self._path.exists():
            return {"schema": SCHEMA_VERSION, "automations": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise VersionStoreError(
                f"version store could not be parsed: {err}"
            ) from err
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != SCHEMA_VERSION
            or not isinstance(raw.get("automations"), dict)
            or not all(
                isinstance(history, list) for history in raw["automations"].values()
            )
        ):
            raise VersionStoreError(
                "version store file has an unexpected shape; refusing to overwrite it"
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
                dir=str(self._path.parent), prefix=".versions_", suffix=".tmp"
            )
        except OSError as err:
            raise VersionStoreError(
                f"version store could not be written: {err}"
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


def _agent_id(automation_id: str) -> str:
    """Normalize and enforce the agent-id scope (SPEC 12)."""
    automation_id = str(automation_id)
    if not automation_id.startswith(AGENT_ID_PREFIX):
        raise VersionStoreError(
            f"{automation_id!r} is not an agent-managed automation id"
        )
    return automation_id


def _plain_json_mapping(body: dict) -> dict:
    """Deep-normalize a body to plain JSON types; refuse anything else."""
    if not isinstance(body, dict):
        raise VersionStoreError("automation body must be a mapping")
    try:
        return json.loads(json.dumps(body))
    except (TypeError, ValueError) as err:
        raise VersionStoreError(
            f"automation body is not JSON-serializable: {err}"
        ) from err


def _body_lines(body: dict) -> list[str]:
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False).splitlines()
