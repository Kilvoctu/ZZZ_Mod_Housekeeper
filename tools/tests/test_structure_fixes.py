"""Structural scan/apply tests for tools.fixer + tools.structure, synthetic only.

All hashes are lowercase 8-hex stand-ins for the spec's placeholders because
both consumers require the ^[0-9a-fA-F]{8}$ regex shape.
"""

from pathlib import Path

from tools.characters import CharacterDB
from tools.fixer import (
    STRUCTURAL_KINDS,
    FixerData,
    _scan_file_details,
    apply_plan,
    scan_files,
)
from tools.model import Character, Component
from tools.structure import StructureData, build_structure
from tools.ui.worker import structure_for

CHAR_NAME = "角色甲CharaA"

VB1, VB2, VB3, VB4 = "aa000001", "aa000002", "aa000003", "aa000004"
VB5 = "aa000005"
IBHAIR, IBBODY = "bb000001", "bb000002"
IBW0, IBW1 = "cc000006", "cc00000a"
VL0, VL1 = "cc000001", "cc000002"
PB0, TB0, BB0 = "cc000003", "cc000004", "cc000005"
PB1, TB1, BB1 = "cc000007", "cc000008", "cc000009"

LEGACY = "deadbeef"

D2048, D1024 = "d2048aaa", "d1024bbb"
NM2048, NM1024 = "0a2048aa", "0a1024bb"
LM2048, LM1024 = "1a2048aa", "1a1024bb"
BD2048, BD1024 = "ee2048aa", "ee1024cc"
EXA2048, EXA1024 = "ea2048aa", "ea1024bb"
EXB2048, EXB1024 = "eb2048aa", "eb1024bb"
WT2048 = "fade2048"


def make_db(variant: str) -> CharacterDB:
    """The synthetic table for one resolution variant."""
    if variant == "2048p":
        hair_textures = [
            [["Diffuse", ".dds", D2048], ["NormalMap", ".dds", NM2048], ["LightMap", ".dds", LM2048]]
        ]
        body_textures = [[["Diffuse", ".dds", BD2048], ["NormalMap", ".dds", NM2048]]]
        extras_textures = [[["Diffuse", ".dds", EXA2048], ["Diffuse", ".dds", EXB2048]]]
    else:
        hair_textures = [
            [["Diffuse", ".dds", D1024], ["NormalMap", ".dds", NM1024], ["LightMap", ".dds", LM1024]]
        ]
        body_textures = [[["Diffuse", ".dds", BD1024], ["NormalMap", ".dds", NM1024]]]
        extras_textures = [[["Diffuse", ".dds", EXA1024], ["Diffuse", ".dds", EXB1024]]]
    weapon_textures = [[["Diffuse", ".dds", WT2048]]]
    character = Character(
        name=CHAR_NAME,
        components=[
            Component(
                name="Hair-头发",
                fields={
                    "draw_vb": VB1,
                    "position_vb": VB2,
                    "texcoord_vb": VB3,
                    "blend_vb": VB4,
                    "ib": IBHAIR,
                },
                texture_hashes=hair_textures,
            ),
            Component(
                name="Body-身体",
                fields={"draw_vb": VB5, "ib": IBBODY},
                texture_hashes=body_textures,
            ),
            Component(
                name="Extras-配饰",
                fields={},
                texture_hashes=extras_textures,
            ),
            Component(
                name="weapon武器变形后-枪身",
                fields={"vertexlimit": VL0, "position": PB0, "texcoord": TB0, "blend": BB0, "ib": IBW0},
                texture_hashes=weapon_textures,
            ),
            Component(
                name="weapon武器-枪把",
                fields={"vertexlimit": VL1, "position": PB1, "texcoord": TB1, "blend": BB1, "ib": IBW1},
                texture_hashes=weapon_textures,
            ),
        ],
    )
    db = CharacterDB()
    db.characters[character.name] = character
    return db


DB_2048 = make_db("2048p")
DB_1024 = make_db("1024p")
STRUCTURE = build_structure(
    {"2048p": DB_2048, "1024p": DB_1024}, chain_pairs=[(LEGACY, IBBODY)]
)


def make_data(db: CharacterDB | None = None) -> FixerData:
    if db is None:
        db = CharacterDB()
    return FixerData(chains={}, ib_index_changes={}, db=db, entries=[])


