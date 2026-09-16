"""Cross-repo structural fix knowledge derived from the character hash tables."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property

from .characters import CharacterDB, Component
from .model import iter_texture_triples

VB_ROLES = frozenset(
    {
        "draw_vb",
        "position_vb",
        "texcoord_vb",
        "blend_vb",
        "position",
        "texcoord",
        "blend",
    }
)

MULTIPLY_ROLES = frozenset({"diffuse", "lightmap", "materialmap", "normalmap"})

WEAPON_PREFIX = "weapon"

_LATIN_SUFFIX_RE = re.compile(r"[a-z0-9]+$", re.IGNORECASE)
_ASCII_KEEP_RE = re.compile(r"[^a-zA-Z0-9 ]")


def latin_suffix(value: str) -> str:
    """Trailing ASCII alphanumeric run of a raw name, e.g. "朱鸢ZhuYuan" -> "ZhuYuan"."""
    match = _LATIN_SUFFIX_RE.search(value or "")
    return match.group(0) if match else ""


def comp_latin(name: str, is_weapon: bool) -> str:
    """ASCII slug used in section titles for one component.

    Weapon components all render as "weapon"; non-weapon components use the
    ASCII part of the first dash-separated token, e.g. "Face-脸" -> "Face"."""
    if is_weapon:
        return "weapon"
    token = name.split("-", 1)[0]
    latin = _ASCII_KEEP_RE.sub("", token).strip()
    return latin or "Part"


def _component_textures(component: Component) -> list[tuple[str, str]]:
    """Flatten a component's texture_hashes into [(role, hash)] triples.

    The JSON nests empty/one-element arrays; a leaf is a list of three
    strings ``[role, extension, hash]``.  Deduplicates identical triples."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for role, hash_value in iter_texture_triples(component.texture_hashes):
        key = (role.lower(), hash_value.lower())
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


@dataclass(frozen=True)
class TextureSlot:
    """One texture bind of a component: a role with one hash per variant."""

    role: str
    hashes: dict[str, str]


@dataclass
class ComponentFacts:
    """Everything derived about one component across both variants."""

    char: str = ""
    json_name: str = ""
    comp_name: str = ""
    comp_latin: str = ""
    is_weapon: bool = False
    ib: str | None = None
    vb: frozenset[str] = frozenset()
    vertexlimit: str | None = None
    textures: list[TextureSlot] = field(default_factory=list)


@dataclass(frozen=True)
class AnchorRule:
    """Rule B: add an IB-anchor section when this hash is present and the IB is missing.

    ``for_hash`` triggers when present; ``ib`` is the section hash to gate on
    and write, with the gate expanded by the chain-alias set at scan time."""

    for_hash: str
    ib: str
    char: str
    comp_name: str
    comp_latin: str
    section_title: str
    kind: str = "anchor"


@dataclass(frozen=True)
class SharedAnchorRule:
    """Rule D: gate on a tuple of IBs and add a shared NormalMap anchor section."""

    for_hash: str
    ibs: tuple[str, ...]
    char: str
    section_title: str


@dataclass(frozen=True)
class MultiplyRule:
    """Rule C: duplicate a texture section for the sibling resolution.

    ``section_title``'s letter suffix (``HairA``/``HairB``/...) encodes the
    matched slot's 1-based ordinal among the same-role slots."""

    for_hash: str
    counterpart: str
    other_res: str
    role: str
    char: str
    comp_name: str
    comp_latin: str
    section_title: str


@dataclass
class HashRules:
    """All structural rules triggered by one hash appearing in a file."""

    is_ib: bool = False
    anchors: list[AnchorRule] = field(default_factory=list)
    shared_normalmap: list[SharedAnchorRule] = field(default_factory=list)
    multiples: list[MultiplyRule] = field(default_factory=list)


GateRule = AnchorRule | SharedAnchorRule | MultiplyRule


