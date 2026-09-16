"""Cross-mod shared-hash claim conflict detection for 3dmigoto mod ini files.

Enabled mods claiming the same mesh hash without an interlocking ``$var`` handshake, stomping the same override property or buffer slot, or dispatching via ``checktextureoverride`` yield conflict rows.
Mods matching the dynamic dispatch hair pattern that the Fix action can convert are reported as fixable info rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from .backups import included_ini_files
from .fixer import cached_ini_parse
from .toggles import _RAW_VAR_RE, _compile_body, _normalize_statement, _walk_compiled

_DISABLED_PREFIX = "DISABLED_"
_TEXTURE_OVERRIDE_PREFIX = "textureoverride"
_GENERIC_STEM = "textureoverride"
_HANDLING_RE = re.compile(r"^handling\s*=\s*(\S+)$")
_FIRST_INDEX_RE = re.compile(r"^match_first_index\s*=")
_INDEX_COUNT_RE = re.compile(r"^match_index_count\s*=")
_OVERRIDE_RE = re.compile(r"^(override_\w+)\s*=\s*(\S.*)$")
_BIND_RE = re.compile(r"^(ib|vb\d+|ps-t\d+)\s*=\s*(\S.*)$")
_CTO_RE = re.compile(r"^checktextureoverride\s*=\s*(\S+)$")
_DRAW_TYPE_RE = re.compile(r"(?<![\w$])draw_type\s*==\s*(?:2|4)(?!\d)")
_WRITE_RE = re.compile(r"^(?:post\s+|pre\s+)?\$(\w+)\s*[-+*/]?=(?!=)")
_ASSIGN_RE = re.compile(r"^(?:post\s+|pre\s+)?\$(\w+)\s*=(?!=)\s*(\S.*)$")
_ZERO_LITERAL_RE = re.compile(r"^0+(?:\.0*)?$")
_DECREMENT_RE = re.compile(r"^(?:post\s+|pre\s+)?\$(\w+)\s*=\s*\$\1\s*-\s*\S+$")
_OP_DECREMENT_RE = re.compile(r"^(?:post\s+|pre\s+)?\$(\w+)\s*-=\s*\S+$")
_UNIFY_KEY = "override_vertex_count"
_UNIFY_PREFIX_RE = re.compile(r"^\s*override_vertex_count\s*=\s*", re.IGNORECASE)
_SOLVABLE_SUFFIX = " Fix on the containing mod folder resolves this."


@dataclass
class ClaimConflict:
    """One cross-mod shared-hash finding about enabled mods (``kind`` "co-claim",
    "stomp", "dispatch" or "fixable"; ``severity`` "conflict" or "info"; ``hash``
    "" when no claim hash applies; ``solvable`` marks rows the Fix action can
    resolve via deference or unify specs)."""

    kind: str
    hash: str
    mods: list[str]
    text: str
    severity: str
    solvable: bool = False


@dataclass
class DeferWrapSpec:
    """One pending ``if $flag == 0`` wrap of a mod section's full line span.

    ``flag`` is ``other``'s outfit variable name without ``$``; wrapping the
    span defers this section while that mod's outfit renders on screen."""

    file: Path
    mod: str
    section: str
    start: int
    end: int
    flag: str
    other: str


@dataclass
class UnifySpec:
    """One pending rewrite of an ``override_vertex_count`` line to the group max.

    ``others`` lists the other group mods' names, sorted."""

    file: Path
    mod: str
    section: str
    line_no: int
    old: str
    new: str
    others: list[str]


@dataclass
class _SectionFacts:
    """Claim-relevant facts of one [TextureOverride…] section with a hash."""

    name: str
    hash: str
    file: Path
    start: int
    end: int
    draw_capable: bool
    pipeline: bool
    handling: str | None
    matched: bool
    overrides: dict[str, str]
    binds: dict[str, str]
    reads: frozenset[str]
    dispatch_slots: list[str]
    draw_type_dispatch: bool
    arms: frozenset[str]
    override_sites: dict[str, tuple[int, str]]


@dataclass
class _ModFacts:
    """Parsed claim facts of one enabled mod folder."""

    key: str
    name: str
    writes: frozenset[str]
    sections: list[_SectionFacts]
    resets: frozenset[str]
    decays: frozenset[str]
    strikes: frozenset[str]


