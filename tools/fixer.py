"""Scan 3DMigoto .ini mod files and fix stale hashes using ZZZ-Model-Hash data.

Resolves ``hash =`` lines through changelog chains, remaps index values through
IB object-index changes, and preserves untouched lines and encoding byte-for-byte.
"""

import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .backups import backup_path_for, included_ini_files, move_file
from .changelog import (
    build_chain_index,
    parse_changelog_file,
    parse_face_texcoord_transitions_file,
)
from .characters import (
    CharacterDB,
    HashRef,
    face_texcoord_hashes,
    is_face_component,
    load_characters,
)
from .dumpdata import DumpData
from .legacy import legacy_chains_path, merge_entries, parse_legacy_chains
from .model import ChangeEntry, Component, FixSuggestion, normalize_name
from .patches import load_user_patches
from .pcdata import (
    load_buffer_coupled_hashes,
    parse_player_character_data,
    player_character_data_path,
)
from .repo import changelog_path
from .structure import StructureData, latin_suffix

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^]]+)]\s*$")
_HASH_LINE_RE = re.compile(r"^\s*hash\s*=\s*(?P<hash>[0-9a-fA-F]{8})\s*$")
_FIRST_INDEX_RE = re.compile(r"^\s*match_first_index\s*=\s*(?P<value>\d+)\s*$")
_INDEX_COUNT_RE = re.compile(r"^\s*match_index_count\s*=\s*(?P<value>\d+)\s*$")
_CUSTOM_BINARY_RE = re.compile(r"(?im)^\s*filename\s*=\s*\S+\.(?:ib|buf)\s*$")

HAIR_COMMANDLISTS_KIND = "hair_commandlists"
HAIR_DISPATCH_KIND = "dispatch_run"
DEFER_WRAP_KIND = "defer_wrap"
DEFER_UNIFY_KIND = "defer_unify"
STRUCTURAL_KINDS = frozenset(
    {
        "insert_run",
        "add_section",
        "multiply_section",
        HAIR_COMMANDLISTS_KIND,
        DEFER_WRAP_KIND,
    }
)
INDEX_WARNING_KIND = "index_warning"
WARNING_KINDS = frozenset({INDEX_WARNING_KIND})
SECTION_DELETE_KIND = "delete_section"
RABBITFX_KIND = "rabbitfx"
_ANY_RUN_LINE_RE = re.compile(r"^\s*run\s*=", re.IGNORECASE)
_DRAW_COMMAND_RE = re.compile(r"^\s*(?:draw|drawindexed|drawindexedcheat|drawraw)\b")
_RESOURCE_BIND_RE = re.compile(r"^\s*(?:ib|vb\d+|ps-t\d+|this)\s*=")
_PRIORITY_RE = re.compile(r"^\s*match_priority\s*=")
_BLANK_RE = re.compile(r"^\s*$")

_HAIR_DISPATCH_RE = re.compile(r"^checktextureoverride\s*=\s*ib\s*$", re.IGNORECASE)
_HAIR_HANDLING_RE = re.compile(r"^handling\s*=\s*skip\s*$", re.IGNORECASE)
_HAIR_DRAW_TYPE_EXCLUDE_RE = re.compile(
    r"(?<![\w$])draw_type\s*==\s*(?:2|4)(?!\d)", re.IGNORECASE
)
_HAIR_CLAIM_MARKER_RE = re.compile(r"(?<![a-z0-9])ib[\s._-]*$", re.IGNORECASE)
_HAIR_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z0-9]+")
_HAIR_COMMANDLIST_PREFIX = "commandlist."
_HAIR_STEM_SEPARATORS = "._- "

_UTF8_BOM = b"\xef\xbb\xbf"
_TEXTUREOVERRIDE_PREFIX = "textureoverride"


def _strip_texture_override(section_name: str) -> str:
    """Section name with a leading "TextureOverride" prefix removed (case-insensitive)."""
    name = section_name.strip()
    if name.lower().startswith(_TEXTUREOVERRIDE_PREFIX):
        name = name[len(_TEXTUREOVERRIDE_PREFIX):]
    return name


@dataclass
class FixerData:
    """All parsed data needed to scan and fix mod files."""

    chains: dict[str, list[ChangeEntry]]
    ib_index_changes: dict[str, list[ChangeEntry]]
    db: CharacterDB
    entries: list[ChangeEntry] = field(default_factory=list)
    user_patches: dict[str, ChangeEntry] = field(default_factory=dict)
    face_texcoord_hashes: frozenset[str] = field(default_factory=frozenset)
    dumps: DumpData = field(default_factory=DumpData)


def empty_fixer_data() -> FixerData:
    """A FixerData with no known hashes: safe for scan/analyze, fixes nothing."""
    return FixerData(chains={}, ib_index_changes={}, db=CharacterDB())


def _collect_face_texcoord_hashes(
    changelog_file: Path | None, entries: list[ChangeEntry], db: CharacterDB
) -> frozenset[str]:
    """Every known face-texcoord hash for upgrade gating.

    Sources: changelog face-block texcoord transitions (from and to) and the character table's
    face-component texcoord fields, plus older chain steps whose to-hash reached that set (stale mods match too)."""
    hashes: set[str] = set()
    if changelog_file is not None and changelog_file.is_file():
        transitions = parse_face_texcoord_transitions_file(changelog_file)
        hashes.update(transitions)
        hashes.update(transitions.values())
    hashes.update(face_texcoord_hashes(db))
    steps: list[tuple[str, str]] = []
    for entry in entries:
        if entry.role in ("texcoord", "texcoord_vb"):
            from_hash = entry.from_hash
            to_hash = entry.to_hash
            if from_hash is not None and to_hash is not None:
                steps.append((from_hash, to_hash))
    changed = True
    while changed:
        changed = False
        for from_hash, to_hash in steps:
            if to_hash in hashes and from_hash not in hashes:
                hashes.add(from_hash)
                changed = True
    return frozenset(hashes)


def load_fixer_data(
    repo_dir: Path | None,
    *,
    include_pcdata: bool = False,
    dumps: DumpData | None = None,
) -> FixerData:
    """Parse the data repo changelog and character JSONs into FixerData.

    Legacy pre-2.0 history merges ahead of the changelog; ``include_pcdata`` ingests the importer dataset as hint-gated gap-fill (buffer-coupled hashes excluded); ``dumps`` attaches pre-parsed dump data.
    ``repo_dir=None`` skips the changelog and character DB entirely while legacy chains, pcdata gap-fill and user patches still apply."""
    if repo_dir is None:
        real: list[ChangeEntry] = []
    else:
        repo_dir = Path(repo_dir)
        real = parse_changelog_file(changelog_path(repo_dir))
    pc_rows: list[ChangeEntry] = []
    excluded: frozenset[str] = frozenset()
    first_index = 1
    if include_pcdata and player_character_data_path().exists():
        pc_rows = parse_player_character_data(player_character_data_path())
        if pc_rows:
            first_index = max(entry.version_index for entry in pc_rows) + 1
    if legacy_chains_path().exists():
        entries = merge_entries(
            parse_legacy_chains(legacy_chains_path(), first_index=first_index), real
        )
    else:
        entries = real
    chains = build_chain_index(entries)
    if pc_rows:
        excluded = load_buffer_coupled_hashes()
        chain_gap = [
            entry
            for entry in pc_rows
            if entry.from_hash not in chains
            and entry.from_hash not in excluded
            and entry.to_hash not in excluded
        ]
        if chain_gap:
            chain_gap.sort(
                key=lambda entry: (
                    entry.version_index,
                    entry.characters,
                    entry.component_label or "",
                    entry.from_hash,
                    entry.to_hash,
                )
            )
            entries = chain_gap + entries
            chains = build_chain_index(entries)
    ib_index_changes: dict[str, list[ChangeEntry]] = {}
    for entry in entries:
        if (
            entry.role in ("object_indexes", "pcdata")
            and entry.from_hash is not None
            and entry.from_indexes is not None
            and entry.to_indexes is not None
        ):
            ib_index_changes.setdefault(entry.from_hash.lower(), []).append(entry)
    for entry in pc_rows:
        if entry.from_hash is None:
            continue
        if entry.from_hash in ib_index_changes:
            continue
        if entry.from_hash in excluded:
            continue
        if entry.to_hash in excluded:
            continue
        has_indexes = entry.from_indexes is not None and entry.to_indexes is not None
        has_counts = (
            entry.from_index_counts is not None and entry.to_index_counts is not None
        )
        if not (has_indexes or has_counts):
            continue
        ib_index_changes.setdefault(entry.from_hash, []).append(entry)
    for bucket in ib_index_changes.values():
        bucket.sort(key=lambda change: change.version_index)
    patches = load_user_patches()
    max_index = max((entry.version_index for entry in entries), default=0)
    for patch in patches.values():
        patch.version_index = max_index + 2
    db = CharacterDB() if repo_dir is None else load_characters(repo_dir)
    return FixerData(
        chains=chains,
        ib_index_changes=ib_index_changes,
        db=db,
        entries=entries,
        user_patches=patches,
        face_texcoord_hashes=_collect_face_texcoord_hashes(
            changelog_path(repo_dir) if repo_dir is not None else None, entries, db
        ),
        dumps=DumpData() if dumps is None else dumps,
    )