def ini_bytes(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def write_ini(directory: Path, lines: list[str], name: str = "Mod.ini") -> Path:
    path = Path(directory) / name
    path.write_bytes(ini_bytes(lines))
    return path


def structural_suggestions(
    directory: Path,
    lines: list[str],
    name: str = "Mod.ini",
    structure: StructureData = STRUCTURE,
):
    path = write_ini(directory, lines, name)
    _text, _encoding, suggestions = _scan_file_details(path, make_data(), structure)
    return suggestions


def test_insert_run_indexed(tmp_path):
    suggestions = structural_suggestions(
        tmp_path,
        ["[TextureOverrideCharaA.Hair]", f"hash = {IBHAIR}", "match_first_index = 0"],
    )
    assert len(suggestions) == 1
    run = suggestions[0]
    assert run.kind == "insert_run"
    assert run.section == "TextureOverrideCharaA.Hair"
    assert run.line_no == 3
    assert run.after_line == 3
    assert run.insert_text == "run = CommandListSkinTexture"
    assert run.old == IBHAIR
    assert run.new == IBHAIR


def test_insert_run_unindexed_fallback(tmp_path):
    suggestions = structural_suggestions(
        tmp_path, ["[TextureOverrideCharaA.Hair]", f"hash = {IBHAIR}"]
    )
    assert len(suggestions) == 1
    run = suggestions[0]
    assert run.kind == "insert_run"
    assert run.after_line == 2
    assert run.insert_text == "run = CommandListSkinTexture"


def test_insert_run_suppressed(tmp_path):
    suggestions = structural_suggestions(
        tmp_path,
        ["[TextureOverrideCharaA.Hair]", f"hash = {IBHAIR}", "run = CommandListSkinTexture"],
    )
    assert suggestions == []


def test_insert_run_suppressed_by_custom_command_list(tmp_path):
    indexed = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Hair]",
            f"hash = {IBHAIR}",
            "match_first_index = 0",
            "run = CommandList\\ZZMI\\SetTextures",
        ],
    )
    assert indexed == []
    unindexed = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Hair]",
            f"hash = {IBHAIR}",
            "run = CommandListBodyADiffuse",
        ],
        name="Unindexed.ini",
    )
    assert unindexed == []


def test_insert_run_indexed_covers_unindexed_fallback(tmp_path):
    suggestions = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Hair]",
            f"hash = {IBHAIR}",
            "match_first_index = 0",
            "run = CommandListSkinTexture",
            "[TextureOverrideCharaA.Extras]",
            f"hash = {IBHAIR}",
        ],
    )
    assert not any(s.kind == "insert_run" for s in suggestions)
    assert suggestions == []


def test_anchor_add_section(tmp_path):
    suggestions = structural_suggestions(
        tmp_path, ["[TextureOverrideCharaA.Hair]", f"hash = {VB1}"]
    )
    assert len(suggestions) == 1
    anchor = suggestions[0]
    assert anchor.kind == "add_section"
    assert anchor.section == "TextureOverrideCharaA.Hair"
    assert anchor.after_line == 2
    assert anchor.old == VB1
    assert anchor.new == IBHAIR
    assert anchor.insert_text == (
        f"\n[TextureOverrideCharaA.Hair.IB]\nhash = {IBHAIR}\nmatch_priority = 0"
    )

    suppressed = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Hair]",
            f"hash = {VB1}",
            "[TextureOverrideCharaA.Hair.IB]",
            f"hash = {IBHAIR}",
        ],
        name="Suppressed.ini",
    )
    assert not any(s.kind == "add_section" for s in suppressed)


def test_anchor_gate_chain_alias(tmp_path):
    control = structural_suggestions(
        tmp_path, ["[TextureOverrideCharaA.Body]", f"hash = {VB5}"], name="Control.ini"
    )
    anchors = [s for s in control if s.kind == "add_section" and s.new == IBBODY]
    assert len(anchors) == 1
    assert anchors[0].insert_text == (
        f"\n[TextureOverrideCharaA.Body.IB]\nhash = {IBBODY}\nmatch_priority = 0"
    )

    gated = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Body]",
            f"hash = {VB5}",
            "[TextureOverrideCharaA.Misc]",
            f"hash = {LEGACY}",
        ],
        name="Gated.ini",
    )
    assert not any(s.kind == "add_section" and s.new == IBBODY for s in gated)


