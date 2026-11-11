"""Cross-character face conflict repair for ZZZ (ZZMI) mod ini files.

A mod drawing a shared position hash (Lucy hair 6c733c84) breaks other characters;
the repair moves its draw content into the blend section under `if DRAW_TYPE == 1`.
"""

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .backups import backup_path_for, included_ini_files, move_file
from .fixer import _decode_ini
from .model import HASH_RE
from .repo import data_dir

POSITION_RE = re.compile(r"Resource\s*(\w+?)\s*Position\b", re.IGNORECASE)
BLEND_RE = re.compile(r"Resource\s*(\w+?)\s*Blend\b", re.IGNORECASE)
# Loose fallbacks tolerate a few mojibake characters between Resource and the role word.
POSITION_RE_LOOSE = re.compile(
    r"Resource.{0,4}?(\w+?)Position\b", re.IGNORECASE | re.DOTALL
)
BLEND_RE_LOOSE = re.compile(r"Resource.{0,4}?(\w+?)Blend\b", re.IGNORECASE | re.DOTALL)
_ACTIVE_HASH_RE = re.compile(r"^hash\s*=\s*([0-9a-fA-F]{8})")
_COMMENTED_HASH_RE = re.compile(r"^;\s*hash\s*=\s*([0-9a-fA-F]{8})")
_ROLE_RE = re.compile(r"(Position|Blend)\w*")
_VB0_RE = re.compile(r"^\s*vb0\s*=", re.IGNORECASE)
_DRAW_LINE_RE = re.compile(r"^\s*(vb0|draw)\s*=", re.IGNORECASE)
_UNLISTED_HASH_FALLBACK = (
    "a hash is not in the face conflict table (possibly changed), left unprocessed; "
    "add it to data/face_conflict_hashes.json"
)

KeyList = list[tuple[str, str, str]]


@dataclass
class FaceConflictCharacter:
    """One character's shared-hash sets from the shipped table."""

    name: str
    position_vb: frozenset[str] = frozenset()
    position_wallpaper_vb: frozenset[str] = frozenset()
    blend_vb: frozenset[str] = frozenset()


@dataclass
class FaceConflictTable:
    """The loaded cross-character hash table (empty when missing or malformed)."""

    characters: tuple[FaceConflictCharacter, ...] = ()


@dataclass
class SectionReplacement:
    """One section rewrite: the 1-based header line index and its new body (no EOLs)."""

    start: int
    body: tuple[str, ...]


@dataclass
class FaceConflictPlan:
    """The repair plan for one ini: section replacements plus report/warning lines."""

    replacements: tuple[SectionReplacement, ...] = ()
    reports: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass
class _CharSets:
    """Per-character hash sets used for section attribution."""

    name: str
    pos: frozenset[str]
    wall: frozenset[str]
    blend: frozenset[str]


@dataclass
class _FaceSection:
    """One parsed ini section with cached scan facts."""

    name: str
    start: int
    end: int
    body: tuple[str, ...]
    keys: KeyList = field(default_factory=list)
    all_keys: KeyList = field(default_factory=list)
    has_top_if: bool = False
    active: str | None = None
    commented: tuple[str, ...] = ()
    has_vb0: bool = False
    has_vb2: bool = False

    @property
    def h(self) -> str | None:
        """The locating hash: the active one, else the first commented one."""
        if self.active is not None:
            return self.active
        return self.commented[0] if self.commented else None


@dataclass
class _SecView:
    """One position/blend section's similarity-view entry for complement checks."""

    prefix: str | None
    role: str
    h: str | None
    name: str
    body: tuple[str, ...]


@dataclass
class _FixTarget:
    """A bare (or degenerate blend-hash) position section to migrate."""

    ci: int
    prefix: str | None
    sec: _FaceSection
    keys: KeyList


@dataclass
class _WallTarget:
    """A wallpaper/variant position section to hollow."""

    sec: _FaceSection
    prefix: str | None
    ci: int
    h: str | None
    keys: KeyList


@dataclass
class _OldTarget:
    """A legacy if-wrapped position section to migrate whole."""

    sec: _FaceSection
    h: str | None
    ci: int


@dataclass
class _Classification:
    """Per-section repair classification for one file."""

    fixes: list[_FixTarget] = field(default_factory=list)
    walls: list[_WallTarget] = field(default_factory=list)
    legacy: list[_OldTarget] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    """Merged migration content for one (character, resource prefix) key."""

    keysets: list[KeyList] = field(default_factory=list)
    blocks: list[list[str]] = field(default_factory=list)
    sections: list[tuple[_FaceSection, str]] = field(default_factory=list)