def known_hashes(data: FixerData) -> set[str]:
    """Every hash the dataset can explain, lowercased.

    Union of the character-DB reverse index, changelog chain sources and targets: anything scan/fix logic can classify."""
    known = set(data.db.reverse)
    known.update(data.chains)
    for entry in data.entries:
        if entry.to_hash:
            known.add(entry.to_hash.lower())
    for patch in data.user_patches.values():
        if patch.from_hash:
            known.add(patch.from_hash)
        if patch.to_hash:
            known.add(patch.to_hash)
    return known


def hash_is_outdated(h: str, data: FixerData, hint: str = "") -> bool:
    """Pessimistic changelog outdatedness for one hash.

    True when chain resolution is ambiguous (None) or leads to a different hash; a round-trip
    means already current, and a matching hint fires hint-gated legacy edges."""
    resolved = resolve_hash_chain(h, hint, data)
    return resolved is None or bool(resolved)


def detect_variant(
    hashes: Iterable[str],
    datasets: Mapping[str, FixerData],
    known: Mapping[str, set[str]] | None = None,
) -> str | None:
    """Detect which dataset variant a scope's hashes belong to.

    Scores count only exclusive matches; highest exclusive score wins, ties break by total
    known hashes, no exclusive match returns None.  ``known`` precomputes sets."""
    if known is None:
        known = {variant: known_hashes(data) for variant, data in datasets.items()}
    exclusive = dict.fromkeys(known, 0)
    total = dict.fromkeys(known, 0)
    for hash_value in {value.lower() for value in hashes}:
        owners = [variant for variant, keys in known.items() if hash_value in keys]
        for variant in owners:
            total[variant] += 1
        if len(owners) == 1:
            exclusive[owners[0]] += 1
    if not any(exclusive.values()):
        return None
    best = max(exclusive.values())
    leaders = [variant for variant, score in exclusive.items() if score == best]
    if len(leaders) == 1:
        return leaders[0]
    return max(leaders, key=lambda variant: total[variant])


@dataclass
class FilePlan:
    """The fix suggestions found for one .ini file."""

    path: str
    suggestions: list[FixSuggestion] = field(default_factory=list)


def _decode_ini(data: bytes) -> tuple[str, str]:
    """Decode raw .ini bytes, returning (text, encoding name for writing back)."""
    if data.startswith(_UTF8_BOM):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("gbk"), "gbk"
    except UnicodeDecodeError as exc:
        raise ValueError("unsupported encoding") from exc


def _split_eol(line: str) -> tuple[str, str]:
    """Split one kept-ends line into (content, eol suffix)."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    if line.endswith("\r"):
        return line[:-1], "\r"
    return line, ""


def hint_matches(characters: list[str], hint: str) -> bool:
    """True when any normalized character name's latin suffix occurs in the hint."""
    if not hint:
        return False
    for name in characters:
        tail = latin_suffix(name)
        if tail and tail in hint:
            return True
    return False


def _section_hint(section_name: str) -> str:
    """Normalized hint from an INI section name, minus a leading TextureOverride."""
    return normalize_name(_strip_texture_override(section_name))


def resolve_hash_chain(h: str, hint: str, data: FixerData) -> list[ChangeEntry] | None:
    """Follow the changelog chain for h in ascending version order.

    Returns the applied steps, or None when ambiguous: candidates sharing the lowest
    version_index but differing to_hash, unresolved by the hint; [] on a round-trip."""
    patch = data.user_patches.get(h.lower())
    if patch is not None:
        if patch.to_hash and patch.to_hash != h.lower():
            return [patch]
        return []
    current = h
    last = 0
    steps: list[ChangeEntry] = []
    while True:
        bucket = data.chains.get(current)
        if not bucket:
            break
        candidates = [
            entry
            for entry in bucket
            if entry.version_index > last
            and entry.to_hash is not None
            and entry.to_hash != current
        ]
        if not candidates:
            break
        preferred = [entry for entry in candidates if hint_matches(entry.characters, hint)]
        pool = preferred or [
            entry for entry in candidates if entry.role not in ("legacy", "pcdata")
        ]
        if not pool:
            break
        min_index = min(entry.version_index for entry in pool)
        chosen = [entry for entry in pool if entry.version_index == min_index]
        if len({entry.to_hash for entry in chosen}) > 1:
            return None
        steps.append(chosen[0])
        last = chosen[0].version_index
        next_hash = chosen[0].to_hash
        if next_hash is None:
            break
        current = next_hash
    if steps and current == h:
        return []
    return steps


def _compact_labels(labels: list[str]) -> str:
    """Join version labels, keeping the first occurrence of each."""
    seen: list[str] = []
    for label in labels:
        if label and label not in seen:
            seen.append(label)
    return ", ".join(seen)


def _hash_reason(start_hash: str, steps: list[ChangeEntry]) -> str:
    if any(entry.role == "user" for entry in steps):
        target = steps[-1].to_hash or start_hash
        return f"user patch: {start_hash} -> {target}"
    hashes = [start_hash]
    for entry in steps:
        if entry.to_hash:
            hashes.append(entry.to_hash)
    labels = _compact_labels([entry.version_label for entry in steps])
    return f"changelog {labels}: {' -> '.join(hashes)}"


def _index_reason(start: int, steps: list[ChangeEntry], kind: str = "indexes") -> str:
    first = _entry_index_arrays(steps[0], kind)
    arrays: list[list[int]] = [first[0], first[1]] if first is not None else []
    for entry in steps[1:]:
        pair = _entry_index_arrays(entry, kind)
        if pair is not None:
            arrays.append(pair[1])
    position = arrays[0].index(start)
    labels = _compact_labels([entry.version_label for entry in steps])
    joined = " -> ".join(str(array) for array in arrays)
    return f"changelog {labels}: object_indexes {joined} (position {position})"


def _entry_index_arrays(
    entry: ChangeEntry, kind: str
) -> tuple[list[int], list[int]] | None:
    """The (from, to) index arrays one index-mapping step uses, or None.

    kind "counts" prefers the object_index_counts arrays when both are present, else
    falls back to object_indexes; kind "indexes" always uses object_indexes."""
    if (
        kind == "counts"
        and entry.from_index_counts is not None
        and entry.to_index_counts is not None
    ):
        return entry.from_index_counts, entry.to_index_counts
    if entry.from_indexes is not None and entry.to_indexes is not None:
        return entry.from_indexes, entry.to_indexes
    return None


def walk_index_value(
    start: int,
    start_hash: str,
    data: FixerData,
    kind: str = "indexes",
) -> tuple[int, list[ChangeEntry]]:
    """Map an integer through successive object_indexes array changes.

    Follows the IB hash's remap arrays ascending by version; the value's first
    From-array position selects the To-array replacement (stops when absent)."""
    if kind not in ("indexes", "counts"):
        raise ValueError(f"unknown index-array kind: {kind!r}")
    current = start
    cur_ib = start_hash
    last = 0
    steps: list[ChangeEntry] = []
    while True:
        bucket = data.ib_index_changes.get(cur_ib)
        if not bucket:
            break
        candidates: list[tuple[ChangeEntry, tuple[list[int], list[int]]]] = []
        for entry in bucket:
            if entry.version_index <= last:
                continue
            pair = _entry_index_arrays(entry, kind)
            if pair is None:
                continue
            candidates.append((entry, pair))
        if not candidates:
            break
        min_index = min(entry.version_index for entry, _ in candidates)
        entry, pair = next((e, p) for e, p in candidates if e.version_index == min_index)
        from_indexes, to_indexes = pair
        if current not in from_indexes:
            break
        pos = from_indexes.index(current)
        if pos >= len(to_indexes):
            break
        current = to_indexes[pos]
        steps.append(entry)
        last = entry.version_index
        cur_ib = entry.to_hash or cur_ib
    return current, steps