@dataclass
class _ClaimWeb:
    """Shared per-hash claimant facts for row rules and spec generation."""

    names: dict[str, str]
    writes: dict[str, frozenset[str]]
    reads: dict[str, dict[str, set[str]]]
    links: dict[str, set[frozenset[str]]]


def _write_vars(items: list[tuple[str, str]]) -> frozenset[str]:
    """$var names assigned on the left of any reachable statement in a body.

    ``post``/``pre`` prefixed assignments count; with an empty environment the
    walk keeps every branch but literally-false ones, so gated writes count."""
    writes: set[str] = set()
    for statement, _gates in _walk_compiled(items, {}):
        match = _WRITE_RE.match(statement)
        if match is not None:
            writes.add(match.group(1).lower())
    return frozenset(writes)


def _arms_from_items(items: list[tuple[str, str]]) -> frozenset[str]:
    """$var names this section arms with a nonzero constant assignment."""
    arms: set[str] = set()
    for statement, _gates in _walk_compiled(items, {}):
        match = _ASSIGN_RE.match(statement)
        if match is None:
            continue
        value = match.group(2)
        if _ZERO_LITERAL_RE.match(value) is None and "$" not in value:
            arms.add(match.group(1).lower())
    return frozenset(arms)


def _strikes_from_items(items: list[tuple[str, str]]) -> frozenset[str]:
    """$var names written outside the nonzero-constant arming form in a body."""
    strikes: set[str] = set()
    for statement, _gates in _walk_compiled(items, {}):
        match = _WRITE_RE.match(statement)
        if match is None:
            continue
        assign = _ASSIGN_RE.match(statement)
        if (
            assign is not None
            and _ZERO_LITERAL_RE.match(assign.group(2)) is None
            and "$" not in assign.group(2)
        ):
            continue
        strikes.add(match.group(1).lower())
    return frozenset(strikes)


def _override_sites(body: list[tuple[int, str]]) -> dict[str, tuple[int, str]]:
    """Per override key, the last assignment's (line number, original line)."""
    sites: dict[str, tuple[int, str]] = {}
    for line_no, content in body:
        statement = _normalize_statement(content)
        if statement is None:
            continue
        match = _OVERRIDE_RE.match(statement)
        if match is not None:
            sites[match.group(1)] = (line_no, content)
    return sites


def _facts_from_items(
    file: Path,
    name: str,
    hash_value: str,
    start: int,
    end: int,
    body: list[tuple[int, str]],
    items: list[tuple[str, str]],
) -> _SectionFacts:
    """Claim-relevant facts of one TextureOverride section from compiled items."""
    reads: set[str] = set()
    draws = False
    handling: str | None = None
    first_index = False
    index_count = False
    overrides: dict[str, str] = {}
    binds: dict[str, str] = {}
    slots: list[str] = []
    draw_type = False
    for kind, payload in items:
        if kind in ("if", "elif"):
            reads.update(
                match.group(1).lower() for match in _RAW_VAR_RE.finditer(payload)
            )
            draw_type = draw_type or _DRAW_TYPE_RE.search(payload) is not None
            continue
        if kind != "stmt":
            continue
        if _DRAW_TYPE_RE.search(payload) is not None:
            draw_type = True
        if payload.startswith("draw"):
            draws = True
        match = _HANDLING_RE.match(payload)
        if match is not None:
            handling = match.group(1)
            continue
        if _FIRST_INDEX_RE.match(payload) is not None:
            first_index = True
            continue
        if _INDEX_COUNT_RE.match(payload) is not None:
            index_count = True
            continue
        match = _CTO_RE.match(payload)
        if match is not None:
            slots.append(match.group(1).lower())
            continue
        match = _OVERRIDE_RE.match(payload)
        if match is not None:
            overrides[match.group(1)] = match.group(2).strip()
            continue
        match = _BIND_RE.match(payload)
        if match is not None:
            binds[match.group(1)] = match.group(2).strip()
    return _SectionFacts(
        name=name,
        hash=hash_value,
        file=file,
        start=start,
        end=end,
        draw_capable=draws or handling is not None or "ib" in binds,
        pipeline=draws or handling is not None or bool(binds),
        handling=handling,
        matched=first_index and index_count,
        overrides=overrides,
        binds=binds,
        reads=frozenset(reads),
        dispatch_slots=slots,
        draw_type_dispatch=draw_type,
        arms=_arms_from_items(items),
        override_sites=_override_sites(body),
    )