def face_conflict_table_path() -> Path:
    """Path of the shipped cross-character hash table."""
    return data_dir("face_conflict_hashes.json")


def _hash_set(values: object) -> frozenset[str]:
    """Lowercase 8-hex hashes from a JSON list; anything else contributes nothing."""
    if not isinstance(values, list):
        return frozenset()
    return frozenset(
        value.lower()
        for value in values
        if isinstance(value, str) and HASH_RE.fullmatch(value)
    )


def load_face_conflict_table(path: Path | None = None) -> FaceConflictTable:
    """Loaded table; empty (no characters) when the file is missing or malformed.

    Hashes must be 8 hex digits, lowercased and deduped; entries tolerate missing keys,
    unknown JSON fields."""
    path = face_conflict_table_path() if path is None else Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return FaceConflictTable()
    if not isinstance(payload, dict):
        return FaceConflictTable()
    entries = payload.get("characters")
    if not isinstance(entries, list):
        return FaceConflictTable()
    return FaceConflictTable(
        characters=tuple(
            FaceConflictCharacter(
                name=str(entry.get("name", "")),
                position_vb=_hash_set(entry.get("position_vb")),
                position_wallpaper_vb=_hash_set(entry.get("position_wallpaper_vb")),
                blend_vb=_hash_set(entry.get("blend_vb")),
            )
            for entry in entries
            if isinstance(entry, dict)
        )
    )


def _parse_ini(text: str) -> tuple[list[str], list[_FaceSection]]:
    """Split ini text into lines and sections (0-based header index, exclusive end)."""
    lines = text.split("\n")
    headers = [
        i for i, line in enumerate(lines) if line.lstrip().startswith("[") and "]" in line
    ]
    sections: list[_FaceSection] = []
    for j, idx in enumerate(headers):
        end = headers[j + 1] if j + 1 < len(headers) else len(lines)
        name = lines[idx][lines[idx].find("[") + 1 : lines[idx].find("]")].strip()
        sections.append(_FaceSection(name=name, start=idx, end=end, body=tuple(lines[idx + 1 : end])))
    return lines, sections


def _texture_override_sections(sections: list[_FaceSection]) -> list[_FaceSection]:
    """Sections whose name starts with TextureOverride."""
    return [sec for sec in sections if sec.name.lower().startswith("textureoverride")]


def _analyze_body(body: Sequence[str]) -> tuple[KeyList, bool]:
    """Top-level key triples and whether the body has a top-level if block."""
    keys: KeyList = []
    depth = 0
    has_top_if = False
    for line in body:
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        low = s.lower()
        if low == "endif":
            depth = max(0, depth - 1)
            continue
        if low.startswith("else"):
            continue
        if low == "if" or low.startswith("if "):
            if depth == 0:
                has_top_if = True
            depth += 1
            continue
        if depth > 0:
            continue
        if "=" in s:
            k, v = s.split("=", 1)
            keys.append((k.strip().lower(), k.strip(), v.strip()))
    return keys, has_top_if


def _extract_all_keys(body: Sequence[str]) -> KeyList:
    """Every key in the body ignoring if/else/endif structure, deduped by key."""
    keys: KeyList = []
    seen: set[str] = set()
    for line in body:
        s = line.strip()
        if not s or s.startswith(";"):
            continue
        low = s.lower()
        if low.startswith(("if ", "else")) or low in ("if", "endif"):
            continue
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        kl = k.strip().lower()
        if kl not in seen:
            seen.add(kl)
            keys.append((kl, k.strip(), v.strip()))
    return keys


def _get_hashes(body: Sequence[str]) -> tuple[str | None, tuple[str, ...]]:
    """(active hash, commented hashes) of a section body."""
    active: str | None = None
    commented: list[str] = []
    for line in body:
        s = line.strip()
        match = _ACTIVE_HASH_RE.match(s)
        if match is not None:
            active = match.group(1).lower()
            continue
        match = _COMMENTED_HASH_RE.match(s)
        if match is not None:
            commented.append(match.group(1).lower())
    return active, tuple(commented)