def _ships_custom_binaries(text: str) -> bool:
    """True when the ini text declares a Resource filename ending in .ib or .buf."""
    return bool(_CUSTOM_BINARY_RE.search(text))


def _is_mesh_ib_rename(
    new: str, steps: list[ChangeEntry], data: FixerData
) -> bool:
    """True when a hash rename renames a mesh IB whose binaries index vertices.

    Table-driven: the character table's role-"ib" HashRef on the resolved-to hash decides;
    chain steps with role "ib" are the fallback."""
    if any(entry.role == "ib" for entry in steps):
        return True
    return any(ref.role == "ib" for ref in data.db.reverse.get(new, []))


def _is_face_texcoord_rename(old: str, new: str, data: FixerData) -> bool:
    """True when a hash rename concerns a face texcoord buffer."""
    return old in data.face_texcoord_hashes or new in data.face_texcoord_hashes


def _hash_untracked(h: str, data: FixerData) -> bool:
    """True when no dataset entry explains h (no chain, no table use, no target)."""
    if h in data.chains or h in data.db.reverse or h in data.user_patches:
        return False
    return not any(entry.to_hash == h for entry in data.entries)


def _face_override_characters(text: str, data: FixerData) -> list[str]:
    """Characters owning a face IB hash among this ini's TextureOverride hashes."""
    found: list[str] = []
    for hash_value, _hint in _iter_texture_override_hash_lines(text):
        for ref in data.db.reverse.get(hash_value, ()):
            if (
                ref.role == "ib"
                and is_face_component(ref.component)
                and ref.character not in found
            ):
                found.append(ref.character)
    return found


def _current_face_texcoord(data: FixerData, characters: Sequence[str]) -> str | None:
    """The character table's current face texcoord hash for the first matching character."""
    for name in characters:
        character = data.db.characters.get(name)
        if character is None:
            continue
        for component in character.components:
            if not is_face_component(component.name):
                continue
            for key in ("texcoord", "texcoord_vb"):
                value = component.fields.get(key)
                if value:
                    return value
    return None


def _face_texcoord_repoint_target(
    h: str, hint: str, text: str, data: FixerData
) -> str | None:
    """Current face texcoord hash an untracked texcoord override should use, or None.

    Fires only when the section hint names a texcoord (and not an eyebrow) and the file also
    overrides a known face IB; hash-named sections have no character token, so the face IB is the character evidence."""
    if not hint or "texcoord" not in hint:
        return None
    if "eyebrow" in hint or "眉" in hint:
        return None
    characters = _face_override_characters(text, data)
    if not characters:
        return None
    target = _current_face_texcoord(data, characters)
    if target is None or target == h:
        return None
    return target


def _scan_text(text: str, data: FixerData, file: str = "") -> list[FixSuggestion]:
    suggestions: list[FixSuggestion] = []
    warned: set[tuple[str, str]] = set()
    section = None
    hint = ""
    context_hash: str | None = None
    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        content, _eol = _split_eol(line)
        section_match = _SECTION_RE.match(content)
        if section_match is not None:
            section = section_match.group("name")
            if section is None:
                section = ""
            hint = _section_hint(section)
            context_hash = None
            continue
        hash_match = _HASH_LINE_RE.match(content)
        if hash_match is not None:
            h = hash_match.group("hash").lower()
            steps = resolve_hash_chain(h, hint, data)
            if steps is None:
                pass
            elif steps:
                new_hash = steps[-1].to_hash or h
                suggestions.append(
                    FixSuggestion(
                        file=file,
                        section=section,
                        line_no=line_no,
                        kind="hash",
                        old=h,
                        new=new_hash,
                        labels=_compact_labels([entry.version_label for entry in steps]),
                        reason=_hash_reason(h, steps),
                    )
                )
                if (
                    (h, new_hash) not in warned
                    and _ships_custom_binaries(text)
                    and _is_mesh_ib_rename(new_hash, steps, data)
                ):
                    warned.add((h, new_hash))
                    suggestions.append(
                        FixSuggestion(
                            file=file,
                            section=section,
                            line_no=line_no,
                            kind=INDEX_WARNING_KIND,
                            old=h,
                            new=h,
                            labels="",
                            reason=(
                                f"IB {h} -> {steps[-1].to_hash or h}: the mod ships custom "
                                ".ib/.buf buffers; a structurally changed mesh needs re-dumped "
                                "binaries - ini fixes alone will not render correctly"
                            ),
                        )
                    )
                if (
                    (h, new_hash) not in warned
                    and _ships_custom_binaries(text)
                    and _is_face_texcoord_rename(h, new_hash, data)
                ):
                    warned.add((h, new_hash))
                    suggestions.append(
                        FixSuggestion(
                            file=file,
                            section=section,
                            line_no=line_no,
                            kind=INDEX_WARNING_KIND,
                            old=h,
                            new=h,
                            labels="",
                            reason=(
                                f"face texcoord {h} -> {new_hash}: the game unpacked the "
                                "face texcoord format; the mod's 36-byte .buf will be "
                                "converted automatically (packed 4xUNORM8 -> 4xfloat32, "
                                "stride 36 -> 48)"
                            ),
                        )
                    )
            elif h not in data.face_texcoord_hashes and _hash_untracked(h, data):
                repoint = _face_texcoord_repoint_target(h, hint, text, data)
                if repoint is not None:
                    suggestions.append(
                        FixSuggestion(
                            file=file,
                            section=section,
                            line_no=line_no,
                            kind="hash",
                            old=h,
                            new=repoint,
                            labels="",
                            reason=(
                                f"face texcoord re-point: {h} is untracked; the character "
                                f"table's current face texcoord is {repoint}"
                            ),
                        )
                    )
            context_hash = h
            continue
        if section is None or context_hash is None:
            continue
        if context_hash not in data.ib_index_changes:
            continue
        first_match = _FIRST_INDEX_RE.match(content)
        count_match = None if first_match else _INDEX_COUNT_RE.match(content)
        index_match = first_match or count_match
        if index_match is None:
            continue
        original = int(index_match.group("value"))
        index_kind = "indexes" if first_match else "counts"
        new_value, steps = walk_index_value(original, context_hash, data, kind=index_kind)
        if new_value != original and steps:
            kind = "match_first_index" if first_match else "match_index_count"
            suggestions.append(
                FixSuggestion(
                    file=file,
                    section=section,
                    line_no=line_no,
                    kind=kind,
                    old=str(original),
                    new=str(new_value),
                    reason=_index_reason(original, steps, kind=index_kind),
                )
            )
    return suggestions


@dataclass
class _Section:
    """One INI section parsed for the structural scan."""

    name: str
    start: int
    end: int
    hashes: list[tuple[int, str]] = field(default_factory=list)
    first_index_line: int | None = None
    has_run: bool = False
    body: list[tuple[int, str]] = field(default_factory=list)


def _parse_sections(text: str) -> list[_Section]:
    """Single pass over the file's lines into per-section scan facts."""
    sections: list[_Section] = []
    current: _Section | None = None
    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        content, _eol = _split_eol(line)
        section_match = _SECTION_RE.match(content)
        if section_match is not None:
            section_name = section_match.group("name") or ""
            fresh_section = _Section(name=section_name, start=line_no, end=line_no)
            sections.append(fresh_section)
            current = fresh_section
            continue
        if current is None:
            continue
        if _BLANK_RE.match(content):
            current.body.append((line_no, content))
            continue
        current.end = line_no
        hash_match = _HASH_LINE_RE.match(content)
        if hash_match is not None:
            current.hashes.append((line_no, hash_match.group("hash").lower()))
        elif current.first_index_line is None and _FIRST_INDEX_RE.match(content):
            current.first_index_line = line_no
        if not current.has_run and _ANY_RUN_LINE_RE.match(content):
            current.has_run = True
        current.body.append((line_no, content))
    return sections


