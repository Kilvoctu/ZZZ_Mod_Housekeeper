"""Plain data models for ZZZ-Model-Hash data and fix suggestions."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

_PAREN_GROUP_RE = re.compile(r"\([^()（）]*\)|（[^()（）]*）")
_REMOVED_CHARS = "-_（）()"

HASH_RE = re.compile(r"[0-9a-fA-F]{8}")
HASH_FULL_RE = re.compile(r"^[0-9a-fA-F]{8}$")


def normalize_name(value: str) -> str:
    """Normalize a character/mod name for cross-matching.

    Drops parenthetical segments, removes whitespace, dashes and underscores, then lowercases, e.g. "哲-皮肤WiseSkin（头发共用）" becomes "哲皮肤wiseskin".
    """
    value = _PAREN_GROUP_RE.sub("", value)
    kept = (ch for ch in value if not ch.isspace() and ch not in _REMOVED_CHARS)
    return "".join(kept).lower()


def iter_texture_triples(node: object) -> Iterator[tuple[str, str]]:
    """Yield (role, hash) for every [role, ext, hash] string triple in a
    nested texture_hashes structure."""
    if not isinstance(node, list):
        return
    if len(node) == 3 and all(isinstance(part, str) for part in node):
        role, _ext, hash_value = node
        if HASH_FULL_RE.match(hash_value):
            yield role, hash_value
        return
    for item in node:
        yield from iter_texture_triples(item)


@dataclass
class ChangeEntry:
    """One parsed line of the Chinese changelog (Hash变动日志.txt)."""

    from_hash: str | None = None
    to_hash: str | None = None
    version_index: int = 0
    characters: list[str] = field(default_factory=list)
    role: str = ""
    component_label: str | None = None
    from_indexes: list[int] | None = None
    to_indexes: list[int] | None = None
    from_index_counts: list[int] | None = None
    to_index_counts: list[int] | None = None
    version_label: str = ""


@dataclass
class Component:
    """One component entry of a character JSON."""

    name: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    texture_hashes: list | None = None
    object_indexes: list[int] | None = None
    object_classifications: list[str] | None = None


@dataclass
class Character:
    """One per-character JSON file."""

    name: str = ""
    components: list[Component] = field(default_factory=list)


@dataclass
class FixSuggestion:
    """A single proposed replacement in a 3DMigoto .ini file."""

    file: str = ""
    section: str | None = None
    line_no: int = 0
    kind: str = ""
    old: str = ""
    new: str = ""
    labels: str = ""
    reason: str = ""
    after_line: int = 0
    insert_text: str = ""
