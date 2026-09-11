"""Parse the per-character JSON files of ZZZ-Model-Hash (角色hash表 folder)."""

import json
from dataclasses import dataclass
from pathlib import Path

from .model import HASH_FULL_RE, Character, Component, iter_texture_triples
from .repo import characters_dir


@dataclass
class HashRef:
    """One place a hash is used, for the hash -> usages reverse index."""

    character: str
    component: str
    role: str


class CharacterDB:
    """Every parsed character JSON plus a lowercased-hash reverse index."""

    def __init__(self) -> None:
        self.characters: dict[str, Character] = {}
        self.reverse: dict[str, list[HashRef]] = {}


def load_characters(repo_dir: Path) -> CharacterDB:
    """Load every .json in repo_dir/角色hash表 into a CharacterDB.

    Files are visited sorted by filename so the reverse index order is
    reproducible; a non-decodable file raises a ValueError naming the file.
    """
    db = CharacterDB()
    for path in sorted(characters_dir(repo_dir).glob("*.json"), key=lambda p: p.name):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path.name}: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError(f"expected a JSON array in {path.name}")
        character = Character(name=path.stem)
        for entry in data:
            if not isinstance(entry, dict) or "component_name" not in entry:
                continue
            underscore_keys = sum(1 for key in entry if key.startswith("_"))
            if underscore_keys > len(entry) - underscore_keys:
                continue
            component = _parse_component(entry)
            character.components.append(component)
            _add_reverse_refs(db.reverse, character.name, component)
        db.characters[character.name] = character
    return db


def _parse_component(entry: dict) -> Component:
    """Build a Component from one JSON dict of a character file."""
    fields: dict[str, str] = {}
    for key, value in entry.items():
        if key.startswith("_") or key == "component_name":
            continue
        if isinstance(value, str) and HASH_FULL_RE.match(value):
            fields[key.lower()] = value.lower()
    return Component(
        name=entry["component_name"],
        fields=fields,
        texture_hashes=entry.get("texture_hashes"),
        object_indexes=_valid_object_indexes(entry.get("object_indexes")),
        object_classifications=_valid_object_classifications(
            entry.get("object_classifications")
        ),
    )


def _valid_object_indexes(value: object) -> list[int] | None:
    """Return value when it is a non-empty list of plain ints."""
    if not (isinstance(value, list) and value):
        return None
    if all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        return value
    return None


def _valid_object_classifications(value: object) -> list[str] | None:
    """Return value when it is a list of strings."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def _add_reverse_refs(
    reverse: dict[str, list[HashRef]], character: str, component: Component
) -> None:
    """Index every hash field and texture hash of one component."""
    for role, hash_value in component.fields.items():
        reverse.setdefault(hash_value, []).append(
            HashRef(character, component.name, role)
        )
    _index_texture_hashes(reverse, character, component.name, component.texture_hashes)


def _index_texture_hashes(
    reverse: dict[str, list[HashRef]],
    character: str,
    component_name: str,
    node: object,
) -> None:
    """Walk texture_hashes recursively and index [type, ext, hash] triples."""
    for role, hash_value in iter_texture_triples(node):
        reverse.setdefault(hash_value.lower(), []).append(
            HashRef(character, component_name, role.lower())
        )