def _hash_matches(body: Sequence[str], hash_set: frozenset[str]) -> bool:
    """True when the active or any commented hash line hits ``hash_set``."""
    active, commented = _get_hashes(body)
    if active is not None and active in hash_set:
        return True
    return any(c in hash_set for c in commented)


def _get_hash(keys: KeyList) -> str | None:
    """The section's active hash from top-level keys, lowercase."""
    for k, _ko, v in keys:
        if k == "hash":
            return v.lower()
    return None


def _bind_resource(
    keys: KeyList, regex: re.Pattern[str], loose_regex: re.Pattern[str] | None = None
) -> str | None:
    """Resource prefix extracted from key values (exact regex, then loose fallback)."""
    for _k, _ko, v in keys:
        match = regex.search(v)
        if match is not None:
            return match.group(1)
    if loose_regex is not None:
        for _k, _ko, v in keys:
            match = loose_regex.search(v)
            if match is not None:
                return match.group(1)
    return None


def _sec_role(sec_name: str) -> str | None:
    """'position'/'blend' keyword of a section name, lowercase, or None."""
    match = _ROLE_RE.search(sec_name)
    return match.group(1).lower() if match else None


def _prefix_from_name(sec_name: str, keyword: str) -> str | None:
    """Fallback resource prefix from the section name for one role keyword."""
    match = re.match(r"TextureOverride\+?(\w+?)" + keyword, sec_name, re.IGNORECASE)
    return match.group(1) if match else None


def _get_prefix(keys: KeyList, sec_name: str, allow_blend: bool = False) -> str | None:
    """Section resource prefix: value binds first, then the section name fallback."""
    prefix = _bind_resource(keys, POSITION_RE, POSITION_RE_LOOSE)
    if prefix is None:
        prefix = _prefix_from_name(sec_name, "Position")
    if prefix is None and allow_blend:
        prefix = _prefix_from_name(sec_name, "Blend")
    return prefix


def _role_of(body: Sequence[str], char_sets: list[_CharSets]) -> tuple[int | None, str | None]:
    """(character index, 'pos'/'wall'/'blend') of the first hash hit, or (None, None)."""
    for i, cs in enumerate(char_sets):
        for kind, hashes in (("pos", cs.pos), ("wall", cs.wall), ("blend", cs.blend)):
            if _hash_matches(body, hashes):
                return i, kind
    return None, None


def _fill_info(sections: list[_FaceSection]) -> None:
    """Populate cached scan facts (keys, hashes, buffer binds) on each section."""
    for sec in sections:
        sec.keys, sec.has_top_if = _analyze_body(sec.body)
        sec.all_keys = _extract_all_keys(sec.body)
        sec.active, sec.commented = _get_hashes(sec.body)
        sec.has_vb0 = any(k == "vb0" for k, _ko, _v in sec.keys) or any(
            k == "vb0" for k, _ko, _v in sec.all_keys
        )
        sec.has_vb2 = any(k == "vb2" for k, _ko, _v in sec.keys) or any(
            k == "vb2" for k, _ko, _v in sec.all_keys
        )


def _character_sets(table: FaceConflictTable) -> list[_CharSets]:
    """Per-character pos/wall/blend hash sets from the table."""
    return [
        _CharSets(
            name=character.name,
            pos=character.position_vb,
            wall=character.position_wallpaper_vb,
            blend=character.blend_vb,
        )
        for character in table.characters
    ]


def _found_similar(
    overrides: list[_FaceSection], all_sets: frozenset[str]
) -> dict[tuple[str | None, str], list[str]]:
    """(prefix, role) -> section names for sections whose hash is on record."""
    found: dict[tuple[str | None, str], list[str]] = {}
    for sec in overrides:
        if not _hash_matches(sec.body, all_sets):
            continue
        role = _sec_role(sec.name)
        prefix = _get_prefix(sec.all_keys, sec.name, allow_blend=True)
        if role is not None and prefix is not None:
            found.setdefault((prefix, role), []).append(sec.name)
    return found


def _sec_view(overrides: list[_FaceSection]) -> list[_SecView]:
    """Position/blend similarity entries for the complement check."""
    view: list[_SecView] = []
    for sec in overrides:
        if sec.h is None:
            continue
        role = _sec_role(sec.name)
        if role not in ("position", "blend"):
            continue
        view.append(
            _SecView(
                prefix=_get_prefix(sec.all_keys, sec.name, allow_blend=True),
                role=role,
                h=sec.h,
                name=sec.name,
                body=sec.body,
            )
        )
    return view


