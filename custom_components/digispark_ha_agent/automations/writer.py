"""Safe, non-destructive writer for automations.yaml.

Copyright (c) 2026 DigiSpark Advisory LLC. All rights reserved.
Clean-room implementation authored from SPEC.md §6 — see PROVENANCE.md.

Agent-drafted automations are written conservatively: created disabled
(initial_state: false) with an agent-prefixed id and alias, any
enabled/initial_state/id the model supplied is stripped, the new content is
round-trip validated before touching disk, a .bak backup is taken, and the
write is atomic (temp file + fsync + os.replace) preserving the original
file's permission bits. ruamel's round-trip mode preserves unicode, key
order, and comments on unrelated automations. A non-list file is rejected
rather than clobbered; a single-mapping trigger/condition/action is
normalized to a one-element list instead of being dropped.

Home Assistant reads automations.yaml with a YAML 1.1 loader (PyYAML), while
ruamel dumps under YAML 1.2. Scalars that YAML 1.1 resolves to non-strings —
``10:00:00`` (sexagesimal int), ``on`` (bool), ``null``, numeric-like text —
are emitted unquoted by ruamel and then silently retyped on HA's side (the
``at: 10:00:00`` -> 36000 draft-corruption bug). We force a quoted style on
exactly those scalars before dumping, and a YAML 1.1 round-trip guard aborts
the write if any scalar's value would still change on reload.

Pure filesystem logic — no Home Assistant imports; callers run it in an
executor (the event loop must not block on disk I/O).
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import tempfile
from io import StringIO
from pathlib import Path
from uuid import uuid4

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import ScalarString, SingleQuotedScalarString

AGENT_ID_PREFIX = "digispark_agent_"
AGENT_ALIAS_PREFIX = "[DigiSpark Agent] "

# Keys the model must never control: the user's review step cannot be
# bypassed, and ids are assigned by us so an existing automation can never
# be shadowed or overwritten.
_STRIPPED_KEYS = frozenset({"enabled", "initial_state", "id"})
_NORMALIZED_LIST_KEYS = ("trigger", "condition", "action")

# Strings a YAML 1.1 loader (Home Assistant's PyYAML) resolves to a NON-string:
# bool / null / merge / value, every int form INCLUDING sexagesimal
# (``10:00:00`` -> 36000), every float form including sexagesimal and inf/nan,
# and timestamps. Mirrors PyYAML's SafeLoader implicit resolvers; validated for
# exact parity against PyYAML. Used to decide which plain scalars to force-quote
# so they survive HA's reader as strings.
_YAML11_AMBIGUOUS = re.compile(
    r"""^(?:
        yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF
        |~|null|Null|NULL|
        |<<|=
        |[-+]?0b[0-1_]+
        |[-+]?0[0-7_]+
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0x[0-9a-fA-F_]+
        |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+
        |[-+]?(?:[0-9][0-9_]*)?\.[0-9_]*(?:[eE][-+]?[0-9]+)?
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN)
        |[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]
        |[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?
        (?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9]
        (?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?
    )$""",
    re.VERBOSE,
)


class AutomationWriteError(ValueError):
    """A draft could not be written safely; nothing on disk was changed."""


def sanitize_draft(automation: dict) -> dict:
    """Return the conservative, review-gated form of a model-drafted automation.

    Strips model-supplied enabled/initial_state/id, forces
    ``initial_state: false``, prefixes the alias and a fresh id, and
    normalizes single-mapping trigger/condition/action into one-element
    lists (SPEC §6).
    """
    if not isinstance(automation, dict):
        raise AutomationWriteError("automation draft must be a mapping")

    body = {k: v for k, v in automation.items() if k not in _STRIPPED_KEYS}
    for key in _NORMALIZED_LIST_KEYS:
        if key in body and isinstance(body[key], dict):
            body[key] = [body[key]]

    if not body.get("trigger") or not body.get("action"):
        raise AutomationWriteError("automation draft must include trigger and action")

    alias = str(body.pop("alias", "") or "draft automation").strip()
    if not alias.startswith(AGENT_ALIAS_PREFIX):
        alias = f"{AGENT_ALIAS_PREFIX}{alias}"

    return {
        "id": f"{AGENT_ID_PREFIX}{uuid4().hex}",
        "alias": alias,
        "initial_state": False,
        **body,
    }


def write_draft_automation(path: str | Path, automation: dict) -> dict:
    """Append a sanitized draft to automations.yaml; return what was written.

    The full SPEC §6 write path: sanitize, load and validate the existing
    file (reject non-list), round-trip validate the new content in memory,
    back up the original, then atomically replace it preserving permission
    bits. Raises AutomationWriteError without touching disk when anything
    is unsafe.
    """
    target = Path(path)
    draft = sanitize_draft(automation)
    yaml = _yaml()
    data = _load_list(target, yaml)
    data.append(draft)

    # Round-trip validate in memory before touching disk (SPEC §6).
    new_text = _dump_validated(yaml, data)
    written = _yaml().load(new_text)[-1]
    if written.get("initial_state") is not False or written.get("id") != draft["id"]:
        raise AutomationWriteError("round-trip validation failed; write aborted")

    _atomic_write(target, new_text)
    return draft


def list_agent_automations(path: str | Path) -> list[dict]:
    """Summarize agent-managed automations for the review surface.

    Returns id, alias, description, and whether the draft has been accepted
    (initial_state removed). A missing file means no drafts; a non-list file
    is rejected, consistent with the write path.
    """
    target = Path(path)
    if not target.exists():
        return []
    data = _load_list(target, _yaml())
    out: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("id", ""))
        if not entry_id.startswith(AGENT_ID_PREFIX):
            continue
        out.append(
            {
                "id": entry_id,
                "alias": str(entry.get("alias", "")),
                "description": str(entry.get("description", "")),
                "accepted": entry.get("initial_state") is not False,
            }
        )
    return out


def accept_draft(path: str | Path, automation_id: str) -> dict:
    """Accept an agent draft: remove its forced-disabled flag (SPEC §6)."""
    target = Path(path)
    yaml = _yaml()
    data = _load_list(target, yaml, must_exist=True)
    index = _find_agent_entry(data, automation_id)
    data[index].pop("initial_state", None)

    new_text = _dump_validated(yaml, data)
    reparsed = _yaml().load(new_text)
    if "initial_state" in reparsed[index] or reparsed[index].get("id") != automation_id:
        raise AutomationWriteError("round-trip validation failed; write aborted")

    _atomic_write(target, new_text)
    entry = data[index]
    return {"id": automation_id, "alias": str(entry.get("alias", ""))}


def discard_draft(path: str | Path, automation_id: str) -> dict:
    """Remove one agent-managed automation entirely (explicit user action)."""
    target = Path(path)
    yaml = _yaml()
    data = _load_list(target, yaml, must_exist=True)
    index = _find_agent_entry(data, automation_id)
    entry = data.pop(index)

    new_text = _dump_validated(yaml, data)

    _atomic_write(target, new_text)
    return {"id": automation_id, "alias": str(entry.get("alias", ""))}


def list_agent_automation_bodies(path: str | Path) -> list[dict]:
    """Full plain-mapping bodies of every agent-managed automation (SPEC §13)."""
    target = Path(path)
    if not target.exists():
        return []
    data = _load_list(target, _yaml())
    return [
        _plain(entry)
        for entry in data
        if isinstance(entry, dict)
        and str(entry.get("id", "")).startswith(AGENT_ID_PREFIX)
    ]


def get_agent_automation(path: str | Path, automation_id: str) -> dict:
    """Return one agent-managed automation as a plain mapping (SPEC §12)."""
    target = Path(path)
    data = _load_list(target, _yaml(), must_exist=True)
    index = _find_agent_entry(data, automation_id)
    return _plain(data[index])


def replace_agent_automation(path: str | Path, automation_id: str, body: dict) -> dict:
    """Replace one agent-managed automation's body in place (SPEC §12)."""
    if not isinstance(body, dict):
        raise AutomationWriteError("replacement body must be a mapping")
    replacement = dict(body)
    supplied_id = replacement.get("id")
    if supplied_id is not None and supplied_id != automation_id:
        raise AutomationWriteError(
            f"replacement body id {supplied_id!r} does not match {automation_id!r}"
        )
    replacement["id"] = automation_id

    target = Path(path)
    yaml = _yaml()
    data = _load_list(target, yaml, must_exist=True)
    index = _find_agent_entry(data, automation_id)
    data[index] = replacement

    new_text = _dump_validated(yaml, data)
    reparsed = _yaml().load(new_text)
    if reparsed[index].get("id") != automation_id:
        raise AutomationWriteError("round-trip validation failed; write aborted")

    _atomic_write(target, new_text)
    return {"id": automation_id, "alias": str(replacement.get("alias", ""))}


def _plain(value):
    """Deep-convert ruamel round-trip nodes into plain Python containers."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, ScalarString):
        return str(value)
    return value


def _needs_yaml11_quote(value) -> bool:
    """True for a plain str a YAML 1.1 loader would resolve to a non-string."""
    return (
        isinstance(value, str)
        and not isinstance(value, ScalarString)
        and bool(_YAML11_AMBIGUOUS.match(value))
    )


def _quote_yaml11_ambiguous(node) -> None:
    """Force a quoted style on YAML-1.1-ambiguous plain scalars, in place.

    Mutates ``node`` so ruamel round-trip metadata (comments, key order, and
    existing quote styles) on untouched nodes survives. Only plain ``str`` is
    considered; scalars already loaded as ruamel ScalarString keep their own
    style. This is what stops HA's YAML 1.1 loader from retyping ``at:
    10:00:00`` into the integer 36000 (and ``to: on`` into a bool, etc.).
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _quote_yaml11_ambiguous(value)
            elif _needs_yaml11_quote(value):
                node[key] = SingleQuotedScalarString(value)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if isinstance(value, (dict, list)):
                _quote_yaml11_ambiguous(value)
            elif _needs_yaml11_quote(value):
                node[index] = SingleQuotedScalarString(value)


def _load_list(target: Path, yaml: YAML, *, must_exist: bool = False) -> list:
    """Load automations.yaml as a list; reject anything else (SPEC §6)."""
    if not target.exists():
        if must_exist:
            raise AutomationWriteError("automations.yaml does not exist")
        return []
    try:
        data = yaml.load(target.read_text(encoding="utf-8"))
    except YAMLError as err:
        raise AutomationWriteError(
            f"automations.yaml could not be parsed: {err}"
        ) from err
    if data is None:
        return []
    if not isinstance(data, list):
        raise AutomationWriteError(
            "automations.yaml does not contain a list; refusing to overwrite it"
        )
    return data


def _yaml_1_1() -> YAML:
    """A round-trip loader pinned to YAML 1.1 — Home Assistant's read semantics.

    Used only to verify what HA will actually parse back from our output, so
    the scalar-coercion guard sees the same retyping HA would.
    """
    yaml = YAML()
    yaml.version = (1, 1)  # type: ignore[assignment]
    return yaml


def _dump_validated(yaml: YAML, data: list) -> str:
    """Serialize and re-parse in memory; the result must survive a round trip.

    Two guards: (1) quote YAML-1.1-ambiguous scalars so HA's loader keeps them
    as strings, and (2) reparse the emitted text with HA's YAML 1.1 semantics
    and abort if any scalar's value changed — no silent coercion can ship.
    """
    _quote_yaml11_ambiguous(data)
    buffer = StringIO()
    yaml.dump(data, buffer)
    new_text = buffer.getvalue()
    reparsed = _yaml_1_1().load(new_text)
    if reparsed is None:
        reparsed = []
    if not isinstance(reparsed, list) or len(reparsed) != len(data):
        raise AutomationWriteError("round-trip validation failed; write aborted")
    if _plain(reparsed) != _plain(data):
        raise AutomationWriteError("round-trip changed a scalar's value; write aborted")
    return new_text


def _find_agent_entry(data: list, automation_id: str) -> int:
    """Index of the agent-managed entry with this id; refuses non-agent ids."""
    if not str(automation_id).startswith(AGENT_ID_PREFIX):
        raise AutomationWriteError(
            f"{automation_id!r} is not an agent-managed automation id"
        )
    for index, entry in enumerate(data):
        if isinstance(entry, dict) and entry.get("id") == automation_id:
            return index
    raise AutomationWriteError(f"no agent automation with id {automation_id!r}")


def _yaml() -> YAML:
    yaml = YAML()  # round-trip mode: preserves comments, key order, quotes
    yaml.preserve_quotes = True
    yaml.allow_unicode = True  # no \\uXXXX escaping (SPEC §6)
    return yaml


def _atomic_write(target: Path, new_text: str) -> None:
    """Back up the original, then atomically replace it (SPEC §6)."""
    mode: int | None = None
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
        shutil.copy2(target, Path(f"{target}.bak"))

    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".automations_", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(new_text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