@dataclass
class StructureData:
    """The complete structural knowledge derived from both repos."""

    rules_by_hash: dict[str, HashRules] = field(default_factory=dict)
    chain_aliases: dict[str, frozenset[str]] = field(default_factory=dict)

    def gate(self, hashes: Iterable[str]) -> set[str]:
        """Expand a gate tuple with every chain alias of its members."""
        expanded: set[str] = set()
        for h in hashes:
            expanded.add(h)
            expanded.update(self.chain_aliases.get(h, ()))
        return expanded

    @cached_property
    def sorted_chars(self) -> list[str]:
        """All distinct rule chars, longest first so prefixes win the match."""
        chars: set[str] = set()
        for rules in self.rules_by_hash.values():
            for rule in rules.anchors:
                chars.add(rule.char)
            for rule in rules.shared_normalmap:
                chars.add(rule.char)
            for rule in rules.multiples:
                chars.add(rule.char)
        return sorted(chars, key=lambda c: (-len(c), c))

    @cached_property
    def cached_gates(self) -> dict[GateRule, frozenset[str]]:
        """Per-rule gate hashes: anchors -> {ib}, shared -> ibs, multiples -> {counterpart}."""
        gates: dict[GateRule, frozenset[str]] = {}
        for rules in self.rules_by_hash.values():
            for rule in rules.anchors:
                gates[rule] = frozenset(self.gate({rule.ib}))
            for rule in rules.shared_normalmap:
                gates[rule] = frozenset(self.gate(rule.ibs))
            for rule in rules.multiples:
                gates[rule] = frozenset(self.gate({rule.counterpart}))
        return gates

    def known_hashes(self) -> frozenset[str]:
        """Every hash our rules can classify, lowercased."""
        return frozenset(self.rules_by_hash)


def _merge_texture_slots(
    target: ComponentFacts, variant: str, pairs: Iterable[tuple[str, str]]
) -> None:
    """Merge one variant's texture pairs into the cross-variant slots.

    Slots are aligned by (role, ordinal), so the same logical texture across
    variants shares one TextureSlot holding one hash per variant."""
    existing = target.textures
    by_role: dict[str, list[TextureSlot]] = {}
    for slot in existing:
        by_role.setdefault(slot.role, []).append(slot)
    counts: dict[str, int] = {}
    for role, hash_value in pairs:
        ordinal = counts.get(role, 0)
        counts[role] = ordinal + 1
        bucket = by_role.get(role)
        if bucket and ordinal < len(bucket):
            bucket[ordinal].hashes[variant] = hash_value
        else:
            slot = TextureSlot(role=role, hashes={variant: hash_value})
            existing.append(slot)
            by_role.setdefault(role, []).append(slot)