def _complement_warnings(
    view: list[_SecView], char_sets: list[_CharSets]
) -> list[str]:
    """Per-character one-sided position/blend hints when only one side is on record."""
    warns: list[str] = []
    for cs in char_sets:
        pos_ok = [
            entry
            for entry in view
            if entry.role == "position" and _hash_matches(entry.body, cs.pos | cs.wall)
        ]
        blend_ok = [
            entry for entry in view if entry.role == "blend" and _hash_matches(entry.body, cs.blend)
        ]
        warns.extend(_one_sided_warnings(view, cs, pos_ok, blend_ok))
    return warns


def _one_sided_warnings(
    view: list[_SecView],
    cs: _CharSets,
    pos_ok: list[_SecView],
    blend_ok: list[_SecView],
) -> list[str]:
    """Hints for the missing side of one character's position/blend pair."""
    warns: list[str] = []
    if pos_ok and not blend_ok:
        for entry in view:
            if entry.role == "blend" and not _hash_matches(entry.body, cs.blend):
                base = next((v.name for v in pos_ok if v.prefix == entry.prefix), None)
                if base is not None:
                    warns.append(
                        f"position hash found but no matching blend hash: hash {entry.h} "
                        f"(section resembles [{base}]) may have changed; add it to the "
                        "face conflict table"
                    )
                    break
    elif blend_ok and not pos_ok:
        for entry in view:
            if entry.role == "position" and not _hash_matches(entry.body, cs.pos | cs.wall):
                base = next((v.name for v in blend_ok if v.prefix == entry.prefix), None)
                if base is not None:
                    warns.append(
                        f"blend hash found but no matching position hash: hash {entry.h} "
                        f"(section resembles [{base}]) may have changed; add it to the "
                        "face conflict table"
                    )
                    break
    return warns


def _has_unfixed_section(
    overrides: list[_FaceSection], all_sets: frozenset[str]
) -> bool:
    """True when a bare position section carries a known hash (file looks unfixed)."""
    for sec in overrides:
        if "blend" in sec.name.lower() or sec.has_top_if or not sec.has_vb0:
            continue
        if _hash_matches(sec.body, all_sets):
            return True
    return False


def _similarity_warning(
    h: str,
    role: str | None,
    prefix: str | None,
    found_similar: dict[tuple[str | None, str], list[str]],
    fallback: str,
) -> str:
    """Specific changed-hash hint when a same-prefix same-role section is on record."""
    match: str | None = None
    if role is not None:
        for (p, r), names in found_similar.items():
            if r == role and p == prefix:
                match = names[0]
                break
    if match is not None:
        return (
            f"hash {h} may be a changed {role} hash (section name resembles [{match}]); "
            "add it to the face conflict table"
        )
    return fallback


def _classify_sections(
    overrides: list[_FaceSection],
    char_sets: list[_CharSets],
    has_unfixed: bool,
    found_similar: dict[tuple[str | None, str], list[str]],
) -> _Classification:
    """Bucket TextureOverride sections into migration/wallpaper/legacy targets."""
    out = _Classification()
    noticed = False
    for sec in overrides:
        if not sec.keys and not sec.all_keys:
            continue
        prefix = _get_prefix(sec.keys, sec.name)
        ci, ck = _role_of(sec.body, char_sets)
        if not sec.has_vb0:
            if sec.has_vb2 and sec.h is not None and not noticed and has_unfixed and ci is None:
                noticed = True
                out.warnings.append(
                    _similarity_warning(
                        sec.h,
                        _sec_role(sec.name),
                        _get_prefix(sec.keys, sec.name, allow_blend=True),
                        found_similar,
                        _UNLISTED_HASH_FALLBACK,
                    )
                )
            continue
        if sec.has_top_if:
            if ck == "wall":
                out.walls.append(_WallTarget(sec, prefix, ci, sec.h, sec.all_keys))
            elif ck == "pos":
                out.legacy.append(_OldTarget(sec, sec.h, ci))
            continue
        if ck == "pos" or (ck == "blend" and "blend" not in sec.name.lower()):
            out.fixes.append(_FixTarget(ci, prefix, sec, sec.keys))
        elif ck == "wall":
            out.walls.append(_WallTarget(sec, prefix, ci, sec.h, sec.keys))
        elif ck is None and sec.h is not None and not noticed and has_unfixed:
            noticed = True
            out.warnings.append(
                _similarity_warning(sec.h, _sec_role(sec.name), prefix, found_similar, _UNLISTED_HASH_FALLBACK)
            )
    return out