def _section_char(section_name: str, chars: list[str]) -> str:
    """The rule char owning this section name, or "" when none matches."""
    name = _strip_texture_override(section_name)
    for c in chars:
        if name == c or name.startswith(c + "."):
            return c
    return ""


def _owning_chars(structure: StructureData, h: str) -> set[str]:
    """Chars owning any rule (anchor, shared or multiply) for hash h."""
    rules = structure.rules_by_hash.get(h)
    if rules is None:
        return set()
    return {
        rule.char
        for rule in (*rules.anchors, *rules.shared_normalmap, *rules.multiples)
    }


def _section_has_draw_or_bind(sec: _Section) -> bool:
    """Whether any body line through ``end`` issues a draw or binds a resource."""
    for line_no, content in sec.body:
        if line_no > sec.end:
            continue
        statement = content.strip().lower()
        if _DRAW_COMMAND_RE.match(statement) or _RESOURCE_BIND_RE.match(statement):
            return True
    return False


def _scan_structural(
    text: str, structure: StructureData, file: str = ""
) -> list[FixSuggestion]:
    """Structural suggestions for decoded ini text (parses sections once)."""
    return _scan_structural_sections(_parse_sections(text), structure, file=file)


def _scan_structural_sections(
    sections: list[_Section], structure: StructureData, file: str = ""
) -> list[FixSuggestion]:
    """Structural suggestions on top of the line-level scan: insert_run (an IB
    section with a draw or resource bind but no run command line; gate-only
    bodies are skipped), add_section (a present VB or texture hash requiring
    its anchor IB) and multiply_section (missing sibling)."""
    chars = structure.sorted_chars
    file_hashes = {h for section in sections for _line_no, h in section.hashes}
    suggestions: list[FixSuggestion] = []
    seen: set[tuple] = set()
    unindexed: dict[str, tuple[int, str]] = {}
    indexed_done: set[str] = set()
    for sec in sections:
        for line_no, h in sec.hashes:
            rules = structure.rules_by_hash.get(h)
            if rules is None:
                continue
            if rules.is_ib:
                if sec.first_index_line is not None:
                    indexed_done.add(h)
                    if not sec.has_run and _section_has_draw_or_bind(sec):
                        key = ("insert_run", sec.name, sec.first_index_line)
                        if key not in seen:
                            seen.add(key)
                            suggestions.append(
                                FixSuggestion(
                                    file=file,
                                    section=sec.name,
                                    line_no=sec.first_index_line,
                                    kind="insert_run",
                                    old=h,
                                    new=h,
                                    after_line=sec.first_index_line,
                                    insert_text="run = CommandListSkinTexture",
                                    reason=f"IB {h}: add run = CommandListSkinTexture",
                                )
                            )
                elif not sec.has_run and _section_has_draw_or_bind(sec):
                    unindexed.setdefault(h, (line_no, sec.name))
            sec_char = _section_char(sec.name, chars)
            if not sec_char:
                owners = _owning_chars(structure, h)
                if len(owners) == 1:
                    sec_char = next(iter(owners))
                elif owners:
                    lowered = _strip_texture_override(sec.name).lower()
                    contained = [c for c in owners if c.lower() in lowered]
                    if contained:
                        sec_char = max(contained, key=len)
            if not sec_char:
                continue
            for rule in rules.anchors:
                if rule.char != sec_char:
                    continue
                if structure.cached_gates[rule] & file_hashes:
                    continue
                key = ("add_section", rule.section_title, rule.ib)
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(
                    FixSuggestion(
                        file=file,
                        section=sec.name,
                        line_no=sec.end,
                        kind="add_section",
                        old=h,
                        new=rule.ib,
                        after_line=sec.end,
                        insert_text=f"\n[TextureOverride{rule.section_title}]\nhash = {rule.ib}\nmatch_priority = 0",
                        reason=f"anchor: add [TextureOverride{rule.section_title}] (hash = {rule.ib})",
                    )
                )
            for rule in rules.shared_normalmap:
                if rule.char != sec_char:
                    continue
                if structure.cached_gates[rule] & file_hashes:
                    continue
                key = ("add_section", rule.section_title, rule.ibs[0])
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(
                    FixSuggestion(
                        file=file,
                        section=sec.name,
                        line_no=sec.end,
                        kind="add_section",
                        old=h,
                        new=rule.ibs[0],
                        after_line=sec.end,
                        insert_text=f"\n[TextureOverride{rule.section_title}]\nhash = {rule.ibs[0]}\nmatch_priority = 0",
                        reason=f"shared normalmap: add [TextureOverride{rule.section_title}] (hash = {rule.ibs[0]})",
                    )
                )
            for rule in rules.multiples:
                if rule.char != sec_char:
                    continue
                if structure.cached_gates[rule] & file_hashes:
                    continue
                key = ("multiply_section", rule.section_title, rule.counterpart)
                if key in seen:
                    continue
                seen.add(key)
                critical = [
                    content
                    for _body_line, content in sec.body
                    if not (
                        _SECTION_RE.match(content)
                        or _HASH_LINE_RE.match(content)
                        or _FIRST_INDEX_RE.match(content)
                        or _INDEX_COUNT_RE.match(content)
                    )
                ]
                block = [
                    f"[TextureOverride{rule.section_title}]",
                    f"hash = {rule.counterpart}",
                    *critical,
                ]
                suggestions.append(
                    FixSuggestion(
                        file=file,
                        section=sec.name,
                        line_no=sec.end,
                        kind="multiply_section",
                        old=h,
                        new=rule.counterpart,
                        after_line=sec.end,
                        insert_text="\n" + "\n".join(block),
                        reason=f"multiply: add [TextureOverride{rule.section_title}] (hash = {rule.counterpart})",
                    )
                )
    for h, (line_no, sec_name) in unindexed.items():
        if h in indexed_done:
            continue
        key = ("insert_run", sec_name, line_no)
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(
            FixSuggestion(
                file=file,
                section=sec_name,
                line_no=line_no,
                kind="insert_run",
                old=h,
                new=h,
                after_line=line_no,
                insert_text="run = CommandListSkinTexture",
                reason=f"IB {h}: add run = CommandListSkinTexture",
            )
        )
    return suggestions


def delete_section_suggestion(
    sec: _Section, file: str = "", reason: str = ""
) -> FixSuggestion:
    """A delete_section suggestion removing one parsed section's header through its last body line.

    ``old`` snapshots the body lines through ``sec.end`` so apply_plan can re-validate the section
    at apply time; the header is validated by name and blank lines after ``end`` stay in the file."""
    return FixSuggestion(
        file=file,
        section=sec.name,
        line_no=sec.start,
        kind=SECTION_DELETE_KIND,
        old="\n".join(content for line_no, content in sec.body if line_no <= sec.end),
        new="",
        after_line=sec.end,
        reason=reason or f"delete section [{sec.name}]",
    )


def _hair_stem_name(name: str) -> str:
    """Lowercased section name with a leading TextureOverride prefix removed."""
    return _strip_texture_override(name).strip().lower()


def _unique_hair_list_name(base: str, taken: set[str], suffix: str = "Hair") -> str:
    """Case-insensitively unique CommandList name derived from a mod identity string.

    ``base`` tokens (non-alphanumeric runs, first char capitalized) join with ``suffix``; a candidate colliding with
    ``taken`` — the scope's lowered section names, compared bare and ``CommandList.``-prefixed — gets a numeric suffix, which the caller adds to ``taken``."""
    stem = "".join(
        token[:1].upper() + token[1:]
        for token in _HAIR_TOKEN_SPLIT_RE.split(base)
        if token
    )
    candidate = stem + suffix
    number = 1
    while (
        candidate.lower() in taken
        or _HAIR_COMMANDLIST_PREFIX + candidate.lower() in taken
    ):
        number += 1
        candidate = f"{stem}{suffix}{number}"
    return candidate


def _hair_base(path: Path, scope_base: str) -> str:
    """Mod identity for hair naming: passed-down scope folder name, else parent folder, else stem."""
    return scope_base or path.parent.name or path.stem


def _scope_section_names(paths: Iterable[Path]) -> set[str]:
    """Lowercased section names across the scope's decodable ini files (hair-name pool)."""
    taken: set[str] = set()
    for path in paths:
        try:
            text, _encoding = _read_ini_details(path)
        except (ValueError, OSError):
            continue
        taken.update(sec.name.strip().lower() for sec in _parse_sections(text))
    return taken