def _mod_facts(folder: Path, files: Sequence[Path]) -> _ModFacts:
    """Parse one mod folder's ini files into per-section facts and $var writes."""
    sections: list[_SectionFacts] = []
    writes: set[str] = set()
    resets: set[str] = set()
    decays: set[str] = set()
    strikes: set[str] = set()
    for path in files:
        try:
            parsed = cached_ini_parse(path)
        except ValueError:
            continue
        if parsed is None:
            continue
        for raw in parsed[2]:
            items = _compile_body([content for _line, content in raw.body])
            writes.update(_write_vars(items))
            if raw.name.strip().lower() == "present":
                for statement, _gates in _walk_compiled(items, {}):
                    match = _ASSIGN_RE.match(statement)
                    if (
                        match is not None
                        and _ZERO_LITERAL_RE.match(match.group(2)) is not None
                    ):
                        resets.add(match.group(1).lower())
                    match = _DECREMENT_RE.match(statement)
                    if match is None:
                        match = _OP_DECREMENT_RE.match(statement)
                    if match is not None:
                        decays.add(match.group(1).lower())
                continue
            strikes.update(_strikes_from_items(items))
            name = raw.name.strip()
            if not name.lower().startswith(_TEXTURE_OVERRIDE_PREFIX):
                continue
            if not raw.hashes:
                continue
            for _line, hash_value in raw.hashes:
                sections.append(
                    _facts_from_items(
                        path, name, hash_value, raw.start, raw.end, raw.body, items
                    )
                )
    return _ModFacts(
        key=str(folder),
        name=folder.name,
        writes=frozenset(writes),
        sections=sections,
        resets=frozenset(resets),
        decays=frozenset(decays),
        strikes=frozenset(strikes),
    )


def _mod_files(root: Path) -> list[tuple[Path, list[Path]]]:
    """Group every enabled mod's ini files by their holding folder.

    A mod is a folder directly holding included .ini files; DISABLED_-prefixed
    directories at any depth and DISABLED_-prefixed ini filenames are skipped."""
    groups: dict[Path, list[Path]] = {}
    for path in included_ini_files(root):
        rel = path.relative_to(root)
        if rel.parts[-1].startswith(_DISABLED_PREFIX):
            continue
        if any(part.startswith(_DISABLED_PREFIX) for part in rel.parts[:-1]):
            continue
        groups.setdefault(path.parent, []).append(path)
    return sorted(groups.items(), key=lambda item: str(item[0]))


def _mod_facts_quietly(folder: Path, files: Sequence[Path]) -> _ModFacts | None:
    """One mod's facts, or None when its parsing fails for any reason."""
    try:
        return _mod_facts(folder, files)
    except Exception:  # noqa: BLE001
        return None


def _load_mods(root: Path) -> list[_ModFacts]:
    """Parse every enabled mod; a failing mod contributes nothing."""
    mods: list[_ModFacts] = []
    for folder, files in _mod_files(root):
        facts = _mod_facts_quietly(folder, files)
        if facts is not None:
            mods.append(facts)
    return mods


def _longest_common_prefix(names: Sequence[str]) -> str:
    """Longest common leading substring of the names ("" when there is none)."""
    if not names:
        return ""
    prefix = names[0]
    for name in names[1:]:
        while prefix and not name.startswith(prefix):
            prefix = prefix[:-1]
        if not prefix:
            break
    return prefix


def _quote_join(names: Iterable[str]) -> str:
    """Join names as 'a' and 'b', or 'a', 'b' and 'c' for three and more."""
    quoted = [f"'{name}'" for name in names]
    if len(quoted) <= 2:
        return " and ".join(quoted)
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def _claimant_reads(mods: Sequence[_ModFacts]) -> dict[str, dict[str, set[str]]]:
    """Per hash, the draw-capable claimant mod keys and the $vars they read."""
    reads: dict[str, dict[str, set[str]]] = {}
    for mod in mods:
        for section in mod.sections:
            if not section.draw_capable:
                continue
            claimants = reads.setdefault(section.hash, {})
            claimants.setdefault(mod.key, set()).update(section.reads)
    return reads