def _build_merged_block(target_keys_lists: list[KeyList]) -> list[list[str]]:
    """New-format migration blocks: handling/vb2 pre, if DRAW_TYPE == 1 wrapped draw keys,
    match_priority post; byte-identical blocks are deduped.

    ``target_keys_lists`` holds one key list per source section, so sections with
    different draw values become separate blocks."""
    blocks: list[list[str]] = []
    for keys in target_keys_lists:
        pre: list[tuple[str, str, str]] = []
        inner: list[tuple[str, str, str]] = []
        post: list[tuple[str, str, str]] = []
        for k, ko, v in keys:
            if k == "hash":
                continue
            if k == "match_priority":
                if not any(p[0] == k for p in post):
                    post.append((k, ko, v))
            elif k in ("handling", "vb2"):
                if not any(p[0] == k for p in pre):
                    pre.append((k, ko, v))
            elif not any(p[0] == k for p in inner):
                inner.append((k, ko, v))
        pre.sort(key=lambda t: 0 if t[0] == "handling" else 1)
        lines = [f"{ko} = {v}" for _k, ko, v in pre]
        if inner:
            lines.append("if DRAW_TYPE == 1")
            lines.extend(f"{ko} = {v}" for _k, ko, v in inner)
            lines.append("endif")
        lines.extend(f"{ko} = {v}" for _k, ko, v in post)
        blocks.append(lines)
    out: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for block in blocks:
        marker = tuple(block)
        if marker not in seen:
            seen.add(marker)
            out.append(block)
    return out


def _keep_hash_only(body: Sequence[str]) -> tuple[str, ...]:
    """Hollowed position body: hash lines, comments and blanks only."""
    return tuple(
        line
        for line in body
        if not line.strip()
        or line.strip().startswith(";")
        or line.strip().lower().startswith("hash")
    )


def _split_if_blocks(body: Sequence[str]) -> tuple[tuple[str, ...], list[list[str]]]:
    """(top-level keep lines, complete if-blocks) with unclosed blocks kept too."""
    keep: list[str] = []
    blocks: list[list[str]] = []
    depth = 0
    cur: list[str] | None = None
    for line in body:
        s = line.strip().lower()
        if s == "endif":
            if cur is not None:
                cur.append(line)
                depth -= 1
                if depth == 0:
                    blocks.append(cur)
                    cur = None
            continue
        if s == "if" or s.startswith("if "):
            if depth == 0:
                cur = [line]
                depth = 1
            elif cur is not None:
                cur.append(line)
                depth += 1
            continue
        if s.startswith("else") and cur is not None:
            cur.append(line)
            continue
        if cur is not None:
            cur.append(line)
            continue
        keep.append(line)
    if cur is not None:
        blocks.append(cur)
    return tuple(keep), blocks


def _is_draw_type_block(blk: Sequence[str]) -> bool:
    """True when the block opens with the new-format ``if DRAW_TYPE == 1``."""
    return bool(
        blk
        and blk[0].strip().lower().startswith("if")
        and "draw_type" in blk[0].strip().lower()
    )


def _vb0_wrapped_correctly(blk: Sequence[str]) -> bool:
    """True when every vb0 line's innermost open if is a DRAW_TYPE condition."""
    stack: list[bool] = []
    for line in blk:
        s = line.strip().lower()
        if s == "if" or s.startswith("if "):
            stack.append("draw_type" in s)
        elif s == "endif":
            if stack:
                stack.pop()
        elif _VB0_RE.match(s) and (not stack or not stack[-1]):
            return False
    return True


def _needs_draw_type(blk: Sequence[str]) -> bool:
    """True when a legacy if-block needs DRAW_TYPE normalization (vb0 unwrapped)."""
    if not any(_VB0_RE.match(line) for line in blk):
        return False
    return not _vb0_wrapped_correctly(blk)