def _hair_exclusion_domain(
    sections: list[_Section], stem: str, dispatch: _Section, claim: _Section
) -> list[_Section]:
    """Sections scanned for the DRAW_TYPE == 2|4 hair rewrite exclusion.

    A separator-bearing claim stem clusters its prefix-matched siblings; a generic
    (separator-less) stem clusters the whole file; dispatch and claim always join."""
    if not stem or not any(ch in stem for ch in _HAIR_STEM_SEPARATORS):
        return list(sections)
    cluster = [sec for sec in sections if _hair_stem_name(sec.name).startswith(stem)]
    cluster.extend((dispatch, claim))
    return cluster


def _scan_hair_rewrite(
    sections: list[_Section],
    file: str = "",
    taken: set[str] | None = None,
    base: str = "",
) -> list[FixSuggestion]:
    """Guide-pattern hair rewrite for one file holding exactly one dispatch + one claim section (v1).

    The dispatch's ``checktextureoverride = ib`` line becomes a run into a draw-gated CommandList pair from the claim body (hash/match/handling dropped); claim + stem-sharing texcoord section deleted.
    A ``DRAW_TYPE == 2|4`` line anywhere in the claim's name-stem cluster (whole file when the stem has no separator) blocks the rewrite; cross-file pairs emit nothing."""
    if taken is None:
        taken = {sec.name.strip().lower() for sec in sections}
    dispatches: list[tuple[_Section, int, str]] = []
    claims: list[tuple[_Section, int, int]] = []
    for sec in sections:
        if not sec.name.strip().lower().startswith(_TEXTUREOVERRIDE_PREFIX):
            continue
        dispatch: tuple[int, str] | None = None
        first_index: int | None = None
        index_count: int | None = None
        handling = False
        for line_no, content in sec.body:
            if line_no > sec.end:
                continue
            if dispatch is None and _HAIR_DISPATCH_RE.match(content):
                dispatch = (line_no, content)
            if first_index is None and _FIRST_INDEX_RE.match(content):
                first_index = int(_FIRST_INDEX_RE.match(content).group("value"))
            if index_count is None and _INDEX_COUNT_RE.match(content):
                index_count = int(_INDEX_COUNT_RE.match(content).group("value"))
            if _HAIR_HANDLING_RE.match(content):
                handling = True
        if dispatch is not None:
            dispatches.append((sec, dispatch[0], dispatch[1]))
        elif sec.hashes and handling and first_index is not None and index_count is not None:
            claims.append((sec, first_index, index_count))
    if len(dispatches) != 1 or len(claims) != 1:
        return []
    dispatch_sec, dispatch_line_no, dispatch_line = dispatches[0]
    claim, gate_first, gate_count = claims[0]
    stem = _HAIR_CLAIM_MARKER_RE.sub("", _hair_stem_name(claim.name))
    stem = stem.rstrip(_HAIR_STEM_SEPARATORS)
    if any(
        _HAIR_DRAW_TYPE_EXCLUDE_RE.search(content)
        for sec in _hair_exclusion_domain(sections, stem, dispatch_sec, claim)
        for _line_no, content in sec.body
        if _line_no <= sec.end
    ):
        return []
    texcoord = None
    if stem:
        for sec in sections:
            if sec is claim or sec is dispatch_sec:
                continue
            if not sec.name.strip().lower().startswith(_TEXTUREOVERRIDE_PREFIX):
                continue
            stem_name = _hair_stem_name(sec.name)
            if not stem_name.startswith(stem):
                continue
            tail = stem_name[len(stem):].lstrip(_HAIR_STEM_SEPARATORS)
            if tail.startswith("texcoord"):
                texcoord = sec
                break
    reason = (
        f"convert dynamic hair dispatch to draw-gated claim (hash {claim.hashes[0][1]})"
    )
    hair_name = _unique_hair_list_name(base, taken)
    taken.add(hair_name.lower())
    run_name = _unique_hair_list_name(base, taken, suffix="HairA")
    taken.add(run_name.lower())
    body = [
        content
        for line_no, content in claim.body
        if line_no <= claim.end
        and not _HASH_LINE_RE.match(content)
        and not _FIRST_INDEX_RE.match(content)
        and not _INDEX_COUNT_RE.match(content)
        and not _HAIR_HANDLING_RE.match(content)
    ]
    insert_text = "\n".join(
        [
            f"[CommandList.{hair_name}]",
            f"if first_index == {gate_first} && index_count == {gate_count}",
            f"    run = CommandList.{run_name}",
            "endif",
            "",
            f"[CommandList.{run_name}]",
            *body,
        ]
    )
    indent = dispatch_line[: len(dispatch_line) - len(dispatch_line.lstrip())]
    suggestions = [
        FixSuggestion(
            file=file,
            section=dispatch_sec.name,
            line_no=dispatch_line_no,
            kind=HAIR_DISPATCH_KIND,
            old=dispatch_line,
            new=f"{indent}run = CommandList.{hair_name}",
            reason=reason,
        ),
        FixSuggestion(
            file=file,
            section=claim.name,
            line_no=claim.end,
            kind=HAIR_COMMANDLISTS_KIND,
            old=claim.hashes[0][1],
            new=hair_name,
            after_line=claim.end,
            insert_text=insert_text,
            reason=reason,
        ),
        delete_section_suggestion(claim, file=file, reason=reason),
    ]
    if texcoord is not None:
        suggestions.append(delete_section_suggestion(texcoord, file=file, reason=reason))
    return suggestions


@dataclass
class _IndexScan:
    """One IB section's index line plus its chain-resolved hash context."""

    section: _Section
    anchor: str
    current: str
    renamed: bool
    value: int
    value_line: int


def _first_index_value(sec: _Section) -> tuple[int, int] | None:
    """The (value, line_no) of the first match_first_index line in the section."""
    for line_no, content in sec.body:
        match = _FIRST_INDEX_RE.match(content)
        if match is not None:
            return int(match.group("value")), line_no
    return None


def _classification_pairing(
    scans: list[_IndexScan], classifications: list[str]
) -> list[tuple[int, int]] | None:
    """Pair scans to classification letters 1:1 by section-name suffix."""
    if not scans or not classifications or len(scans) != len(classifications):
        return None
    by_suffix: dict[str, list[int]] = {}
    for index, scan in enumerate(scans):
        name = _strip_texture_override(scan.section.name)
        if not name:
            return None
        by_suffix.setdefault(name[-1], []).append(index)
    pairing: list[tuple[int, int]] = []
    used: set[int] = set()
    for position, letter in enumerate(classifications):
        matches = by_suffix.get(letter, [])
        if len(matches) != 1:
            return None
        scan_index = matches[0]
        if scan_index in used:
            return None
        used.add(scan_index)
        pairing.append((scan_index, position))
    return pairing


def _candidate_pairing(
    scans: list[_IndexScan], component: Component
) -> list[tuple[int, int, int]] | None:
    """(scan_index, position, new_value) triples pairing scans to object_indexes."""
    indexes = component.object_indexes
    if indexes is None:
        return None
    if component.object_classifications:
        pairing = _classification_pairing(scans, component.object_classifications)
        if pairing is None:
            return None
        if any(position >= len(indexes) for _scan_index, position in pairing):
            return None
        return [
            (scan_index, position, indexes[position])
            for scan_index, position in pairing
        ]
    if len(scans) != len(indexes):
        return None
    return [
        (scan_index, position, indexes[position])
        for position, scan_index in enumerate(range(len(scans)))
    ]


def _ib_candidates(
    data: FixerData, current: str
) -> list[tuple[HashRef, Component]]:
    """(HashRef, Component) usages of current with usable object_indexes."""
    candidates: list[tuple[HashRef, Component]] = []
    for ref in data.db.reverse.get(current, []):
        if ref.role != "ib":
            continue
        character = data.db.characters.get(ref.character)
        if character is None:
            continue
        for component in character.components:
            if component.name == ref.component and component.object_indexes is not None:
                candidates.append((ref, component))
    return candidates