def _claim_web(mods: Sequence[_ModFacts]) -> _ClaimWeb:
    """Claimant reads plus, per hash, the interlocked mod-key pair links.

    A pair links only when each mod reads a $var the other writes on that
    hash's draw-capable sections; the map feeds rows and spec generation."""
    names = {mod.key: mod.name for mod in mods}
    writes = {mod.key: mod.writes for mod in mods}
    reads = _claimant_reads(mods)
    links: dict[str, set[frozenset[str]]] = {}
    for hash_value, claimants in reads.items():
        ordered = sorted(claimants, key=lambda key: (names[key], key))
        for first, second in combinations(ordered, 2):
            first_defers = bool(
                claimants[first] & writes.get(second, frozenset())
            )
            second_defers = bool(
                claimants[second] & writes.get(first, frozenset())
            )
            if first_defers and second_defers:
                links.setdefault(hash_value, set()).add(frozenset((first, second)))
    return _ClaimWeb(names=names, writes=writes, reads=reads, links=links)


def _outfit_flag(writer: _ModFacts, holder: _ModFacts) -> str | None:
    """The writer's frame-stable outfit flag for deferral wraps, or None: armed
    with a nonzero constant only in hash-bearing pipeline sections the holder does
    not hold and reset to literal zero only in [Present]; mid-frame re-assigned
    flags disqualify, and the first survivor is picked."""
    holder_hashes = {section.hash for section in holder.sections}
    eligible = {
        var
        for var in writer.resets
        if var not in writer.decays and var not in writer.strikes
    }
    armed: dict[str, list[_SectionFacts]] = {}
    for section in writer.sections:
        if section.hash in holder_hashes:
            continue
        for var in section.arms:
            armed.setdefault(var, []).append(section)
    candidates = {
        var
        for var, sites in armed.items()
        if var in eligible and all(site.pipeline for site in sites)
    }
    return min(candidates) if candidates else None


def _defer_specs(
    mod: _ModFacts, hash_value: str, flag: str, other: str
) -> list[DeferWrapSpec]:
    """Wrap specs deferring one mod's draw-capable sections on a shared hash."""
    return [
        DeferWrapSpec(
            file=section.file,
            mod=mod.name,
            section=section.name,
            start=section.start,
            end=section.end,
            flag=flag,
            other=other,
        )
        for section in mod.sections
        if section.hash == hash_value
        and section.draw_capable
        and flag not in section.reads
    ]


def _co_claim_rows(
    mods: Sequence[_ModFacts], web: _ClaimWeb
) -> tuple[list[ClaimConflict], list[DeferWrapSpec]]:
    """Co-claim rows plus the wrap specs that would defer missing directions."""
    by_key = {mod.key: mod for mod in mods}
    rows: list[ClaimConflict] = []
    wraps: list[DeferWrapSpec] = []
    for hash_value, claimants in sorted(web.reads.items()):
        if len(claimants) < 2:
            continue
        ordered = sorted(claimants, key=lambda key: (web.names[key], key))
        for first, second in combinations(ordered, 2):
            first_defers = bool(
                claimants[first] & web.writes.get(second, frozenset())
            )
            second_defers = bool(
                claimants[second] & web.writes.get(first, frozenset())
            )
            if first_defers and second_defers:
                continue
            first_name = web.names[first]
            second_name = web.names[second]
            if first_defers:
                link = (
                    f"'{first_name}' defers to '{second_name}' but "
                    f"'{second_name}' does not defer to '{first_name}'"
                )
            elif second_defers:
                link = (
                    f"'{second_name}' defers to '{first_name}' but "
                    f"'{first_name}' does not defer to '{second_name}'"
                )
            else:
                link = "neither defers to the other"
            solvable = True
            if not first_defers:
                flag = _outfit_flag(by_key[second], by_key[first])
                if flag is None:
                    solvable = False
                else:
                    wraps.extend(
                        _defer_specs(by_key[first], hash_value, flag, second_name)
                    )
            if not second_defers:
                flag = _outfit_flag(by_key[first], by_key[second])
                if flag is None:
                    solvable = False
                else:
                    wraps.extend(
                        _defer_specs(by_key[second], hash_value, flag, first_name)
                    )
            text = (
                f"'{first_name}' and '{second_name}' both claim the draw "
                f"for hash {hash_value} and {link}, so both sections can "
                "run on the same draw and double-render it."
            )
            if solvable:
                text += _SOLVABLE_SUFFIX
            rows.append(
                ClaimConflict(
                    kind="co-claim",
                    hash=hash_value,
                    mods=[first_name, second_name],
                    text=text,
                    severity="conflict",
                    solvable=solvable,
                )
            )
    return rows, wraps