def build_chain_aliases(pairs: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    """Map every hash to the other members of its changelog chain component.

    Chains are undirected, so a historical or later hash that appears in the
    mod is treated as the same value for gating."""
    unions: dict[str, set[str]] = {}
    for left_raw, right_raw in pairs:
        left = left_raw.lower()
        right = right_raw.lower()
        left_root = unions.setdefault(left, {left})
        right_root = unions.setdefault(right, {right})
        if left_root is not right_root:
            combined = left_root | right_root
            for member in combined:
                unions[member] = combined
    return {h: frozenset(members - {h}) for h, members in unions.items()}


def build_structure(
    dbs: Mapping[str, CharacterDB],
    variants: Iterable[str] | None = None,
    chain_pairs: Iterable[tuple[str, str]] = (),
) -> StructureData:
    """Build the structural rules from every variant's character table.

    ``dbs`` maps a variant key (e.g. "2048p") to its parsed CharacterDB;
    ``chain_pairs`` feeds the chain-alias gate expansion with changelog hash pairs."""
    variants = tuple(variants or dbs)
    merged: dict[tuple[str, str], ComponentFacts] = {}
    by_hash: dict[str, list[tuple[str, tuple[str, str], str]]] = {}
    first_weapon_ib: dict[str, str] = {}

    def note_role(
        entry_hash: str,
        entry_char: str,
        entry_key: tuple[str, str],
        entry_role: str,
    ) -> None:
        by_hash.setdefault(entry_hash, []).append((entry_char, entry_key, entry_role))

    for variant in variants:
        db = dbs[variant]
        for character in db.characters.values():
            char_latin = latin_suffix(character.name) or character.name
            for component in character.components:
                fields = component.fields
                key = (char_latin, component.name)
                is_weapon = component.name.lower().startswith(WEAPON_PREFIX)
                if (
                    is_weapon
                    and char_latin not in first_weapon_ib
                    and fields.get("ib")
                ):
                    first_weapon_ib[char_latin] = fields["ib"]
                facts = merged.get(key)
                if facts is None:
                    facts = ComponentFacts(
                        char=char_latin,
                        json_name=character.name,
                        comp_name=component.name,
                        comp_latin=comp_latin(component.name, is_weapon),
                        is_weapon=is_weapon,
                        ib=fields.get("ib"),
                        vertexlimit=fields.get("vertexlimit"),
                    )
                    merged[key] = facts
                else:
                    facts.comp_name = component.name
                    facts.json_name = character.name
                vb = set(facts.vb)
                for role in VB_ROLES:
                    value = fields.get(role)
                    if value:
                        vb.add(value)
                        note_role(value, char_latin, key, role)
                facts.vb = frozenset(vb)
                if facts.ib:
                    note_role(facts.ib, char_latin, key, "ib")
                if facts.vertexlimit:
                    note_role(facts.vertexlimit, char_latin, key, "vertexlimit")
                _merge_texture_slots(facts, variant, _component_textures(component))
                for role, hash_value in _component_textures(component):
                    note_role(hash_value, char_latin, key, role)

    aliases = build_chain_aliases(chain_pairs)
    rules: dict[str, HashRules] = {}

    for hash_value, refs in by_hash.items():
        bundle = rules.setdefault(hash_value, HashRules())
        roles = {role for _char, _key, role in refs}
        if "ib" in roles:
            bundle.is_ib = True

        seen_anchor_keys: set[tuple[str, tuple[str, str]]] = set()
        for char, key, role in refs:
            facts = merged.get(key)
            if facts is None or role not in VB_ROLES:
                continue
            if facts.ib and (char, key) not in seen_anchor_keys:
                seen_anchor_keys.add((char, key))
                bundle.anchors.append(
                    AnchorRule(
                        for_hash=hash_value,
                        ib=facts.ib,
                        char=char,
                        comp_name=facts.comp_name,
                        comp_latin=facts.comp_latin,
                        section_title=f"{char}.{facts.comp_latin}.IB",
                        kind="weapon" if facts.is_weapon else "anchor",
                    )
                )

        texture_roles = roles & (MULTIPLY_ROLES | {"normalmap"})
        if not texture_roles:
            continue

        by_char_membership: dict[str, tuple[list[tuple[str, str]], list[tuple[str, str]]]] = {}
        for char, key, role in refs:
            if role not in texture_roles:
                continue
            facts = merged.get(key)
            if facts is None:
                continue
            non_weapon, weapon = by_char_membership.setdefault(char, ([], []))
            bucket = weapon if facts.is_weapon else non_weapon
            if key not in bucket:
                bucket.append(key)

        for char, (non_weapon_keys, weapon_keys) in sorted(by_char_membership.items()):
            if not non_weapon_keys and weapon_keys:
                primary = first_weapon_ib.get(char)
                if primary:
                    bundle.anchors.append(
                        AnchorRule(
                            for_hash=hash_value,
                            ib=primary,
                            char=char,
                            comp_name=min(weapon_keys)[1],
                            comp_latin="weapon",
                            section_title=f"{char}.weapon.IB",
                            kind="weapon",
                        )
                    )
            elif len(non_weapon_keys) == 1:
                key = non_weapon_keys[0]
                facts = merged.get(key)
                if facts and facts.ib:
                    bundle.anchors.append(
                        AnchorRule(
                            for_hash=hash_value,
                            ib=facts.ib,
                            char=char,
                            comp_name=facts.comp_name,
                            comp_latin=facts.comp_latin,
                            section_title=f"{char}.{facts.comp_latin}.IB",
                        )
                    )

            if "normalmap" in texture_roles and len(non_weapon_keys) >= 2:
                found_ibs: list[str] = []
                for key in non_weapon_keys:
                    facts = merged.get(key)
                    if facts is None:
                        continue
                    ib = facts.ib
                    if ib is None or not ib:
                        continue
                    found_ibs.append(ib)
                ibs = sorted(found_ibs)
                if ibs:
                    resolution = _primary_resolution(hash_value, char, merged)
                    bundle.shared_normalmap.append(
                        SharedAnchorRule(
                            for_hash=hash_value,
                            ibs=tuple(ibs),
                            char=char,
                            section_title=f"{char}.Shared.NormalMap.{resolution}",
                        )
                    )

        for role in MULTIPLY_ROLES:
            if role not in texture_roles:
                continue
            for char, (non_weapon_keys, _weapon_keys) in sorted(by_char_membership.items()):
                if not non_weapon_keys:
                    continue
                owner_matches: list[tuple[tuple[str, str], tuple[str, str, int]]] = []
                for key in non_weapon_keys:
                    facts = merged.get(key)
                    if facts is None:
                        continue
                    sibling = _sibling(facts, role, hash_value)
                    if sibling is not None:
                        owner_matches.append((key, sibling))
                distinct_siblings: list[tuple[str, str, int]] = []
                seen_siblings: set[tuple[str, str]] = set()
                for _key, sibling in owner_matches:
                    pair = (sibling[0], sibling[1])
                    if pair not in seen_siblings:
                        seen_siblings.add(pair)
                        distinct_siblings.append(sibling)
                if len(distinct_siblings) != 1:
                    continue
                counterpart, other_res, _ordinal = distinct_siblings[0]
                if counterpart == hash_value:
                    continue
                comp_key, best_sibling = max(owner_matches, key=lambda match: match[1][2])
                facts = merged.get(comp_key)
                if facts is None:
                    continue
                display_role = role.capitalize()
                letter = chr(ord("A") + best_sibling[2] - 1)
                title = (
                    f"{char}.{facts.comp_latin}{letter}.{display_role}.{other_res}"
                    if other_res
                    else f"{char}.{facts.comp_latin}{letter}.{display_role}"
                )
                bundle.multiples.append(
                    MultiplyRule(
                        for_hash=hash_value,
                        counterpart=counterpart,
                        other_res=other_res,
                        role=role,
                        char=char,
                        comp_name=facts.comp_name,
                        comp_latin=facts.comp_latin,
                        section_title=title,
                    )
                )

    return StructureData(rules_by_hash=rules, chain_aliases=aliases)


def _sibling(
    facts: ComponentFacts, role: str, hash_value: str
) -> tuple[str, str, int] | None:
    """The sibling resolution of one texture slot, if any.

    Returns ``(other_hash, other_res, ordinal)``, with ``ordinal`` the 1-based
    index of the matched slot among the same-role slots."""
    ordinal = 0
    for slot in facts.textures:
        if slot.role != role:
            continue
        ordinal += 1
        if hash_value not in slot.hashes.values():
            continue
        for variant, h in slot.hashes.items():
            if h != hash_value:
                return h, ("2048" if variant == "2048p" else "1024"), ordinal
    return None


def _primary_resolution(
    hash_value: str,
    char: str,
    merged: Mapping[tuple[str, str], ComponentFacts],
) -> str:
    """The resolution a shared hash belongs to for one character ("2048"/"1024").

    Most characters use the 2048p shared NormalMap hash; a few low-varm-era
    tables reference the 1024p hash in their own components."""
    for facts in merged.values():
        if facts.char != char:
            continue
        for slot in facts.textures:
            for variant, h in slot.hashes.items():
                if h == hash_value:
                    return "2048" if variant == "2048p" else "1024"
    return "2048"