def _scan_index_remaps(
    sections: list[_Section], data: FixerData, file: str = ""
) -> list[FixSuggestion]:
    """match_first_index suggestions and index warnings for IB sections.

    Sections group by chain-resolved current hash; suggestions fire when every
    character-table candidate pairing agrees, warnings replace them otherwise."""
    groups: dict[str, list[_IndexScan]] = {}
    for sec in sections:
        if not sec.hashes:
            continue
        anchor = sec.hashes[0][1]
        steps = resolve_hash_chain(anchor, _section_hint(sec.name), data)
        if steps is None:
            continue
        found = _first_index_value(sec)
        if found is None:
            continue
        value, value_line = found
        target = (steps[-1].to_hash or anchor) if steps else anchor
        groups.setdefault(target, []).append(
            _IndexScan(
                section=sec,
                anchor=anchor,
                current=target,
                renamed=bool(steps),
                value=value,
                value_line=value_line,
            )
        )
    suggestions: list[FixSuggestion] = []
    for current, scans in groups.items():
        refs_nonempty = bool(data.db.reverse.get(current))
        candidates = _ib_candidates(data, current)
        pairings = [_candidate_pairing(scans, component) for _ref, component in candidates]
        usable = [pairing for pairing in pairings if pairing is not None]
        ambiguous = any(pairing is None for pairing in pairings) or (
            len({tuple(pairing) for pairing in usable}) > 1
        )
        if candidates and not ambiguous:
            first_ref, first_component = candidates[0]
            for scan_index, position, new_value in usable[0]:
                scan = scans[scan_index]
                if scan.value == new_value:
                    continue
                suggestions.append(
                    FixSuggestion(
                        file=file,
                        section=scan.section.name,
                        line_no=scan.value_line,
                        kind="match_first_index",
                        old=str(scan.value),
                        new=str(new_value),
                        labels="",
                        reason=(
                            f"character table {first_ref.character} {first_component.name}: "
                            f"object_indexes {first_component.object_indexes or []} (position {position})"
                        ),
                    )
                )
        elif refs_nonempty and any(scan.renamed for scan in scans):
            for scan in scans:
                suggestions.append(
                    FixSuggestion(
                        file=file,
                        section=scan.section.name,
                        line_no=scan.value_line,
                        kind=INDEX_WARNING_KIND,
                        old=str(scan.value),
                        new=str(scan.value),
                        labels="",
                        reason=(
                            f"IB {scan.anchor} -> {current}: match_first_index = {scan.value} "
                            "not verified — no usable object index data in the character table"
                        ),
                    )
                )
    return suggestions


def _read_ini_details(path: Path) -> tuple[str, str]:
    """Read one .ini file and return (text, encoding name for writing back)."""
    return _decode_ini(path.read_bytes())


_RABBITFX_ROOT = "Resource\\RabbitFX\\"
_RABBITFX_SLOT_KEYS = {"ps-t17": "GlowMap", "ps-t18": "FXMap"}
_RABBITFX_CANONICAL_KEYS = {
    "glowmap": "GlowMap",
    "fxmap": "FXMap",
    "diffuse": "Diffuse",
    "lightmap": "Lightmap",
    "materialmap": "Materialmap",
    "stockingmap": "Stockingmap",
}
_RABBITFX_SLOT_RE = re.compile(
    r"^\s*(?P<slot>ps-t1[78])\s*=\s*(?P<value>\S.*?)\s*$", re.IGNORECASE
)
_RABBITFX_NAMED_RE = re.compile(
    r"^\s*Resource\\RabbitFX\\"
    r"(?P<key>GlowMap|FXMap|Diffuse|Lightmap|Materialmap|Stockingmap)"
    r"\s*=\s*(?P<value>\S.*?)\s*$",
    re.IGNORECASE,
)


def _rabbitfx_indent(content: str) -> str:
    """Leading whitespace of one ini line, verbatim."""
    return content[: len(content) - len(content.lstrip())]


def _rabbitfx_named_keys(text: str) -> set[str]:
    """Lowercased canonical keys of every RabbitFX named-bind line in the file.

    Collected regardless of ref or null state: each such line already occupies its
    named key, so slot conversions must not duplicate it."""
    keys: set[str] = set()
    for line in text.splitlines(keepends=True):
        match = _RABBITFX_NAMED_RE.match(_split_eol(line)[0])
        if match is not None:
            keys.add(match.group("key").lower())
    return keys


def _rabbitfx_slot_fix(
    content: str, claimed: set[str], file: str, section: str, line_no: int
) -> FixSuggestion | None:
    """Slot-to-named bind fix for a ps-t17/ps-t18 line, or None when not applicable.

    The value is kept verbatim; an already-declared named key (dedup guard) or an
    explicit ``= null`` unbind is left alone."""
    match = _RABBITFX_SLOT_RE.match(content)
    if match is None:
        return None
    slot = match.group("slot").lower()
    value = match.group("value")
    key = _RABBITFX_SLOT_KEYS[slot]
    if key.lower() in claimed or value.lower() == "null":
        return None
    return FixSuggestion(
        file=file,
        section=section,
        line_no=line_no,
        kind=RABBITFX_KIND,
        old=content,
        new=f"{_rabbitfx_indent(content)}{_RABBITFX_ROOT}{key} = ref {value}",
        labels="",
        reason=f"migrate RabbitFX {slot} slot bind to named {_RABBITFX_ROOT}{key}",
    )


def _rabbitfx_named_fix(
    content: str, file: str, section: str, line_no: int
) -> FixSuggestion | None:
    """Missing-``ref`` fix for a RabbitFX named-key bind line, or None.

    Path and key casing canonicalize; ``= null`` unbinds, values already carrying
    ``ref`` (including ``ref ref`` junk) and custom keys are untouched."""
    match = _RABBITFX_NAMED_RE.match(content)
    if match is None:
        return None
    value = match.group("value")
    if value.lower() == "null" or value.lower().startswith("ref"):
        return None
    key = _RABBITFX_CANONICAL_KEYS[match.group("key").lower()]
    return FixSuggestion(
        file=file,
        section=section,
        line_no=line_no,
        kind=RABBITFX_KIND,
        old=content,
        new=f"{_rabbitfx_indent(content)}{_RABBITFX_ROOT}{key} = ref {value}",
        labels="",
        reason=f"add missing ref to RabbitFX named bind {_RABBITFX_ROOT}{key}",
    )


def _scan_rabbitfx(text: str, file: str = "") -> list[FixSuggestion]:
    """RabbitFX-format line fixes for decoded ini text.

    Migrates legacy ``ps-t17``/``ps-t18`` slot binds and missing-``ref`` named binds to canonical
    ``Resource\\RabbitFX\\...`` binds; [] when no ``rabbitfx`` token occurs anywhere (protects non-RabbitFX mods)."""
    if "rabbitfx" not in text.lower():
        return []
    claimed = _rabbitfx_named_keys(text)
    suggestions: list[FixSuggestion] = []
    section = ""
    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        content, _eol = _split_eol(line)
        section_match = _SECTION_RE.match(content)
        if section_match is not None:
            section = section_match.group("name")
            continue
        fix = _rabbitfx_slot_fix(content, claimed, file, section, line_no)
        if fix is None:
            fix = _rabbitfx_named_fix(content, file, section, line_no)
        if fix is not None:
            suggestions.append(fix)
    return suggestions


def _scan_file_details(
    path: Path,
    data: FixerData,
    structure: StructureData | None = None,
    taken: set[str] | None = None,
    base: str = "",
) -> tuple[str, str, list[FixSuggestion]]:
    """Read one .ini file and return (text, encoding, suggestions).

    With a StructureData, structural and hair-rewrite suggestions are appended after the line-level ones;
    ``taken`` (scope section names) and ``base`` (mod identity) feed the hair rewrite's unique CommandList naming and default to this file's own sections."""
    text, encoding = _read_ini_details(path)
    sections = _parse_sections(text)
    suggestions = _scan_text(text, data, file=str(path))
    covered = {
        suggestion.line_no
        for suggestion in suggestions
        if suggestion.kind in ("match_first_index", "match_index_count")
    }
    suggestions.extend(
        suggestion
        for suggestion in _scan_index_remaps(sections, data, file=str(path))
        if suggestion.line_no not in covered
    )
    suggestions.extend(_scan_rabbitfx(text, file=str(path)))
    if structure is not None:
        suggestions.extend(
            _scan_structural_sections(sections, structure, file=str(path))
        )
        suggestions.extend(
            _scan_hair_rewrite(sections, file=str(path), taken=taken, base=base)
        )
    return text, encoding, suggestions


