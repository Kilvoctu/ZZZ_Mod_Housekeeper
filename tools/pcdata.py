"""Parse the optional static per-character importer dataset (pcdata).

Each row of PlayerCharacterData.json is one buffer-hash transition whose
ordinal doubles as the version_index; buffer-coupled rows are skipped.
"""

import json
from pathlib import Path

from .model import HASH_RE, ChangeEntry, normalize_name
from .repo import data_dir

KINDS = frozenset({"blend", "draw", "ib", "position", "texcoord"})


def player_character_data_path() -> Path:
    """Static dataset: <project root>/data/PlayerCharacterData.json (user-provided)."""
    return data_dir("PlayerCharacterData.json")


BUFFER_COUPLED_PATH = data_dir("buffer_coupled_hashes.json")


def buffer_coupled_hashes_path() -> Path:
    """Companion exclusion file: <project root>/data/buffer_coupled_hashes.json."""
    return BUFFER_COUPLED_PATH


def load_buffer_coupled_hashes() -> frozenset[str]:
    """The buffer-coupled from-hashes that must never be hash-renamed.

    Reads data/buffer_coupled_hashes.json; a missing or malformed file contributes nothing, and valid 8-hex items are lowercased.
    """
    try:
        data = json.loads(buffer_coupled_hashes_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(
        item.lower()
        for item in data
        if isinstance(item, str) and HASH_RE.fullmatch(item)
    )


def _parse_index_array(value: object) -> list[int] | None:
    """Parse one bracket-array string like "[0,31275]" into a list of ints.

    Returns None when the value is not a bracket-wrapped string of plain non-negative ints; empty brackets "[]" parse as an empty list.
    """
    if not isinstance(value, str):
        return None
    if not value.startswith("[") or not value.endswith("]"):
        return None
    inner = value[1:-1].strip()
    if not inner:
        return []
    numbers: list[int] = []
    for token in inner.split(","):
        token = token.strip()
        if not token.isdecimal():
            return None
        numbers.append(int(token))
    return numbers


def parse_player_character_data(path: Path) -> list[ChangeEntry]:
    """Parse PlayerCharacterData.json into ChangeEntry rows in file order.

    Each valid row becomes one entry with role "pcdata" whose ordinal doubles as the 1-based version_index; invalid rows are skipped and not sorted here.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows: list[ChangeEntry] = []
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, dict):
            continue
        from_hash = item.get("From")
        to_hash = item.get("To")
        if not isinstance(from_hash, str) or not isinstance(to_hash, str):
            continue
        if not HASH_RE.fullmatch(from_hash) or not HASH_RE.fullmatch(to_hash):
            continue
        if from_hash.lower() == to_hash.lower():
            continue
        comment = item.get("Comment")
        if not isinstance(comment, str):
            continue
        parts = comment.split()
        if len(parts) < 4:
            continue
        if not parts[0].isdecimal():
            continue
        kind = parts[-1]
        if kind not in KINDS:
            continue
        char_name = parts[1]
        component = " ".join(parts[2:-1])
        ordinal = int(parts[0])
        rows.append(
            ChangeEntry(
                from_hash=from_hash.lower(),
                to_hash=to_hash.lower(),
                characters=[normalize_name(char_name)],
                role="pcdata",
                component_label=component,
                version_label=f"importer #{ordinal}",
                version_index=ordinal,
                from_indexes=_parse_index_array(item.get("FromIndexes")),
                to_indexes=_parse_index_array(item.get("ToIndexes")),
                from_index_counts=_parse_index_array(item.get("FromIndexCounts")),
                to_index_counts=_parse_index_array(item.get("ToIndexCounts")),
            )
        )
    return rows
