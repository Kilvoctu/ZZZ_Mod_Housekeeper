"""Parse the Chinese changelog of the ZZZ-Model-Hash repo (Hash变动日志.txt).

Newest version section first; shapes: 版本 x -> y headers, 【name/name】 groups,
IB lines (several -> specs /, optional （label） and ※ notes), child rows
(<role>: <old> -> <new>, incl. object_indexes [..] -> [..]); the rest skips.
"""

import re
from pathlib import Path

from .model import HASH_RE, ChangeEntry, normalize_name

_ARROW_RE = re.compile(r"->|→")
ARROW_SPLIT_RE = re.compile(r"\s*(?:->|→)\s*")
_VERSION_HEADER_RE = re.compile(r"^\s*版本\s+(?P<label>.+)$")
_IB_RE = re.compile(r"^IB\s*:\s*(?P<rest>.*)$", re.IGNORECASE)
_CHILD_RE = re.compile(r"^(?P<role>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")
_CHILD_TRANS_RE = re.compile(r"^(?P<old>\S+)\s*(?:->|→)\s*(?P<new>[0-9a-fA-F]{8})\s*$")
_INDEXES_TRANS_RE = re.compile(
    r"^(?P<old>\[[^]]*])\s*(?:->|→)\s*(?P<new>\[[^]]*])\s*$"
)
_NEW_TOKEN_RE = re.compile(r"[（(]\s*新增\s*[）)]")
_LABEL_RE = re.compile(r"[（(](?P<label>[^（）()]*)[）)]")
_INT_RE = re.compile(r"-?\d+")
_VERSION_TAG_RE = re.compile(r"v\d+(?:\.\d+)*(?:热更新)?")


def parse_changelog(text: str) -> list[ChangeEntry]:
    """Parse the changelog text into chronologically ordered change entries.

    Version sections sit newest-first in the file, so version_index is assigned
    bottom-up (oldest = 1) and the returned list is ascending (oldest first).
    """
    pre_entries: list[ChangeEntry] = []
    sections: list[tuple[str, list[ChangeEntry]]] = []
    cur_label = ""
    cur_entries: list[ChangeEntry] | None = None
    cur_characters: list[str] = []
    last_ib_transition: tuple[str, str] | None = None
    in_trailer = False

    def emit(
        role: str,
        from_hash: str | None = None,
        to_hash: str | None = None,
        component_label: str | None = None,
        from_indexes: list[int] | None = None,
        to_indexes: list[int] | None = None,
    ) -> None:
        target = pre_entries if cur_entries is None else cur_entries
        target.append(
            ChangeEntry(
                from_hash=from_hash,
                to_hash=to_hash,
                characters=list(cur_characters),
                role=role,
                component_label=component_label,
                from_indexes=from_indexes,
                to_indexes=to_indexes,
                version_label=cur_label,
            )
        )

    for line in text.splitlines():
        if in_trailer:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) <= {"="} or set(stripped) <= {"-"}:
            if "=" not in stripped:
                in_trailer = True
            continue
        header = _VERSION_HEADER_RE.match(line)
        if header is not None and _ARROW_RE.search(header.group("label")):
            if cur_entries is not None:
                sections.append((cur_label, cur_entries))
            cur_label = header.group("label").strip()
            cur_entries = []
            cur_characters = []
            last_ib_transition = None
            continue
        if stripped.startswith("【") and stripped.endswith("】"):
            cur_characters = _group_characters(stripped[1:-1])
            last_ib_transition = None
            continue
        work = stripped.split("※", 1)[0].strip()
        if not work:
            continue
        ib = _IB_RE.match(work)
        if ib is not None:
            parsed = _parse_ib_line(ib.group("rest"))
            if parsed is not None:
                from_hashes, child_to, label = parsed
                if child_to is not None:
                    for child_from in from_hashes:
                        emit(
                            role="ib",
                            from_hash=child_from,
                            to_hash=child_to,
                            component_label=label,
                        )
                    last_ib_transition = (from_hashes[-1], child_to)
            continue
        child = _CHILD_RE.match(work)
        if child is not None:
            child_role = child.group("role").lower()
            value = child.group("value").strip()
            if child_role in ("object_indexes", "object_classifications"):
                match = _INDEXES_TRANS_RE.match(value)
                if match is not None:
                    child_from_indexes = _parse_int_list(match.group("old"))
                    child_to_indexes = _parse_int_list(match.group("new"))
                    if child_from_indexes is not None and child_to_indexes is not None:
                        if child_role == "object_indexes" and last_ib_transition is not None:
                            emit(
                                role=child_role,
                                from_indexes=child_from_indexes,
                                to_indexes=child_to_indexes,
                                from_hash=last_ib_transition[0],
                                to_hash=last_ib_transition[1],
                            )
                        else:
                            emit(
                                role=child_role,
                                from_indexes=child_from_indexes,
                                to_indexes=child_to_indexes,
                            )
                continue
            transition = _CHILD_TRANS_RE.match(value)
            if transition is not None:
                from_token = transition.group("old")
                if _NEW_TOKEN_RE.fullmatch(from_token):
                    child_from = None
                elif HASH_RE.fullmatch(from_token):
                    child_from = from_token.lower()
                else:
                    continue
                emit(
                    role=child_role,
                    from_hash=child_from,
                    to_hash=transition.group("new").lower(),
                )
            continue

    if cur_entries is not None:
        sections.append((cur_label, cur_entries))
    total = len(sections)
    for position, (_label, entries) in enumerate(sections):
        version_index = total - position
        for entry in entries:
            entry.version_index = version_index
    result = list(pre_entries)
    for position in range(total - 1, -1, -1):
        result.extend(sections[position][1])
    return result