def _pairwise_linked(
    keys: Sequence[str], hash_value: str, links: dict[str, set[frozenset[str]]]
) -> bool:
    """Whether every key pair among keys is interlocked on the hash."""
    group = links.get(hash_value, set())
    return all(frozenset(pair) in group for pair in combinations(keys, 2))


def _unify_spec(
    mod: str, section: _SectionFacts, target: int, others: list[str]
) -> UnifySpec | None:
    """One vertex-count rewrite spec for a mod's line, or None when unmatched."""
    site = section.override_sites.get(_UNIFY_KEY)
    if site is None:
        return None
    line_no, content = site
    match = _UNIFY_PREFIX_RE.match(content)
    if match is None:
        return None
    return UnifySpec(
        file=section.file,
        mod=mod,
        section=section.name,
        line_no=line_no,
        old=content,
        new=f"{match.group(0)}{target}",
        others=others,
    )


def _unify_specs(
    per_mod: dict[str, tuple[str, _SectionFacts]],
    ordered: Sequence[str],
    names: dict[str, str],
) -> list[UnifySpec]:
    """Specs rewriting a group's below-max vertex counts, [] when unsure."""
    counts: dict[str, int] = {}
    for mod_key in ordered:
        try:
            counts[mod_key] = int(per_mod[mod_key][0], 10)
        except ValueError:
            return []
    target = max(counts.values())
    specs: list[UnifySpec] = []
    for mod_key, value in counts.items():
        if value == target:
            continue
        others = [names[key] for key in ordered if key != mod_key]
        spec = _unify_spec(names[mod_key], per_mod[mod_key][1], target, others)
        if spec is None:
            return []
        specs.append(spec)
    return specs


def _stomp_rows(
    mods: Sequence[_ModFacts], web: _ClaimWeb
) -> tuple[list[ClaimConflict], list[UnifySpec]]:
    """Stomp rows (interlocked groups suppressed) plus vertex-limit unify specs."""
    assigns: dict[str, dict[str, dict[str, tuple[str, _SectionFacts]]]] = {}
    for mod in mods:
        for section in mod.sections:
            facts = dict(section.overrides)
            facts.update(section.binds)
            if section.handling is not None:
                facts["handling"] = section.handling
            for key, value in facts.items():
                per_mod = assigns.setdefault(section.hash, {}).setdefault(key, {})
                per_mod[mod.key] = (value, section)
    rows: list[ClaimConflict] = []
    unifies: list[UnifySpec] = []
    for hash_value, per_key in sorted(assigns.items()):
        for key, per_mod in sorted(per_key.items()):
            if len(per_mod) < 2 or len({entry[0] for entry in per_mod.values()}) < 2:
                continue
            ordered = sorted(
                per_mod, key=lambda mod_key: (web.names[mod_key], mod_key)
            )
            if _pairwise_linked(ordered, hash_value, web.links):
                continue
            values: list[str] = []
            for mod_key in ordered:
                if per_mod[mod_key][0] not in values:
                    values.append(per_mod[mod_key][0])
            who = _quote_join(web.names[mod_key] for mod_key in ordered)
            solvable = False
            if key == _UNIFY_KEY:
                specs = _unify_specs(per_mod, ordered, web.names)
                if specs:
                    solvable = True
                    unifies.extend(specs)
            text = (
                f"{who} assign {key} to different values "
                f"({' vs '.join(values)}) on hash {hash_value}; the "
                "last-loaded mod wins."
            )
            if solvable:
                text += _SOLVABLE_SUFFIX
            rows.append(
                ClaimConflict(
                    kind="stomp",
                    hash=hash_value,
                    mods=[web.names[mod_key] for mod_key in ordered],
                    text=text,
                    severity="conflict",
                    solvable=solvable,
                )
            )
    return rows, unifies