def _wrap_vb0_with_draw_type(blk: list[str]) -> list[str]:
    """Legacy if-block with vb0/draw lines wrapped in if DRAW_TYPE == 1 inside.

    handling/vb2 keys stay outside the guard, under the original condition; the trailing
    line is re-appended only when it is the block's closing endif."""
    if not blk:
        return []
    out = [blk[0]]
    outer: list[str] = []
    inner: list[str] = []
    wrapped = False
    depth = 0
    for line in blk[1:]:
        s = line.strip()
        low = s.lower()
        if low == "if" or low.startswith("if "):
            depth += 1
            (inner if wrapped else outer).append(line)
            continue
        if low == "endif":
            if depth > 0:
                depth -= 1
                (inner if wrapped else outer).append(line)
                continue
            break
        if depth == 0 and _DRAW_LINE_RE.match(s):
            wrapped = True
            inner.append(line)
        elif depth == 0:
            outer.append(line)
        else:
            (inner if wrapped else outer).append(line)
    out.extend(outer)
    if wrapped:
        out.append("if DRAW_TYPE == 1")
        out.extend(inner)
        out.append("endif")
    if blk[-1].strip().lower() == "endif":
        out.append(blk[-1])
    return out


def _normalize_block(blk: list[str]) -> list[str]:
    """Normalized new-format block for one legacy if-block."""
    if not blk:
        return []
    if _is_draw_type_block(blk):
        inner = blk[1:-1]
        if not inner:
            return list(blk)
        if inner[0].strip().lower().startswith("if"):
            return _wrap_vb0_with_draw_type(inner)
        return list(blk)
    return _wrap_vb0_with_draw_type(blk)


def _build_new_blend_body(blend: _FaceSection, blk: list[str]) -> list[str]:
    """New blend body: preserved top-level lines, new blocks, legacy if-blocks normalized."""
    keep, blocks = _split_if_blocks(blend.body)
    new_body = list(keep)
    new_body.extend(blk)
    for legacy in blocks:
        if _needs_draw_type(legacy):
            new_body.extend(_normalize_block(legacy))
        else:
            new_body.extend(legacy)
    if blk and blend.body and not blend.body[-1].strip():
        new_body.append("")
    return new_body


def _find_blend_section(
    sections: list[_FaceSection], prefix: str | None, blend_hashes: frozenset[str]
) -> _FaceSection | None:
    """Best blend target for one prefix: name match > hash match > blend bind.

    Sections binding the position buffer are excluded; ties prefer the emptiest section."""
    best: _FaceSection | None = None
    best_score = -1.0
    for sec in sections:
        if not sec.name.lower().startswith("textureoverride") or not sec.keys:
            continue
        if _bind_resource(sec.keys, POSITION_RE, POSITION_RE_LOOSE):
            continue
        binds_blend = _bind_resource(sec.keys, BLEND_RE, BLEND_RE_LOOSE)
        score = 0.0
        if prefix and "blend" in sec.name.lower() and prefix.lower() in sec.name.lower():
            score += 2.0
        if _hash_matches(sec.body, blend_hashes):
            score += 1.0
        if binds_blend:
            score += 0.5
        if score <= 0:
            continue
        if score == best_score and best is not None:
            if len(sec.keys) < len(best.keys):
                best = sec
            continue
        if score > best_score:
            best, best_score = sec, score
    return best


def _find_upgrade_targets(
    sections: list[_FaceSection], blend_hashes: frozenset[str]
) -> list[_FaceSection]:
    """Blend sections carrying legacy position blocks that need the DRAW_TYPE wrap."""
    targets: list[_FaceSection] = []
    for sec in sections:
        if not sec.name.lower().startswith("textureoverride") or "blend" not in sec.name.lower():
            continue
        if not sec.keys:
            continue
        binds_blend = _bind_resource(sec.keys, BLEND_RE, BLEND_RE_LOOSE)
        if not _hash_matches(sec.body, blend_hashes) and not binds_blend:
            continue
        _keep, blocks = _split_if_blocks(sec.body)
        if any(_needs_draw_type(b) for b in blocks):
            targets.append(sec)
    return targets


def _pending_from(classification: _Classification) -> dict[tuple[int, str | None], _Pending]:
    """Migration content grouped by (character index, resource prefix)."""
    pending: dict[tuple[int, str | None], _Pending] = {}
    for target in classification.fixes:
        entry = pending.setdefault((target.ci, target.prefix), _Pending())
        entry.keysets.append(target.keys)
        entry.sections.append((target.sec, ""))
    for old in classification.legacy:
        prefix = _get_prefix(old.sec.all_keys, old.sec.name)
        if prefix is None:
            continue
        _keep, blocks = _split_if_blocks(old.sec.body)
        if not blocks:
            continue
        entry = pending.setdefault((old.ci, prefix), _Pending())
        entry.blocks.extend(_normalize_block(b) for b in blocks)
        entry.sections.append((old.sec, "legacy"))
    return pending


