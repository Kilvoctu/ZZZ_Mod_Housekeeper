"""Parse and merge the dev-time legacy (pre-changelog) hash chain dataset.

The dev-time dataset data/legacy_chains.json (built by generate_legacy_chains.py
from ZZZ-Mod-Fixer reference modules) holds one pre-changelog transition per
character, indexed below the real changelog so resolution walks legacy first.
"""

import json
import re
from pathlib import Path

from .model import HASH_RE, ChangeEntry
from .repo import data_dir

_LABEL_RE = re.compile(r"^\s*(?P<va>[\d.]+[A-Za-z]*)(?:\s*(?:->|→)\s*(?P<vb>[\d.]+[A-Za-z]*))?")
_INF = float("inf")


def legacy_chains_path() -> Path:
    """Static dataset: <project root>/data/legacy_chains.json (dev-time generated)."""
    return data_dir("legacy_chains.json")


def _version_key(token: str) -> tuple[float, str]:
    """Sort key for one version token like "1.5A": numeric part first."""
    numeric = re.match(r"[\d.]+", token)
    try:
        number = float(numeric.group(0)) if numeric else _INF
    except ValueError:
        number = _INF
    return number, token


def _label_sort_key(label: str) -> tuple[tuple[float, str], tuple[float, str]]:
    """Chronological sort key for one label like "1.5A -> 1.5B".

    Single-version labels ("1.5A") parse as from-version only, sorting (v, v);
    unparseable labels sort last.
    """
    match = _LABEL_RE.match(label)
    if match is None:
        return (_INF, ""), (_INF, "")
    va = _version_key(match.group("va"))
    vb = match.group("vb")
    return va, _version_key(vb) if vb else va


def parse_legacy_chains(path: Path, first_index: int = 1) -> list[ChangeEntry]:
    """Parse the harvested pre-2.0 transitions into ordered ChangeEntry objects.

    Entries group by version_label; version_index runs first_index.. in label sort
    order ((from, to) key), so legacy steps sort strictly before the real changelog;
    load_fixer_data renumbers them above pcdata ordinals when both datasets load.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    grouped: dict[str, list[ChangeEntry]] = {}
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            from_hash = item.get("from")
            to_hash = item.get("to")
            if not isinstance(from_hash, str) or not isinstance(to_hash, str):
                continue
            if not HASH_RE.fullmatch(from_hash) or not HASH_RE.fullmatch(to_hash):
                continue
            raw_characters = item.get("characters")
            characters = (
                [str(name) for name in raw_characters]
                if isinstance(raw_characters, list)
                else []
            )
            raw_label = item.get("label")
            label = raw_label if isinstance(raw_label, str) else ""
            grouped.setdefault(label, []).append(
                ChangeEntry(
                    from_hash=from_hash.lower(),
                    to_hash=to_hash.lower(),
                    characters=characters,
                    role="legacy",
                    version_label=label,
                )
            )
    result: list[ChangeEntry] = []
    for position, (_label, entries) in enumerate(
        sorted(grouped.items(), key=lambda pair: _label_sort_key(pair[0])),
        start=first_index,
    ):
        for entry in entries:
            entry.version_index = position
        result.extend(entries)
    return result


def merge_entries(legacy: list[ChangeEntry], real: list[ChangeEntry]) -> list[ChangeEntry]:
    """Shift the real changelog's version_index past the legacy steps; concatenate ascending.

    The shift is the highest legacy version_index (0 if none), not the label count,
    so it stays correct when legacy is renumbered above pcdata ordinals.
    """
    shift = max((entry.version_index for entry in legacy), default=0)
    combined = list(legacy)
    for entry in real:
        entry.version_index += shift
        combined.append(entry)
    return combined