def _dispatch_rows(mods: Sequence[_ModFacts]) -> list[ClaimConflict]:
    """Rows for mods dispatching a hash that another enabled mod also holds."""
    names = {mod.key: mod.name for mod in mods}
    owners: dict[str, set[str]] = {}
    for mod in mods:
        for section in mod.sections:
            if section.hash:
                owners.setdefault(section.hash, set()).add(mod.key)
    rows: list[ClaimConflict] = []
    for mod in mods:
        hashes = sorted(
            {section.hash for section in mod.sections if section.dispatch_slots}
        )
        for hash_value in hashes:
            others = sorted(
                (names[key], key) for key in owners.get(hash_value, set()) - {mod.key}
            )
            if not others:
                continue
            involved = sorted({(mod.name, mod.key), *others})
            other_names = _quote_join(name for name, _key in others)
            rows.append(
                ClaimConflict(
                    kind="dispatch",
                    hash=hash_value,
                    mods=[name for name, _key in involved],
                    text=(
                        f"'{mod.name}' dispatches hash {hash_value} via "
                        "checktextureoverride, which follows the currently bound "
                        "slot instead of a fixed resource; other mods claiming "
                        f"this hash ({other_names}) can capture the draw."
                    ),
                    severity="conflict",
                )
            )
    return rows


def _draw_type_in_cluster(mod: _ModFacts) -> bool:
    """Whether any section sharing the mod's name stem dispatches by DRAW_TYPE.

    The stem is the longest common leading part of the mod's TextureOverride names; a
    generic stem counts as unsure, and unsure mods conservatively scan every section."""
    names = [section.name.lower() for section in mod.sections]
    stem = _longest_common_prefix(names)
    if len(stem) <= len(_GENERIC_STEM):
        return any(section.draw_type_dispatch for section in mod.sections)
    return any(
        section.draw_type_dispatch
        for section in mod.sections
        if section.name.lower().startswith(stem)
    )


def _fixable_rows(mods: Sequence[_ModFacts]) -> list[ClaimConflict]:
    """Info rows for mods whose dispatch hair pattern the Fix action can convert."""
    rows: list[ClaimConflict] = []
    for mod in mods:
        if not any("ib" in section.dispatch_slots for section in mod.sections):
            continue
        claim = next(
            (
                section
                for section in mod.sections
                if section.hash and section.matched and section.handling == "skip"
            ),
            None,
        )
        if claim is None or _draw_type_in_cluster(mod):
            continue
        rows.append(
            ClaimConflict(
                kind="fixable",
                hash=claim.hash,
                mods=[mod.name],
                text=(
                    f"'{mod.name}' uses the dynamic dispatch hair pattern; the "
                    "Fix action can convert it to a self-contained draw-gated "
                    "claim."
                ),
                severity="info",
            )
        )
    return rows


def _deference_analysis(
    root: Path,
) -> tuple[list[ClaimConflict], list[DeferWrapSpec], list[UnifySpec]]:
    """Sorted rows plus defer-wrap and unify specs for the enabled mods."""
    mods = _load_mods(root)
    web = _claim_web(mods)
    co_rows, wraps = _co_claim_rows(mods, web)
    stomp_rows, unifies = _stomp_rows(mods, web)
    rows = [
        *co_rows,
        *stomp_rows,
        *_dispatch_rows(mods),
        *_fixable_rows(mods),
    ]
    rows.sort(key=lambda row: (row.hash, row.kind, tuple(row.mods), row.text))
    wraps.sort(key=lambda spec: (str(spec.file), spec.section, spec.start, spec.flag))
    unifies.sort(key=lambda spec: (str(spec.file), spec.line_no))
    return rows, wraps, unifies


def compute_claim_conflicts(
    mods_root: str, mod_dir: str | None = None
) -> list[ClaimConflict]:
    """Claim-conflict and fixable-pattern rows for the enabled mods under mods_root.

    Rows sort by hash, kind then mods; with mod_dir given only rows involving
    that mod folder name (case-insensitive) survive; failures never raise."""
    try:
        root = Path(mods_root)
        if not root.is_dir():
            return []
        rows, _wraps, _unifies = _deference_analysis(root)
        if mod_dir:
            name = Path(mod_dir).name.casefold()
            rows = [row for row in rows if name in {m.casefold() for m in row.mods}]
        return rows
    except Exception:  # noqa: BLE001
        return []


def deference_specs(
    mods_root: str,
) -> tuple[list[DeferWrapSpec], list[UnifySpec], list[ClaimConflict]]:
    """Defer-wrap and unify specs plus the current conflict rows for mods_root.

    The rows carry solvability flags and suppressions; the specs list the wrap
    and rewrite edits the Fix action can apply; failures never raise."""
    try:
        root = Path(mods_root)
        if not root.is_dir():
            return [], [], []
        rows, wraps, unifies = _deference_analysis(root)
        return wraps, unifies, rows
    except Exception:  # noqa: BLE001
        return [], [], []