def _migration_report(sec: _FaceSection, source: str, blend_name: str) -> str:
    """One report line for a migrated position section."""
    if source == "legacy":
        return (
            f"moved the legacy if-blocks of [{sec.name}] into [{blend_name}]; "
            "original section keeps only its hash"
        )
    return (
        f"moved [{sec.name}] (hash {sec.h}) into [{blend_name}]; "
        "original section keeps only its hash"
    )


def _migration_replacements(
    sections: list[_FaceSection],
    pending: dict[tuple[int, str | None], _Pending],
    char_sets: list[_CharSets],
) -> tuple[dict[int, tuple[str, ...]], list[str], list[str], set[tuple[int, str | None]]]:
    """Blend-side rewrites for each pending key, with refusals kept as warnings.

    Returns (replacements by 0-based section start, reports, warnings, failed keys);
    refused keys keep their original config including dependent wallpaper sections."""
    replacements: dict[int, tuple[str, ...]] = {}
    reports: list[str] = []
    warns: list[str] = []
    failed: set[tuple[int, str | None]] = set()
    for (ci, prefix), entry in pending.items():
        blend = _find_blend_section(sections, prefix, char_sets[ci].blend)
        if blend is None:
            warns.append(
                f"prefix {prefix}: no matching Blend section found; migration skipped, "
                "original config kept"
            )
            failed.add((ci, prefix))
            continue
        if not _hash_matches(blend.body, char_sets[ci].blend):
            warns.append(
                f"blend hash {_get_hash(blend.keys)} does not match the face conflict "
                "table; migration skipped, original config kept (add the hash to the table)"
            )
            failed.add((ci, prefix))
            continue
        new_body = _build_new_blend_body(blend, [])
        for block in _build_merged_block(entry.keysets):
            new_body.extend(block)
        for block in entry.blocks:
            new_body.extend(block)
        for sec, source in entry.sections:
            replacements[sec.start] = _keep_hash_only(sec.body)
            reports.append(_migration_report(sec, source, blend.name))
        replacements[blend.start] = tuple(new_body)
    return replacements, reports, warns, failed