def test_multiply_section(tmp_path):
    suggestions = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Hair]",
            f"hash = {D2048}",
            "match_priority = 5",
            "ps-t0 = resource=x.0.dds",
        ],
    )
    multiples = [s for s in suggestions if s.kind == "multiply_section"]
    assert len(multiples) == 1
    multiply = multiples[0]
    assert multiply.section == "TextureOverrideCharaA.Hair"
    assert multiply.line_no == 4
    assert multiply.after_line == 4
    assert multiply.old == D2048
    assert multiply.new == D1024
    assert multiply.insert_text == (
        f"\n[TextureOverrideCharaA.HairA.Diffuse.1024]\nhash = {D1024}"
        "\nmatch_priority = 5\nps-t0 = resource=x.0.dds"
    )

    suppressed = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Hair]",
            f"hash = {D2048}",
            "match_priority = 5",
            "ps-t0 = resource=x.0.dds",
            "[TextureOverrideCharaA.Hair.1024]",
            f"hash = {D1024}",
        ],
        name="Suppressed.ini",
    )
    assert not any(s.kind == "multiply_section" for s in suppressed)


def test_multiply_titles_use_slot_letters():
    expected = {
        D2048: "CharaA.HairA.Diffuse.1024",
        BD2048: "CharaA.BodyA.Diffuse.1024",
        EXA2048: "CharaA.ExtrasA.Diffuse.1024",
        EXB2048: "CharaA.ExtrasB.Diffuse.1024",
    }
    for hash_value, title in expected.items():
        multiples = [
            m for m in STRUCTURE.rules_by_hash[hash_value].multiples if m.char == "CharaA"
        ]
        assert len(multiples) == 1
        assert multiples[0].section_title == title


def test_shared_normalmap_gate(tmp_path):
    rules = STRUCTURE.rules_by_hash[NM2048]
    assert len(rules.shared_normalmap) == 1
    shared = rules.shared_normalmap[0]
    assert shared.ibs == tuple(sorted((IBHAIR, IBBODY)))
    assert shared.section_title == "CharaA.Shared.NormalMap.2048"

    suggestions = structural_suggestions(
        tmp_path, ["[TextureOverrideCharaA.Hair]", f"hash = {NM2048}"]
    )
    anchors = [s for s in suggestions if s.kind == "add_section" and s.new == shared.ibs[0]]
    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor.after_line == 2
    assert anchor.insert_text == (
        f"\n[TextureOverrideCharaA.Shared.NormalMap.2048]\nhash = {shared.ibs[0]}"
        "\nmatch_priority = 0"
    )

    suppressed = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaA.Hair]",
            f"hash = {NM2048}",
            "[TextureOverrideCharaA.Body.IB]",
            f"hash = {IBBODY}",
        ],
        name="Suppressed.ini",
    )
    assert not any(
        s.kind == "add_section" and s.new == shared.ibs[0] for s in suppressed
    )


def test_char_fallback_by_containment(tmp_path):
    def two_char_db(variant: str) -> CharacterDB:
        db = make_db(variant)
        base = db.characters[CHAR_NAME]
        db.characters["Chara"] = Character(name="角色Chara", components=base.components)
        return db

    structure = build_structure(
        {"2048p": two_char_db("2048p"), "1024p": two_char_db("1024p")},
        chain_pairs=[(LEGACY, IBBODY)],
    )
    shared = next(
        rule
        for rule in structure.rules_by_hash[NM2048].shared_normalmap
        if rule.char == "CharaA"
    )
    assert shared.ibs == tuple(sorted((IBHAIR, IBBODY)))

    suggestions = structural_suggestions(
        tmp_path,
        [
            "[TextureOverrideCharaAHairANormalMap]",
            f"hash = {NM2048}",
            "this = ResourceCharaAHairANormalMap",
        ],
        name="NormalMap.ini",
        structure=structure,
    )
    add_sections = [s for s in suggestions if s.kind == "add_section"]
    assert len(add_sections) == 1
    anchor = add_sections[0]
    assert anchor.section == "TextureOverrideCharaAHairANormalMap"
    assert anchor.new == shared.ibs[0] == IBHAIR
    assert anchor.insert_text == (
        f"\n[TextureOverrideCharaA.Shared.NormalMap.2048]\nhash = {IBHAIR}"
        "\nmatch_priority = 0"
    )

    companion = structural_suggestions(
        tmp_path,
        ["[TextureOverrideCharaAHair]", f"hash = {IBHAIR}"],
        name="Hair.ini",
        structure=structure,
    )
    assert not any(s.kind == "add_section" for s in companion)

    ambiguous = structural_suggestions(
        tmp_path,
        ["[TextureOverrideFooBarBaz]", f"hash = {NM2048}"],
        name="FooBarBaz.ini",
        structure=structure,
    )
    assert ambiguous == []


