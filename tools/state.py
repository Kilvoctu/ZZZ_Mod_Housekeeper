"""Consolidated app state: settings, presets and promoted previews in one JSON.

state.json lives next to the exe: {"version": 1, "settings": {}, "presets": {},
"promoted": {}}.  Loads are tolerant (a missing or unreadable file yields empty
sections, never an exception); saves are atomic (temp file + os.replace).  The
version field exists so future format changes can migrate in place.
"""

import json
import os
from pathlib import Path

from .repo import project_root

_JSON_NAME = "state.json"
_STATE_VERSION = 1
_SECTIONS = ("settings", "presets", "promoted")
_SECTION_VALUE_TYPES = (dict, list, str, int, float, bool)


def state_path(root: Path | None = None) -> Path:
    """JSON file holding the consolidated app state in project root."""
    return Path(root if root is not None else project_root()) / _JSON_NAME


def load_state(root: Path | None = None) -> dict[str, object]:
    """Read the consolidated state; a missing file yields the empty state.

    A present state.json is read tolerantly: unreadable or malformed content
    yields empty sections instead of an exception.  A missing state.json
    returns the normalized empty state (version plus three empty sections)
    without writing anything.
    """
    path = state_path(root)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        return _normalize(raw)
    return _normalize({})


def save_state(state: dict, root: Path | None = None) -> None:
    """Atomically write the consolidated state, forcing the current version."""
    payload: dict[str, object] = {"version": _STATE_VERSION}
    for name in _SECTIONS:
        section = state.get(name)
        payload[name] = section if isinstance(section, dict) else {}
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(_JSON_NAME + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _normalize(raw: object) -> dict[str, object]:
    """Keep the three known sections, dropping malformed entries per section."""
    state: dict[str, object] = {
        "version": _STATE_VERSION,
        "settings": {},
        "presets": {},
        "promoted": {},
    }
    if not isinstance(raw, dict):
        return state
    version = raw.get("version")
    if isinstance(version, int) and not isinstance(version, bool):
        state["version"] = version
    for name in _SECTIONS:
        section = raw.get(name)
        if isinstance(section, dict):
            state[name] = {
                key: value
                for key, value in section.items()
                if isinstance(key, str) and isinstance(value, _SECTION_VALUE_TYPES)
            }
    return state