def _rebuild(
    lines: list[str], sections: list[_FaceSection], replacements: dict[int, tuple[str, ...]]
) -> str:
    """File text with each replaced section's body swapped (header kept)."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if i in replacements:
            out.append(lines[i])
            out.extend(replacements[i])
            i = next((s.end for s in sections if s.start == i), i + 1)
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def _plan_or_none(
    replacements: dict[int, tuple[str, ...]],
    reports: list[str],
    warnings: list[str],
) -> FaceConflictPlan | None:
    """A plan when replacements or warnings survive, else None."""
    if not replacements and not warnings:
        return None
    return FaceConflictPlan(
        replacements=tuple(
            SectionReplacement(start=start + 1, body=body)
            for start, body in sorted(replacements.items())
        ),
        reports=tuple(reports),
        warnings=tuple(warnings),
    )


def _plan_from_classification(
    text: str,
    lines: list[str],
    sections: list[_FaceSection],
    overrides: list[_FaceSection],
    char_sets: list[_CharSets],
    all_blend: frozenset[str],
    classification: _Classification,
    warnings: list[str],
) -> FaceConflictPlan | None:
    """Assemble the final plan: migration, upgrades, hollowing and the no-op check."""
    upgrade_targets: list[_FaceSection] = []
    if not classification.fixes and not classification.walls and not classification.legacy:
        upgrade_targets = _find_upgrade_targets(overrides, all_blend)
        if not upgrade_targets:
            return _plan_or_none({}, [], warnings)
    pending = _pending_from(classification)
    if not pending and not classification.walls and not upgrade_targets:
        return _plan_or_none({}, [], warnings)
    replacements, reports, warns, failed = _migration_replacements(
        sections, pending, char_sets
    )
    warnings.extend(warns)
    for sec in upgrade_targets:
        replacements[sec.start] = tuple(_build_new_blend_body(sec, []))
        reports.append(
            f"upgraded [{sec.name}]: wrapped legacy position blocks in if DRAW_TYPE == 1"
        )
    for wall in classification.walls:
        if (wall.ci, wall.prefix) in failed:
            warnings.append(
                f"prefix {wall.prefix} migration failed; [{wall.sec.name}] kept unchanged"
            )
            continue
        replacements[wall.sec.start] = _keep_hash_only(wall.sec.body)
        reports.append(
            f"hollowed wallpaper/variant section [{wall.sec.name}] "
            f"(hash {_get_hash(wall.keys)}): keeps only its hash"
        )
    if not replacements or _rebuild(lines, sections, replacements) == text:
        return _plan_or_none({}, [], warnings)
    return _plan_or_none(replacements, reports, warnings)


def scan_face_conflict_fixes(
    ini_text: str, table: FaceConflictTable
) -> FaceConflictPlan | None:
    """Repair plan for decoded ini text, or None when there is nothing to fix or report.

    Pure analysis that never raises: sections are classified per character hash and the
    position-to-blend migration, wallpaper hollowing and legacy upgrades are planned."""
    text = ini_text.replace("\r\n", "\n").replace("\r", "\n")
    lines, sections = _parse_ini(text)
    overrides = _texture_override_sections(sections)
    char_sets = _character_sets(table)
    if not overrides or not char_sets:
        return None
    _fill_info(overrides)
    all_blend = frozenset().union(*(cs.blend for cs in char_sets))
    all_sets = frozenset().union(*(cs.pos | cs.wall | cs.blend for cs in char_sets))
    found_similar = _found_similar(overrides, all_sets)
    warnings = _complement_warnings(_sec_view(overrides), char_sets)
    classification = _classify_sections(
        overrides, char_sets, _has_unfixed_section(overrides, all_sets), found_similar
    )
    warnings.extend(classification.warnings)
    return _plan_from_classification(
        text, lines, sections, overrides, char_sets, all_blend, classification, warnings
    )


def _planned_text(path: Path, plan: FaceConflictPlan) -> str | None:
    """Rebuilt file text for a plan, or None when nothing would change."""
    text, _encoding = _decode_ini(path.read_bytes())
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines, sections = _parse_ini(normalized)
    replacements = {r.start - 1: r.body for r in plan.replacements}
    eol = "\r\n" if "\r\n" in text else "\n"
    new_text = eol.join(_rebuild(lines, sections, replacements).split("\n"))
    return None if new_text == text else new_text


def apply_face_conflict_plan(
    path: Path,
    plan: FaceConflictPlan,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None] = print,
    backup: bool = True,
) -> bool:
    """Apply one repair plan with a store backup; True only when the file was written.

    Encoding (BOM included) and line endings are preserved; an empty-replacements plan
    only logs its warnings and leaves the file untouched."""
    path = Path(path)
    if not plan.replacements:
        for warning in plan.warnings:
            log(f"warning: {warning}")
        return False
    new_text = _planned_text(path, plan)
    if new_text is None:
        for warning in plan.warnings:
            log(f"warning: {warning}")
        log(f"no changes needed: {path}")
        return False
    _encoding = _decode_ini(path.read_bytes())[1]
    backup_path = None
    if backup:
        stamp = int(time.time() * 1000)
        backup_path = backup_path_for(Path(store_dir), Path(mods_dir), path, stamp)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        move_file(path, backup_path)
    try:
        path.write_bytes(new_text.encode(_encoding))
    except OSError:
        if backup_path is not None:
            move_file(backup_path, path)
        raise
    for report in plan.reports:
        log(report)
    for warning in plan.warnings:
        log(f"warning: {warning}")
    return True


def apply_face_conflict_folder(
    folder: Path,
    store_dir: Path,
    mods_dir: Path,
    log: Callable[[str], None] = print,
    table: FaceConflictTable | None = None,
) -> list[Path]:
    """Scan+apply every enabled ini under ``folder``; returns the paths written.

    Per-file read/scan failures are logged and skipped; warnings from refused files are
    still logged."""
    if table is None:
        table = load_face_conflict_table()
    changed: list[Path] = []
    for path in included_ini_files(Path(folder)):
        try:
            text, _encoding = _decode_ini(path.read_bytes())
        except (OSError, ValueError) as exc:
            log(f"warning: face conflict scan skipped {path}: {exc}")
            continue
        plan = scan_face_conflict_fixes(text, table)
        if plan is None:
            continue
        try:
            if apply_face_conflict_plan(path, plan, store_dir, mods_dir, log=log):
                changed.append(path)
        except OSError as exc:
            log(f"warning: face conflict fix failed {path}: {exc}")
    return changed