def decode_changelog(data: bytes) -> str:
    """Decode raw changelog bytes (utf-8 with BOM, falling back to gbk)."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")


def parse_changelog_file(path: Path) -> list[ChangeEntry]:
    """Read the changelog file (utf-8, falling back to gbk) and parse it."""
    return parse_changelog(decode_changelog(Path(path).read_bytes()))


def parse_face_texcoord_transitions_file(path: Path) -> dict[str, str]:
    """Read the changelog file and map face texcoord from-hashes to current hashes."""
    return parse_face_texcoord_transitions(decode_changelog(Path(path).read_bytes()))


def parse_face_texcoord_transitions(text: str) -> dict[str, str]:
    """Map from_hash -> to_hash for texcoord child lines under face-labeled IB lines.

    A child line belongs to the nearest preceding "IB: ...（label）" line; only
    labels containing 脸 qualify (headers/groups reset context); the first edge
    per from-hash wins (newest-first, so the most recent).
    """
    transitions: dict[str, str] = {}
    label = ""
    in_trailer = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or in_trailer:
            continue
        if set(stripped) <= {"="} or set(stripped) <= {"-"}:
            if "=" not in stripped:
                in_trailer = True
            continue
        if stripped.startswith("【") and stripped.endswith("】"):
            label = ""
            continue
        if stripped.startswith("版本") and _ARROW_RE.search(stripped):
            label = ""
            continue
        work = stripped.split("※", 1)[0].strip()
        if not work:
            continue
        ib = _IB_RE.match(work)
        if ib is not None:
            label_match = _LABEL_RE.search(ib.group("rest"))
            label = label_match.group("label") if label_match else ""
            continue
        child = _CHILD_RE.match(work)
        if child is None:
            continue
        if child.group("role").lower() not in ("texcoord", "texcoord_vb"):
            continue
        if "脸" not in label:
            continue
        transition = _CHILD_TRANS_RE.match(child.group("value").strip())
        if transition is None:
            continue
        from_token = transition.group("old")
        if not HASH_RE.fullmatch(from_token):
            continue
        from_hash = from_token.lower()
        if from_hash not in transitions:
            transitions[from_hash] = transition.group("new").lower()
    return transitions


def build_chain_index(entries: list[ChangeEntry]) -> dict[str, list[ChangeEntry]]:
    """Map from_hash -> entries with that from_hash, sorted ascending by version_index.

    Entries without an 8-hex from_hash are excluded.
    """
    index: dict[str, list[ChangeEntry]] = {}
    for entry in entries:
        from_hash = entry.from_hash
        if from_hash is None or not HASH_RE.fullmatch(from_hash):
            continue
        index.setdefault(from_hash, []).append(entry)
    for bucket in index.values():
        bucket.sort(key=lambda change: change.version_index)
    return index


def _parse_ib_line(rest: str) -> tuple[list[str], str | None, str | None] | None:
    """Parse the text after "IB:" on one IB line.

    Returns (from_hashes, to_hash, component_label); to_hash None when no
    transition (several IBs whose only children changed); malformed -> None.
    """
    parts = ARROW_SPLIT_RE.split(rest.strip(), maxsplit=1)
    if len(parts) != 2:
        return [], None, None
    left, right = parts
    to_match = HASH_RE.match(right)
    if to_match is None:
        return None
    label_match = _LABEL_RE.search(right[to_match.end():])
    label = label_match.group("label") if label_match else None
    from_hashes: list[str] = []
    for spec in left.split("/"):
        found = HASH_RE.match(spec.strip())
        if found is not None:
            from_hashes.append(found.group(0).lower())
    if not from_hashes:
        return None
    return from_hashes, to_match.group(0).lower(), label


def _parse_int_list(bracketed: str) -> list[int] | None:
    """Parse "[0, 42759]" into [0, 42759]; None when an item is not an integer."""
    values: list[int] = []
    for part in bracketed[1:-1].split(","):
        part = part.strip()
        if not part:
            continue
        if not _INT_RE.fullmatch(part):
            return None
        values.append(int(part))
    return values


def _group_characters(header: str) -> list[str]:
    """Normalized character names from the text inside one 【...】 header.

    Tokens split on "/" and " - "; version tags ("v3.01热更新") and letter-less
    tokens (e.g. "5") drop.
    """
    names: list[str] = []
    for part in header.split("/"):
        for token in part.split(" - "):
            token = token.strip()
            if not token or _VERSION_TAG_RE.fullmatch(token):
                continue
            if not _has_cjk_or_ascii_letter(token):
                continue
            normalized = normalize_name(token)
            if normalized:
                names.append(normalized)
    return names


def _has_cjk_or_ascii_letter(text: str) -> bool:
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z" or "\u4e00" <= ch <= "\u9fff":
            return True
    return False