def _scan_file(
    path: Path,
    data: FixerData,
    structure: StructureData | None = None,
    taken: set[str] | None = None,
    base: str = "",
) -> list[FixSuggestion]:
    _text, _encoding, suggestions = _scan_file_details(
        path, data, structure, taken, base
    )
    return suggestions


def _iter_texture_override_hash_lines(text: str) -> Iterator[tuple[str, str]]:
    """Yield (lowercased hash, normalized section hint) from TextureOverride sections in decoded ini text."""
    section = ""
    in_texture_override = False
    for line in text.splitlines(keepends=True):
        content, _eol = _split_eol(line)
        section_match = _SECTION_RE.match(content)
        if section_match is not None:
            section = section_match.group("name")
            in_texture_override = section.strip().lower().startswith(
                _TEXTUREOVERRIDE_PREFIX
            )
            continue
        if not in_texture_override:
            continue
        hash_match = _HASH_LINE_RE.match(content)
        if hash_match is not None:
            yield hash_match.group("hash").lower(), _section_hint(section)


def read_ini_text(path) -> str:
    """Decoded ini text for a mod file (same decode rules as the scanner)."""
    return _decode_ini(Path(path).read_bytes())[0]


def ini_hints(text: str) -> dict[str, set[str]]:
    """Texture-override hints (hash -> section hints) from decoded ini text."""
    hints: dict[str, set[str]] = {}
    for hash_value, hint in _iter_texture_override_hash_lines(text):
        hints.setdefault(hash_value, set()).add(hint)
    return hints


def parse_ini_facts(text: str) -> tuple[dict[str, set[str]], list[_Section]]:
    """Texture-override hints and parsed sections from one parse pass over text.

    ``hints`` matches ``ini_hints`` exactly; ``sections`` feeds
    ``structural_fix_count_sections`` for structural counting without a reparse."""
    sections = _parse_sections(text)
    hints: dict[str, set[str]] = {}
    for section in sections:
        if not section.name.strip().lower().startswith(_TEXTUREOVERRIDE_PREFIX):
            continue
        for _line_no, hash_value in section.hashes:
            hints.setdefault(hash_value, set()).add(_section_hint(section.name))
    return hints, sections


_INI_PARSE_MEMO_CAP = 4096
_INI_PARSE_MEMO: OrderedDict[tuple[str, int, int], tuple[str, dict, list]] = OrderedDict()


def cached_ini_parse(path: Path) -> tuple[str, dict, list] | None:
    """(text, hints, sections) for a .ini file, memoized across analysis runs.

    Keyed by (path, st_mtime_ns, st_size) in an LRU OrderedDict capped at ``_INI_PARSE_MEMO_CAP`` (hits move to the end, oldest evicted past the cap); os.stat OSError and read OSError (a directory path stats fine but read fails) return None; undecodable bytes raise ValueError — failures are never memoized; no locking (only one TaskWorker runs at a time).
    scan_folder/scan_files/_scan_file_details stay unwired on purpose: apply_plan writes back via the raw ``encoding`` only _scan_file_details decodes, and its ``write_bytes`` bumps the mtime (a natural memo miss anyway)."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    key = (os.path.abspath(str(path)), stat.st_mtime_ns, stat.st_size)
    cached = _INI_PARSE_MEMO.get(key)
    if cached is not None:
        _INI_PARSE_MEMO.move_to_end(key)
        return cached
    try:
        text = read_ini_text(path)
    except OSError:
        return None
    hints, sections = parse_ini_facts(text)
    _INI_PARSE_MEMO[key] = (text, hints, sections)
    while len(_INI_PARSE_MEMO) > _INI_PARSE_MEMO_CAP:
        _INI_PARSE_MEMO.popitem(last=False)
    return text, hints, sections


def collect_texture_override_hashes(path: Path) -> list[str]:
    """Unique lowercased hash values from `hash =` lines inside [TextureOverride…] sections.

    Non-TextureOverride sections are ignored, so shader and stray hashes are excluded; hashes are
    sorted ascending, [] for unreadable or undecodable files."""
    try:
        text = read_ini_text(path)
    except (ValueError, OSError):
        return []
    return sorted({h for h, _hint in _iter_texture_override_hash_lines(text)})


def collect_texture_override_hints(path: Path) -> dict[str, set[str]]:
    """Map each TextureOverride hash to the normalized section hints it appears under.

    Same sections and ``hash =`` lines as collect_texture_override_hashes (one hint per section);
    {} for unreadable or undecodable files."""
    try:
        return ini_hints(read_ini_text(path))
    except (ValueError, OSError):
        return {}


def _deference_suggestions(
    mods_dir: Path,
    wraps: Sequence | None = None,
    unifies: Sequence | None = None,
) -> dict[str, list[FixSuggestion]]:
    """Cross-mod deference edits per file path: defer wraps and vertex-count unifies.

    The fixes are relational (both conflicting mods must be patched in one scope), so only scan_folder calls this; per-mod scan_files emits none. Pre-computed specs skip the claims pass.
    Anchors use the claims parse's original line numbers (open at the section header, close at its last body line)."""
    per_file: dict[str, list[FixSuggestion]] = {}
    if wraps is None or unifies is None:
        from .claims import deference_specs  # local import: claims imports this module

        spec_wraps, spec_unifies, _rows = deference_specs(str(mods_dir))
        wraps = spec_wraps if wraps is None else wraps
        unifies = spec_unifies if unifies is None else unifies
    wraps = sorted(
        wraps, key=lambda spec: (str(spec.file), spec.section, spec.start, spec.flag)
    )
    unifies = sorted(unifies, key=lambda spec: (str(spec.file), spec.line_no))
    for spec in wraps:
        guard = f"if ${spec.flag} == 0"
        reason = (
            f"defer {spec.mod}'s [{spec.section}] claims to the mod owning "
            f"${spec.flag}"
        )
        edits = per_file.setdefault(str(spec.file), [])
        for after_line, insert_text in ((spec.start, guard), (spec.end, "endif")):
            edits.append(
                FixSuggestion(
                    file=str(spec.file),
                    section=spec.section,
                    line_no=after_line,
                    kind=DEFER_WRAP_KIND,
                    old="",
                    new=insert_text,
                    reason=reason,
                    after_line=after_line,
                    insert_text=insert_text,
                )
            )
    for spec in unifies:
        limit = spec.new.rsplit("=", 1)[-1].strip()
        per_file.setdefault(str(spec.file), []).append(
            FixSuggestion(
                file=str(spec.file),
                section=spec.section,
                line_no=spec.line_no,
                kind=DEFER_UNIFY_KIND,
                old=spec.old,
                new=spec.new,
                reason=(
                    f"unify override_vertex_count to {limit} across the mods "
                    "claiming this hash"
                ),
            )
        )
    return per_file


def scan_folder(
    mods_dir: Path, data: FixerData, structure: StructureData | None = None
) -> list[FilePlan]:
    """Scan every .ini under mods_dir recursively and return non-empty plans.

    Skips files or path components named DISABLED_versionfix_/DISABLED_BACKUP_ (genuine DISABLED-named files still scan); a StructureData appends structural, hair-rewrite and cross-mod
    deference suggestions — relational (both conflicting mods must be patched in one folder scope), so per-mod scan_files emits none — and names hair CommandLists after mods_dir (unique across the scope)."""
    mods_dir = Path(mods_dir)
    paths = list(included_ini_files(mods_dir))
    taken = _scope_section_names(paths) if structure is not None else None
    deference = _deference_suggestions(mods_dir) if structure is not None else {}
    plans: list[FilePlan] = []
    for path in paths:
        try:
            suggestions = _scan_file(
                path, data, structure, taken, _hair_base(path, mods_dir.name)
            )
        except (ValueError, OSError):
            continue
        extra = deference.get(str(path))
        if extra:
            suggestions = [*suggestions, *extra]
        if suggestions:
            plans.append(FilePlan(path=str(path), suggestions=suggestions))
    return plans