def test_weapon_only_texture(tmp_path):
    rules = STRUCTURE.rules_by_hash[WT2048]
    assert len(rules.anchors) == 1
    anchor_rule = rules.anchors[0]
    assert anchor_rule.kind == "weapon"
    assert anchor_rule.ib == IBW0
    assert anchor_rule.section_title == "CharaA.weapon.IB"
    assert rules.multiples == []

    suggestions = structural_suggestions(
        tmp_path, ["[TextureOverrideCharaA.weapon]", f"hash = {WT2048}"]
    )
    assert len(suggestions) == 1
    anchor = suggestions[0]
    assert anchor.kind == "add_section"
    assert anchor.insert_text == (
        f"\n[TextureOverrideCharaA.weapon.IB]\nhash = {IBW0}\nmatch_priority = 0"
    )


def test_apply_plan_end_to_end(tmp_path):
    store = tmp_path / "store"
    mods = tmp_path / "mods"
    mods.mkdir()
    lines = ["[TextureOverrideCharaA.Hair]", f"hash = {VB1}"]
    path = write_ini(mods, lines)
    original = ini_bytes(lines)
    data = make_data()

    plans = scan_files([path], data, STRUCTURE)
    assert len(plans) == 1
    assert [s.kind for s in plans[0].suggestions] == ["add_section"]
    logs: list[str] = []

    def apply() -> bool:
        return apply_plan(
            plans[0],
            data,
            store_dir=store,
            mods_dir=mods,
            log=logs.append,
            structure=STRUCTURE,
        )

    assert apply() is True

    fixed = path.read_bytes()
    assert b"\r\n[TextureOverrideCharaA.Hair.IB]\r\nhash = " + IBHAIR.encode() + b"\r\nmatch_priority = 0\r\n" in fixed
    assert fixed.count(b"\r\n") == fixed.count(b"\n")
    assert b"\r\n[TextureOverride" in fixed
    assert fixed.startswith(original)
    assert len(list(store.rglob("*.bak"))) == 1

    assert apply() is True
    assert b"\r\nrun = CommandListSkinTexture\r\n" in path.read_bytes()
    assert len(list(store.rglob("*.bak"))) == 2
    assert apply() is False
    assert scan_files([path], data, STRUCTURE) == []


def test_fixpoint_passes_back_up_once(tmp_path):
    store = tmp_path / "store"
    mods = tmp_path / "mods"
    mods.mkdir()
    lines = ["[TextureOverrideCharaA.Hair]", f"hash = {VB1}"]
    path = write_ini(mods, lines)
    original = ini_bytes(lines)
    data = make_data()

    backed_up: set[str] = set()
    logs: list[str] = []
    for _pass in range(4):
        plans = scan_files([path], data, STRUCTURE)
        if not plans:
            break
        for plan in plans:
            if apply_plan(
                plan,
                data,
                store_dir=store,
                mods_dir=mods,
                log=logs.append,
                structure=STRUCTURE,
                backup=plan.path not in backed_up,
            ):
                backed_up.add(plan.path)

    assert scan_files([path], data, STRUCTURE) == []
    stored = list(store.rglob("*.bak"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == original


def test_scan_without_structure_has_no_structural_kinds(tmp_path):
    path = write_ini(tmp_path, ["[TextureOverrideCharaA.Hair]", f"hash = {VB1}"])
    plans = scan_files([path], make_data())
    for plan in plans:
        for suggestion in plan.suggestions:
            assert suggestion.kind not in STRUCTURAL_KINDS


def test_structure_for_worker():
    assert structure_for({}) is None
    structure = structure_for(
        {"2048p": FixerData(chains={}, ib_index_changes={}, db=DB_2048, entries=[])}
    )
    assert structure is not None
    assert structure.rules_by_hash[IBBODY].is_ib is True