def scan_files(
    paths: Sequence[Path], data: FixerData, structure: StructureData | None = None
) -> list[FilePlan]:
    """Scan explicit .ini file paths (no directory recursion, no gating).

    Skips undecodable or unreadable files and directories; a StructureData appends structural and hair-rewrite suggestions,
    naming hair CommandLists after each file's parent folder (unique across the scope's files)."""
    checked: list[Path] = []
    for path in paths:
        path = Path(path)
        if not path.is_dir():
            checked.append(path)
    taken = _scope_section_names(checked) if structure is not None else None
    plans: list[FilePlan] = []
    for path in checked:
        try:
            suggestions = _scan_file(path, data, structure, taken, _hair_base(path, ""))
        except (ValueError, OSError):
            continue
        if suggestions:
            plans.append(FilePlan(path=str(path), suggestions=suggestions))
    return plans


def structural_fix_count(text: str, structure: StructureData, file: str) -> int:
    """Number of pending structural suggestions in decoded ini text."""
    return sum(
        1
        for suggestion in _scan_structural(text, structure, file=file)
        if suggestion.kind in STRUCTURAL_KINDS
    )


def structural_fix_count_sections(
    sections: list[_Section], structure: StructureData, file: str
) -> int:
    """Number of pending structural suggestions for already-parsed ini sections."""
    return sum(
        1
        for suggestion in _scan_structural_sections(sections, structure, file=file)
        if suggestion.kind in STRUCTURAL_KINDS
    )


def _replace_on_line(content: str, suggestion: FixSuggestion) -> str | None:
    """Apply one suggestion to a line's content, or None when it no longer fits."""
    if suggestion.kind == "hash":
        match = _HASH_LINE_RE.match(content)
        if match is None or match.group("hash").lower() != suggestion.old.lower():
            return None
        return content[: match.start("hash")] + suggestion.new + content[match.end("hash"):]
    if suggestion.kind in ("match_first_index", "match_index_count"):
        pattern = (
            _FIRST_INDEX_RE if suggestion.kind == "match_first_index" else _INDEX_COUNT_RE
        )
        match = pattern.match(content)
        if match is None or match.group("value") != suggestion.old:
            return None
        return content[: match.start("value")] + suggestion.new + content[match.end("value"):]
    if suggestion.kind in (HAIR_DISPATCH_KIND, DEFER_UNIFY_KIND, RABBITFX_KIND):
        if content != suggestion.old:
            return None
        return suggestion.new
    return None


def _delete_section_span(
    suggestion: FixSuggestion, lines: list[str]
) -> tuple[int, int] | None:
    """(start, end) original line numbers of a delete_section target, or None when stale.

    The header at ``line_no`` must parse as ``[suggestion.section]`` and the body lines through
    ``after_line`` must still equal the scanned ``old`` snapshot."""
    start, end = suggestion.line_no, suggestion.after_line
    if not 1 <= start <= end <= len(lines):
        return None
    header, _eol = _split_eol(lines[start - 1])
    match = _SECTION_RE.match(header)
    if match is None or match.group("name") != suggestion.section:
        return None
    body = "\n".join(_split_eol(line)[0] for line in lines[start:end])
    if body != suggestion.old:
        return None
    return start, end


def _apply_section_deletes(
    deletes: list[FixSuggestion],
    lines: list[str],
    fixed_lines: list[str],
    orig_index: dict[int, int],
    log: Callable[[str], None],
) -> int:
    """Apply delete_section suggestions last, highest original line first.

    Stale ranges (section moved or content drifted) and overlapping duplicates are logged as
    skipped and left in place; returns the number of deletes applied."""
    applied = 0
    removed: list[tuple[int, int]] = []
    for suggestion in sorted(deletes, key=lambda s: s.line_no, reverse=True):
        span = _delete_section_span(suggestion, lines)
        if span is None:
            log(f"skipped stale delete: {suggestion.reason}")
            continue
        start, end = span
        if any(r_start <= end and start <= r_end for r_start, r_end in removed):
            log(f"skipped duplicate delete: {suggestion.reason}")
            continue
        del fixed_lines[orig_index[start] : orig_index[end] + 1]
        removed.append(span)
        applied += 1
        log(suggestion.reason)
    return applied


def apply_plan(
    plan: FilePlan,
    data: FixerData,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None] = print,
    structure: StructureData | None = None,
    backup: bool = True,
    suggestions: Sequence[FixSuggestion] | None = None,
) -> bool:
    """Apply one file's fixes, backing up the original.  Returns True when written.

    The file is re-scanned first by default (``suggestions`` skips the scan); ``backup=False`` writes
    without a new backup (fixpoint pass 2+); section deletes apply last, highest original line first."""
    path = Path(plan.path)
    if suggestions is None:
        text, encoding, suggestions = _scan_file_details(path, data, structure)
    else:
        text, encoding = _read_ini_details(path)
    for suggestion in suggestions:
        if suggestion.kind in WARNING_KINDS:
            log(f"warning: {suggestion.reason}")
    effective = [
        suggestion
        for suggestion in suggestions
        if suggestion.kind in STRUCTURAL_KINDS
        or suggestion.kind == SECTION_DELETE_KIND
        or suggestion.old.lower() != suggestion.new.lower()
    ]
    if not effective:
        log(f"no changes needed: {path}")
        return False
    lines = text.splitlines(keepends=True)
    end_key = len(lines) + 1
    by_line: dict[int, FixSuggestion] = {}
    insertions: dict[int, list[str]] = {}
    deletes = [
        suggestion for suggestion in effective if suggestion.kind == SECTION_DELETE_KIND
    ]
    for suggestion in effective:
        if suggestion.kind == SECTION_DELETE_KIND:
            continue
        if suggestion.kind in STRUCTURAL_KINDS:
            if 0 < suggestion.after_line < len(lines):
                key = suggestion.after_line + 1
            else:
                key = end_key
            insertions.setdefault(key, []).append(suggestion.insert_text)
        else:
            by_line[suggestion.line_no] = suggestion
    eol = "\n"
    for line in lines:
        if line.endswith("\r\n"):
            eol = "\r\n"
            break
        if line.endswith("\n"):
            break
        if line.endswith("\r"):
            eol = "\r"
            break
    fixed_lines: list[str] = []
    orig_index: dict[int, int] = {}
    for line_no, line in enumerate(lines, start=1):
        for insert_text in insertions.get(line_no, ()):
            for segment in insert_text.split("\n"):
                fixed_lines.append(segment + eol)
        if deletes:
            orig_index[line_no] = len(fixed_lines)
        suggestion = by_line.get(line_no)
        if suggestion is None:
            fixed_lines.append(line)
            continue
        content, line_eol = _split_eol(line)
        replaced = _replace_on_line(content, suggestion)
        if replaced is None:
            raise ValueError(
                f"{path}: line {line_no} does not match suggestion "
                f"{suggestion.kind} = {suggestion.old}"
            )
        fixed_lines.append(replaced + line_eol)
    for insert_text in insertions.get(end_key, ()):
        for segment in insert_text.split("\n"):
            fixed_lines.append(segment + eol)
    applied_deletes = 0
    if deletes:
        applied_deletes = _apply_section_deletes(
            deletes, lines, fixed_lines, orig_index, log
        )
        if applied_deletes == 0 and not by_line and not insertions:
            log(f"no changes needed: {path}")
            return False
    backup_path = None
    if backup:
        stamp = int(time.time() * 1000)
        backup_path = backup_path_for(Path(store_dir), Path(mods_dir), path, stamp)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        move_file(path, backup_path)
    try:
        path.write_bytes("".join(fixed_lines).encode(encoding))
    except OSError:
        if backup_path is not None:
            move_file(backup_path, path)
        raise
    for suggestion in effective:
        if suggestion.kind == "hash":
            suffix = f" [{suggestion.labels}]" if suggestion.labels else ""
            log(f"{suggestion.old} to {suggestion.new}{suffix}")
        elif suggestion.kind in STRUCTURAL_KINDS:
            log(suggestion.reason)
    changes = len(effective) - len(deletes) + applied_deletes
    if backup_path is not None:
        log(f"fixed {path} ({changes} changes, backup {backup_path.name} -> store)")
    else:
        log(f"fixed {path} ({changes} changes, no new backup)")
    return True


def revert_backups(
    choices: Sequence[tuple[Path, Path]], log: Callable[[str], None] = print
) -> int:
    """Restore live files from the chosen backup of each chain.

    Only the chosen backup is consumed; other chain entries stay for further reverts.
    Returns the number of files restored."""
    restored = 0
    for live, chosen in choices:
        move_file(chosen, live)
        restored += 1
        log(f"restored {live} from {chosen.name}")
    return restored
