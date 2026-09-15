"""Tests for tools.blend_vote: strict old→new index derivation from record-matched
mod/dump blend and position buffers, using synthetic stride-32/stride-40 buffers,
plus the tolerant spatial-grid derivation over v2 mesh dumps, the ini scan and
the marker-shared vote apply half with its grid fallback."""

import hashlib
import json
import struct
from pathlib import Path

from tools.blend_remap import BlendTables, blend_state_path
from tools.blend_vote import (
    _MIN_MATCH_RATE,
    BlendVoteTarget,
    apply_blend_vote_remap,
    derive_blend_vote_map,
    derive_grid_vote_map,
    scan_blend_vote_targets,
)
from tools.dumpdata import DumpData, DumpLayout, V2Dump


def make_position_bytes(count: int, seed: int) -> bytes:
    """`count` distinct deterministic 40-byte position records (4-byte vertex tag × 10)."""
    out = bytearray()
    for vertex in range(count):
        out += (seed + vertex).to_bytes(4, "little") * 10
    return bytes(out)


def make_blend_bytes(count: int, indices_fn) -> bytes:
    """`count` stride-32 blend records; indices_fn(vertex) supplies the 4 bone indices."""
    out = bytearray()
    for vertex in range(count):
        out += b"\x00" * 16  # weights never participate in voting
        for index in indices_fn(vertex):
            out += index.to_bytes(4, "little")
    return bytes(out)


def make_xyz_position_bytes(points) -> bytes:
    """Stride-40 position records from (x, y, z) float triples, 28 pad bytes each."""
    out = bytearray()
    for x, y, z in points:
        out += struct.pack("<3f", x, y, z)
        out += b"\x00" * 28
    return bytes(out)


def make_weighted_blend_bytes(records) -> bytes:
    """Stride-32 blend records from (weights, indices) pairs per vertex."""
    out = bytearray()
    for weights, indices in records:
        out += struct.pack("<4f", *weights)
        out += struct.pack("<4I", *indices)
    return bytes(out)


def make_v2_dump(
    root: Path, char: str, comp: str, name: str, position_vb: str, vertices
) -> V2Dump:
    """V2Dump over a written ``<name>-vb0=<position_vb>.txt`` vb0 text dump.

    Each vertex is ``((x, y, z), weights, indices)``; a vertex given as just
    ``((x, y, z),)`` ships a position line without blend lines.
    """
    folder = root / "v2" / f"{char}-{comp}"
    folder.mkdir(parents=True, exist_ok=True)
    lines = [
        "stride: 44",
        "first vertex: 0",
        f"vertex count: {len(vertices)}",
        "topology: trianglelist",
        "",
        "vertex-data:",
    ]
    for i, vertex in enumerate(vertices):
        x, y, z = vertex[0]
        lines.append(f"vb0[{i}]+0 POSITION0: {x}, {y}, {z}")
        if len(vertex) == 1:
            continue
        weights, indices = vertex[1], vertex[2]
        lines.append(
            f"vb0[{i}]+12 BLENDWEIGHTS0: "
            + ", ".join(str(weight) for weight in weights)
        )
        lines.append(
            f"vb0[{i}]+16 BLENDINDICES0: " + ", ".join(str(bone) for bone in indices)
        )
    path = folder / f"{name}-vb0={position_vb.lower()}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return V2Dump(
        char_latin=char,
        comp=comp,
        position_vb=position_vb.lower(),
        blend_vb="",
        texcoord_vb="",
        ib="",
        texcoord_format=None,
        texcoord_stride=0,
        vertex_count=len(vertices),
        vb0_path=path,
    )


def test_exact_derivation_recovers_shifted_mapping():
    count = 4
    mod_blend = make_blend_bytes(
        count, lambda v: (4 * v, 4 * v + 1, 4 * v + 2, 4 * v + 3)
    )
    dump_blend = make_blend_bytes(
        count, lambda v: (4 * v + 5, 4 * v + 6, 4 * v + 7, 4 * v + 8)
    )
    position = make_position_bytes(count, 0)

    derived, report = derive_blend_vote_map(mod_blend, position, dump_blend, position)

    assert report.ok
    assert report.refusal == ""
    assert report.total == report.matched == count
    assert report.unmatched == 0
    assert report.ambiguous == 0
    assert report.conflicts == {}
    expected = {index: index + 5 for index in range(4 * count)}
    assert derived == expected
    assert report.map == expected


def test_identity_buffers_yield_empty_map():
    blend = make_blend_bytes(3, lambda v: (v + 40, v + 41, v + 42, v + 43))
    position = make_position_bytes(3, 7)

    derived, report = derive_blend_vote_map(blend, position, blend, position)

    assert report.ok
    assert report.refusal == ""
    assert derived == {} and report.map == {}
    assert report.total == report.matched == 3
    assert report.conflicts == {}


def test_vertex_count_mismatch_refuses():
    mod_blend = make_blend_bytes(3, lambda v: (v, v, v, v))
    dump_blend = make_blend_bytes(4, lambda v: (v, v, v, v))

    derived, report = derive_blend_vote_map(
        mod_blend, make_position_bytes(3, 0), dump_blend, make_position_bytes(4, 0)
    )

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 3
    assert "differ" in report.refusal


def test_misaligned_mod_blend_refuses():
    good = make_blend_bytes(1, lambda v: (0, 1, 2, 3))
    position = make_position_bytes(1, 0)

    derived, report = derive_blend_vote_map(good[:-1], position, good, position)

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 0
    assert "not a multiple of 32" in report.refusal


def test_within_side_vcount_mismatch_refuses():
    blend3 = make_blend_bytes(3, lambda v: (v, v, v, v))
    blend4 = make_blend_bytes(4, lambda v: (v, v, v, v))
    pos3 = make_position_bytes(3, 0)
    pos4 = make_position_bytes(4, 0)

    derived, report = derive_blend_vote_map(blend3, pos4, blend3, pos3)
    assert not report.ok
    assert derived == {} and report.map == {}
    assert "mod blend has 3" in report.refusal

    derived, report = derive_blend_vote_map(blend3, pos3, blend4, pos3)
    assert not report.ok
    assert derived == {} and report.map == {}
    assert "dump blend has 4" in report.refusal


def test_low_match_rate_refuses():
    count = 4
    mod_blend = make_blend_bytes(
        count, lambda v: (4 * v, 4 * v + 1, 4 * v + 2, 4 * v + 3)
    )
    dump_blend = make_blend_bytes(
        count, lambda v: (4 * v + 5, 4 * v + 6, 4 * v + 7, 4 * v + 8)
    )
    position = make_position_bytes(count, 0)
    altered = bytearray(position)
    for vertex in range(0, count, 2):
        altered[vertex * 40] ^= 0xFF  # no dump record can match a tagged record

    derived, report = derive_blend_vote_map(
        mod_blend, bytes(altered), dump_blend, position
    )

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 4
    assert report.matched == 2
    assert report.unmatched == 2
    assert report.matched / report.total < _MIN_MATCH_RATE
    assert report.conflicts == {}
    assert "match rate" in report.refusal


def test_conflicting_votes_refuse():
    count = 2
    mod_blend = make_blend_bytes(count, lambda v: (0, v + 10, v + 20, v + 30))
    dump_blend = make_blend_bytes(count, lambda v: (5 + v, v + 10, v + 20, v + 30))
    position = make_position_bytes(count, 0)

    derived, report = derive_blend_vote_map(mod_blend, position, dump_blend, position)

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == report.matched == count
    assert report.conflicts == {0: frozenset({5, 6})}
    assert "conflicting votes" in report.refusal


def test_non_injective_unanimous_votes_refuse():
    count = 2
    mod_blend = make_blend_bytes(count, lambda v: (v, v + 10, v + 20, v + 30))
    dump_blend = make_blend_bytes(count, lambda v: (5, v + 10, v + 20, v + 30))
    position = make_position_bytes(count, 0)

    derived, report = derive_blend_vote_map(mod_blend, position, dump_blend, position)

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == report.matched == count
    assert report.conflicts == {}
    assert report.refusal == "non-injective mapping (old 0 and old 1 both map to new 5)"


def test_duplicate_dump_records_ambiguous_no_vote_and_refused():
    count = 4
    mod_blend = make_blend_bytes(count, lambda v: (v, v + 10, v + 20, v + 30))
    dump_blend = make_blend_bytes(count, lambda v: (v + 5, v + 10, v + 20, v + 30))
    position = make_position_bytes(count, 0)
    # dump vertex 3 copies vertex 1's position record
    duped = position[: 3 * 40] + position[1 * 40 : 2 * 40]

    derived, report = derive_blend_vote_map(mod_blend, position, dump_blend, duped)

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == count
    assert report.matched == 2  # vertices 0 and 2 still match uniquely
    assert report.ambiguous == 1  # vertex 1 sees two dump candidates: no vote
    assert report.unmatched == 1  # vertex 3's record vanished from the dump
    assert report.conflicts == {}
    assert "match rate" in report.refusal


def test_duplicate_dump_record_costs_no_vote_but_holds_at_threshold():
    count = 100

    def mod_indices(vertex: int) -> tuple[int, int, int, int]:
        if vertex == 99:
            return 97, 107, 117, 127  # vertex 99 carries vertex 97's indices
        return vertex, vertex + 10, vertex + 20, vertex + 30

    mod_blend = make_blend_bytes(count, mod_indices)
    dump_blend = make_blend_bytes(count, lambda v: (v + 5, v + 10, v + 20, v + 30))
    position = make_position_bytes(count, 0)
    dump_position = position[: 99 * 40] + position[98 * 40 : 99 * 40]
    mod_position = position[: 99 * 40] + position[97 * 40 : 98 * 40]

    derived, report = derive_blend_vote_map(
        mod_blend, mod_position, dump_blend, dump_position
    )

    assert report.ok
    assert report.refusal == ""
    assert report.total == count
    assert report.matched == 99  # vertices 0..97 plus 99 (via vertex 97's record)
    assert report.ambiguous == 1  # vertex 98 matches dump vertices 98 and 99
    assert report.unmatched == 0
    assert report.matched / report.total == _MIN_MATCH_RATE  # exactly at the gate
    assert report.conflicts == {}
    assert 98 not in derived  # the ambiguous vertex cast no vote
    assert derived == {vertex: vertex + 5 for vertex in range(98)}


def test_zero_vertices_refuse():
    derived, report = derive_blend_vote_map(b"", b"", b"", b"")

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 0
    assert report.refusal


# ---------------------------------------------------------------------------
# Grid derivation: tolerant radius pairing over a component's v2 mesh dump
# ---------------------------------------------------------------------------


def fixture_grid_dump(root: Path) -> V2Dump:
    """V2 mesh dump for the fixture component: bone 7 at (0, 0, 0), bone 9 at (1, 0, 0)."""
    return make_v2_dump(
        root,
        "dialyn",
        "body",
        "DialynBodyA",
        "aaaaaa01",
        [
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0)),
            ((1.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (9, 0, 0, 0)),
        ],
    )


def subdivided_mod_blend() -> bytes:
    """Four stride-32 records: two for p1 (bone 7), two for p2 (old bone 5)."""
    return make_weighted_blend_bytes(
        [((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))] * 2
        + [((1.0, 0.0, 0.0, 0.0), (5, 0, 0, 0))] * 2
    )


def subdivided_mod_position() -> bytes:
    """Four stride-40 xyz records: two near (0, 0, 0), two near (1, 0, 0)."""
    return make_xyz_position_bytes(
        [(0.001, 0.0, 0.0), (0.0, 0.001, 0.0), (1.001, 0.0, 0.0), (1.0, 0.001, 0.0)]
    )


def test_grid_derivation_pairs_subdivided_mod_vertices(tmp_path):
    dump = fixture_grid_dump(tmp_path)
    mod_position = subdivided_mod_position()
    mod_blend = subdivided_mod_blend()

    derived, report = derive_grid_vote_map(mod_position, mod_blend, dump)

    assert report.ok
    assert report.refusal == ""
    assert report.total == report.matched == 4
    assert report.unmatched == 0
    assert report.conflicts == {}
    assert derived == {5: 9}
    assert report.map == {5: 9}


def test_grid_derivation_identity_pairs_yield_ok_empty_map(tmp_path):
    dump = fixture_grid_dump(tmp_path)
    mod_position = subdivided_mod_position()
    mod_blend = make_weighted_blend_bytes(
        [((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))] * 2
        + [((1.0, 0.0, 0.0, 0.0), (9, 0, 0, 0))] * 2
    )

    derived, report = derive_grid_vote_map(mod_position, mod_blend, dump)

    assert report.ok
    assert report.refusal == ""
    assert derived == {} and report.map == {}
    assert report.total == report.matched == 4
    assert report.conflicts == {}


def test_grid_greedy_resolution_keeps_the_higher_vote_count(tmp_path):
    dump = make_v2_dump(
        tmp_path,
        "dialyn",
        "body",
        "DialynBodyB",
        "bbbbbb02",
        [
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (9, 0, 0, 0)),
            ((1.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (11, 0, 0, 0)),
        ],
    )
    mod_position = make_xyz_position_bytes(
        [
            (0.001, 0.0, 0.0),
            (0.0, 0.001, 0.0),
            (0.0, 0.0, 0.001),
            (1.001, 0.0, 0.0),
            (1.0, 0.0, 0.001),
        ]
    )
    mod_blend = make_weighted_blend_bytes([((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))] * 5)

    derived, report = derive_grid_vote_map(mod_position, mod_blend, dump)

    assert report.ok
    assert report.refusal == ""
    assert derived == {7: 9}
    assert report.total == report.matched == 5


def test_grid_derivation_refuses_low_match_ratio(tmp_path):
    dump = fixture_grid_dump(tmp_path)
    points = [(0.001, 0.0, 0.0), (1.001, 0.0, 0.0)]
    points += [(10.0 + 0.1 * v, 0.0, 0.0) for v in range(98)]
    mod_position = make_xyz_position_bytes(points)
    mod_blend = make_weighted_blend_bytes([((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))] * 100)

    derived, report = derive_grid_vote_map(mod_position, mod_blend, dump)

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 100
    assert report.matched == 2
    assert report.unmatched == 98
    assert "not the same mesh" in report.refusal


def test_grid_derivation_refuses_without_any_pair_in_radius(tmp_path):
    dump = fixture_grid_dump(tmp_path)
    mod_position = make_xyz_position_bytes([(10.0 + 0.1 * v, 0.0, 0.0) for v in range(4)])
    mod_blend = make_weighted_blend_bytes([((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))] * 4)

    derived, report = derive_grid_vote_map(mod_position, mod_blend, dump)

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 4
    assert report.matched == 0
    assert "nothing within the grid radius" in report.refusal


def test_grid_derivation_refuses_misaligned_or_mismatched_mod_buffers(tmp_path):
    dump = fixture_grid_dump(tmp_path)
    blend = make_weighted_blend_bytes([((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))])
    position = make_xyz_position_bytes([(0.0, 0.0, 0.0)])

    derived, report = derive_grid_vote_map(position, blend[:-1], dump)
    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 0
    assert "not a multiple of 32" in report.refusal

    derived, report = derive_grid_vote_map(
        make_xyz_position_bytes([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]), blend, dump
    )
    assert not report.ok
    assert derived == {} and report.map == {}
    assert "mod blend has 1 vertices but mod position has 2" in report.refusal


def test_grid_derivation_refuses_a_dump_without_blend_data(tmp_path):
    blend = make_weighted_blend_bytes([((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))])
    position = make_xyz_position_bytes([(0.0, 0.0, 0.0)])

    empty = make_v2_dump(tmp_path, "dialyn", "body", "DialynBodyA", "aaaaaa01", [])
    derived, report = derive_grid_vote_map(position, blend, empty)
    assert not report.ok
    assert derived == {} and report.map == {}
    assert (
        "the v2 dump lacks blend data (0 positions, 0 blend records)"
        in report.refusal
    )

    partial = make_v2_dump(
        tmp_path,
        "dialyn",
        "body",
        "DialynBodyB",
        "bbbbbb02",
        [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0)), ((1.0, 0.0, 0.0),)],
    )
    derived, report = derive_grid_vote_map(position, blend, partial)
    assert not report.ok
    assert derived == {} and report.map == {}
    assert (
        "the v2 dump lacks blend data (2 positions, 1 blend records)" in report.refusal
    )


def test_grid_derivation_refuses_zero_mod_vertices(tmp_path):
    dump = fixture_grid_dump(tmp_path)

    derived, report = derive_grid_vote_map(b"", b"", dump)

    assert not report.ok
    assert derived == {} and report.map == {}
    assert report.total == 0
    assert report.refusal == "no vertices to vote on"


# ---------------------------------------------------------------------------
# Scan/apply fixtures: mods folders shipping CRLF inis plus synthetic dump data
# ---------------------------------------------------------------------------

_EMPTY_TABLES = BlendTables(mappings={}, position_to_blend={})

_DIALYN_INI = (
    "[TextureOverrideDialynBody]\n"
    "hash = ff36809b\n"
    "vb0 = ResourceDialynBodyPosition\n"
    "vb2 = ResourceDialynBodyBlend\n"
    "\n"
    "[ResourceDialynBodyPosition]\n"
    "type = Buffer\n"
    "stride = 40\n"
    "filename = position.buf\n"
    "\n"
    "[ResourceDialynBodyBlend]\n"
    "type = Buffer\n"
    "stride = 32\n"
    "filename = blend.buf\n"
)

_IB_INI = (
    "[TextureOverrideBody]\n"
    "hash = 55667788\n"
    "vb0 = ResourceBodyPosition\n"
    "vb2 = ResourceBodyBlend\n"
    "\n"
    "[ResourceBodyPosition]\n"
    "type = Buffer\n"
    "stride = 40\n"
    "filename = position.buf\n"
    "\n"
    "[ResourceBodyBlend]\n"
    "type = Buffer\n"
    "stride = 32\n"
    "filename = blend.buf\n"
)

def _dump_blend_indices(vertex: int) -> tuple[int, int, int, int]:
    """Dump-side bone indices: the fixture's old numbering shifted by five."""
    return 10 * vertex + 5, 10 * vertex + 6, 10 * vertex + 7, 10 * vertex + 8


def make_ini(path: Path, text: str) -> Path:
    """Ini fixture written with the Windows line endings mods ship."""
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    return path


def build_dumps(cache_root: Path, *components: dict) -> DumpData:
    """Synthetic DumpData with on-disk binaries; one spec dict per component.

    Each spec names its component (``char``/``comp``) and may carry
    ``position_bytes``/``blend_bytes``, ``position_hash``/``blend_hash``/``ib``
    and ``binaries: False`` to ship metadata without usable dump binaries.
    """
    dumps = DumpData()
    for spec in components:
        key2 = (spec["char"], spec["comp"])
        dumps.layouts[(*key2, "position")] = DumpLayout(
            stride=40, slot="vb0", filename="position.buf"
        )
        dumps.layouts[(*key2, "blend")] = DumpLayout(
            stride=32, slot="vb2", filename="blend.buf"
        )
        if spec.get("position_hash"):
            dumps.current_hashes[(*key2, "position")] = spec["position_hash"]
        if spec.get("blend_hash"):
            dumps.current_hashes[(*key2, "blend")] = spec["blend_hash"]
        if spec.get("ib"):
            dumps.ibs[key2] = spec["ib"]
        if spec.get("binaries", True):
            folder = cache_root / f"{spec['char']}-{spec['comp']}"
            folder.mkdir(parents=True, exist_ok=True)
            for role, stride in (("position", 40), ("blend", 32)):
                path = folder / f"{role}.buf"
                path.write_bytes(spec[f"{role}_bytes"])
                dumps.binaries[(*key2, role)] = path
    return dumps


def body_dumps(cache_root: Path, **overrides) -> DumpData:
    """Dump data for one dialyn/body component covering the fixture ini's hash."""
    spec = {
        "char": "dialyn",
        "comp": "body",
        "position_hash": "ff36809b",
        "position_bytes": make_position_bytes(2, 0),
        "blend_bytes": make_blend_bytes(2, _dump_blend_indices),
    }
    spec.update(overrides)
    return build_dumps(cache_root, spec)


def write_mod(root: Path, ini_text: str = _DIALYN_INI, blend: bytes | None = None):
    """Mods folder with the fixture ini and its two bound buffers; (mod, store)."""
    mod = root / "mods" / "Mod"
    mod.mkdir(parents=True, exist_ok=True)
    make_ini(mod / "m.ini", ini_text)
    (mod / "position.buf").write_bytes(make_position_bytes(2, 0))
    if blend is None:
        blend = make_blend_bytes(
            2, lambda v: (10 * v, 10 * v + 1, 10 * v + 2, 10 * v + 3)
        )
    (mod / "blend.buf").write_bytes(blend)
    return mod, root / "store"


def vote_fixtures(tmp_path: Path):
    """A scanned vote target plus (mods, store, mod) for the two-vertex mesh."""
    mod, store = write_mod(tmp_path)
    (target,) = scan_blend_vote_targets(
        tmp_path / "mods", body_dumps(tmp_path / "dump"), _EMPTY_TABLES
    )
    return target, tmp_path / "mods", store, mod


def test_scan_resolves_position_hash_keyed_dialyn_section(tmp_path):
    mod, _store = write_mod(tmp_path)

    targets = scan_blend_vote_targets(
        tmp_path / "mods", body_dumps(tmp_path / "dump"), _EMPTY_TABLES
    )

    assert [(t.hash, t.resource, t.source) for t in targets] == [
        ("ff36809b", "DialynBodyBlend", "dialyn/body")
    ]
    assert targets[0].blend_path == mod / "blend.buf"
    assert targets[0].position_path == mod / "position.buf"
    assert targets[0].dump_blend == tmp_path / "dump" / "dialyn-body" / "blend.buf"
    assert targets[0].dump_position == tmp_path / "dump" / "dialyn-body" / "position.buf"


def test_scan_ib_hash_keyed_section_wins_over_earlier_role_hash(tmp_path):
    mod, _store = write_mod(tmp_path, _IB_INI)

    dumps = build_dumps(
        tmp_path / "dump",
        {  # sorts first, covers the hash by role, but ships no binaries
            "char": "aprior",
            "comp": "body",
            "position_hash": "55667788",
            "binaries": False,
        },
        {  # the ib match must win despite sorting later
            "char": "bzcomp",
            "comp": "body",
            "ib": "55667788",
            "position_bytes": make_position_bytes(2, 0),
            "blend_bytes": make_blend_bytes(2, _dump_blend_indices),
        },
    )
    targets = scan_blend_vote_targets(tmp_path / "mods", dumps, _EMPTY_TABLES)

    assert [(t.source, t.resource, t.hash) for t in targets] == [
        ("bzcomp/body", "BodyBlend", "55667788")
    ]
    assert targets[0].blend_path == mod / "blend.buf"


def test_scan_skips_hashes_owned_by_the_table_pass(tmp_path):
    _mod, _store = write_mod(tmp_path)
    dumps = body_dumps(tmp_path / "dump")
    direct = BlendTables(mappings={"ff36809b": {5: 9}}, position_to_blend={})
    alias = BlendTables(
        mappings={"3d7e53cf": {5: 9}}, position_to_blend={"ff36809b": "3d7e53cf"}
    )

    assert scan_blend_vote_targets(tmp_path / "mods", dumps, direct) == []
    assert scan_blend_vote_targets(tmp_path / "mods", dumps, alias) == []


def test_scan_skips_sections_failing_the_bind_and_count_gates(tmp_path):
    dumps = body_dumps(tmp_path / "dump")

    no_vb0 = (
        "[TextureOverrideBody]\n"
        "hash = ff36809b\n"
        "vb2 = ResourceBodyBlend\n"
        "\n"
        "[ResourceBodyBlend]\n"
        "type = Buffer\n"
        "stride = 32\n"
        "filename = blend.buf\n"
    )
    wrong_stride = _DIALYN_INI.replace("stride = 40", "stride = 36")
    _mod, _store = write_mod(tmp_path, no_vb0)
    assert scan_blend_vote_targets(tmp_path / "mods", dumps, _EMPTY_TABLES) == []
    _mod, _store = write_mod(tmp_path / "case-stride", wrong_stride)
    assert scan_blend_vote_targets(tmp_path / "case-stride", dumps, _EMPTY_TABLES) == []
    _mod, _store = write_mod(
        tmp_path / "case-count", blend=make_blend_bytes(3, lambda v: (v, v, v, v))
    )
    assert scan_blend_vote_targets(tmp_path / "case-count", dumps, _EMPTY_TABLES) == []
    no_binaries = body_dumps(tmp_path / "dump-nobin", binaries=False)
    _mod, _store = write_mod(tmp_path / "case-nobin")
    assert (
        scan_blend_vote_targets(tmp_path / "case-nobin", no_binaries, _EMPTY_TABLES)
        == []
    )


def test_scan_dedupes_two_inis_binding_the_same_blend_buffer(tmp_path):
    mod, _store = write_mod(tmp_path)
    make_ini(mod / "again.ini", _DIALYN_INI)

    targets = scan_blend_vote_targets(
        tmp_path / "mods", body_dumps(tmp_path / "dump"), _EMPTY_TABLES
    )

    assert [t.resource for t in targets] == ["DialynBodyBlend"]
    assert len({t.blend_path for t in targets}) == 1


def test_scan_attaches_grid_dump_when_counts_differ(tmp_path):
    dump = fixture_grid_dump(tmp_path / "dump")
    _mod, _store = write_mod(tmp_path, blend=subdivided_mod_blend())
    dumps = body_dumps(tmp_path / "dump")
    dumps.v2[("dialyn", "body")] = [dump]

    targets = scan_blend_vote_targets(tmp_path / "mods", dumps, _EMPTY_TABLES)

    assert [(t.hash, t.source) for t in targets] == [("ff36809b", "dialyn/body")]
    assert targets[0].grid is dump


def test_scan_skips_count_mismatch_without_a_v2_dump(tmp_path):
    _mod, _store = write_mod(tmp_path, blend=subdivided_mod_blend())
    dumps = body_dumps(tmp_path / "dump")

    assert scan_blend_vote_targets(tmp_path / "mods", dumps, _EMPTY_TABLES) == []


def test_scan_attaches_grid_to_counts_equal_targets(tmp_path):
    dump = fixture_grid_dump(tmp_path / "dump")
    _mod, _store = write_mod(tmp_path)
    dumps = body_dumps(tmp_path / "dump")
    dumps.v2[("dialyn", "body")] = [dump]

    targets = scan_blend_vote_targets(tmp_path / "mods", dumps, _EMPTY_TABLES)

    assert [(t.hash, t.source) for t in targets] == [("ff36809b", "dialyn/body")]
    assert targets[0].grid is dump


def test_apply_vote_remap_rewrites_backs_up_and_marks(tmp_path):
    target, mods, store, mod = vote_fixtures(tmp_path)
    original = (mod / "blend.buf").read_bytes()
    remapped = make_blend_bytes(2, _dump_blend_indices)
    logs: list[str] = []

    assert apply_blend_vote_remap(target, store, mods, log=logs.append) is True

    assert (mod / "blend.buf").read_bytes() == remapped
    (backup,) = sorted(store.rglob("*.bak"))
    assert backup.read_bytes() == original
    assert len(logs) == 1
    assert logs[0].startswith(
        "remapped blend indices (vote): blend.buf — 8 index values changed, "
        "8 mappings derived from dialyn/body (match 2/2)"
    )
    assert logs[0].endswith(".bak)")
    marker = json.loads(blend_state_path(store, mods).read_text(encoding="utf-8"))
    assert set(marker) == {"Mod/blend.buf"}
    entry = marker["Mod/blend.buf"]
    assert entry["action"] == "vote"
    assert entry["source"] == "dialyn/body"
    assert entry["hash"] == "ff36809b"
    assert entry["before"] == hashlib.sha256(original).hexdigest()
    assert entry["after"] == hashlib.sha256(remapped).hexdigest()
    assert isinstance(entry["stamp"], int)


def test_apply_vote_remap_refusal_writes_nothing(tmp_path):
    mod, store = write_mod(tmp_path)
    position = make_position_bytes(2, 0)
    conflicting = make_blend_bytes(2, lambda v: (0, v + 10, v + 20, v + 30))
    (mod / "blend.buf").write_bytes(conflicting)
    (mod / "position.buf").write_bytes(position)
    dumps = body_dumps(
        tmp_path / "dump",
        position_bytes=position,
        blend_bytes=make_blend_bytes(2, lambda v: (5 + v, v + 10, v + 20, v + 30)),
    )
    (target,) = scan_blend_vote_targets(tmp_path / "mods", dumps, _EMPTY_TABLES)
    logs: list[str] = []

    assert apply_blend_vote_remap(target, store, tmp_path / "mods", log=logs.append) is False

    assert (mod / "blend.buf").read_bytes() == conflicting
    assert not blend_state_path(store, tmp_path / "mods").exists()
    assert list(store.rglob("*.bak")) == []
    assert len(logs) == 1
    assert logs[0].startswith(
        "blend vote skipped: blend.buf — conflicting votes: old index 0 voted to [5, 6]"
    )
    assert logs[0].endswith("(match 2/2)")


def test_apply_vote_remap_idempotent_on_matching_marker(tmp_path):
    target, mods, store, mod = vote_fixtures(tmp_path)
    current = (mod / "blend.buf").read_bytes()
    state_path = blend_state_path(store, mods)
    state_path.parent.mkdir(parents=True)
    seeded = {"Mod/blend.buf": {"after": hashlib.sha256(current).hexdigest()}}
    state_path.write_text(json.dumps(seeded), encoding="utf-8")
    logs: list[str] = []

    assert apply_blend_vote_remap(target, store, mods, log=logs.append) is False

    assert logs == []
    assert (mod / "blend.buf").read_bytes() == current
    assert sorted(store.rglob("*.bak")) == []
    assert json.loads(state_path.read_text(encoding="utf-8")) == seeded


def test_apply_vote_remap_identical_buffers_already_current(tmp_path):
    mod, store = write_mod(tmp_path)
    position = make_position_bytes(2, 0)
    blend = make_blend_bytes(2, lambda v: (40 + v, 41 + v, 42 + v, 43 + v))
    (mod / "blend.buf").write_bytes(blend)
    (mod / "position.buf").write_bytes(position)
    dumps = body_dumps(
        tmp_path / "dump", position_bytes=position, blend_bytes=blend
    )
    (target,) = scan_blend_vote_targets(tmp_path / "mods", dumps, _EMPTY_TABLES)
    logs: list[str] = []

    assert apply_blend_vote_remap(target, store, tmp_path / "mods", log=logs.append) is False

    assert logs == ["blend indices already current (vote): blend.buf"]
    assert (mod / "blend.buf").read_bytes() == blend
    assert not blend_state_path(store, tmp_path / "mods").exists()
    assert list(store.rglob("*.bak")) == []


def test_apply_vote_remap_unreadable_blend_path(tmp_path):
    target = BlendVoteTarget(
        hash="ff36809b",
        resource="BodyBlend",
        blend_path=tmp_path / "gone.buf",
        position_path=tmp_path / "position.buf",
        dump_blend=tmp_path / "dump-blend.buf",
        dump_position=tmp_path / "dump-position.buf",
        source="dialyn/body",
    )
    logs: list[str] = []

    assert (
        apply_blend_vote_remap(
            target, tmp_path / "store", tmp_path / "mods", log=logs.append
        )
        is False
    )

    assert len(logs) == 1
    assert "blend vote skipped" in logs[0]
    assert not blend_state_path(tmp_path / "store", tmp_path / "mods").exists()


def test_apply_grid_fallback_remaps_marks_and_backs_up(tmp_path):
    dump = fixture_grid_dump(tmp_path / "dump")
    mod, store = write_mod(tmp_path, blend=subdivided_mod_blend())
    (mod / "position.buf").write_bytes(subdivided_mod_position())
    mods = tmp_path / "mods"
    dumps = body_dumps(tmp_path / "dump")
    dumps.v2[("dialyn", "body")] = [dump]
    (target,) = scan_blend_vote_targets(mods, dumps, _EMPTY_TABLES)
    original = (mod / "blend.buf").read_bytes()
    remapped = make_weighted_blend_bytes(
        [((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))] * 2
        + [((1.0, 0.0, 0.0, 0.0), (9, 0, 0, 0))] * 2
    )
    logs: list[str] = []

    assert apply_blend_vote_remap(target, store, mods, log=logs.append) is True

    assert (mod / "blend.buf").read_bytes() == remapped
    (backup,) = sorted(store.rglob("*.bak"))
    assert backup.read_bytes() == original
    assert len(logs) == 1
    assert logs[0].startswith(
        "remapped blend indices (grid vote): blend.buf — 2 index values changed, "
        "1 mappings derived from dialyn/body (match 4/4)"
    )
    assert "(few mappings — manual review recommended)" in logs[0]
    assert logs[0].endswith(".bak)")
    marker = json.loads(blend_state_path(store, mods).read_text(encoding="utf-8"))
    assert set(marker) == {"Mod/blend.buf"}
    entry = marker["Mod/blend.buf"]
    assert entry["action"] == "grid"
    assert entry["source"] == "dialyn/body (grid)"
    assert entry["hash"] == "ff36809b"
    assert entry["before"] == hashlib.sha256(original).hexdigest()
    assert entry["after"] == hashlib.sha256(remapped).hexdigest()
    assert isinstance(entry["stamp"], int)

    logs.clear()
    assert apply_blend_vote_remap(target, store, mods, log=logs.append) is False

    assert logs == []
    assert (mod / "blend.buf").read_bytes() == remapped
    assert len(sorted(store.rglob("*.bak"))) == 1


def test_apply_grid_fallback_refusal_logs_and_writes_nothing(tmp_path):
    dump = fixture_grid_dump(tmp_path / "dump")
    mod, store = write_mod(
        tmp_path, blend=make_weighted_blend_bytes([((1.0, 0.0, 0.0, 0.0), (7, 0, 0, 0))] * 4)
    )
    (mod / "position.buf").write_bytes(
        make_xyz_position_bytes([(10.0 + 0.1 * v, 0.0, 0.0) for v in range(4)])
    )
    mods = tmp_path / "mods"
    dumps = body_dumps(tmp_path / "dump")
    dumps.v2[("dialyn", "body")] = [dump]
    (target,) = scan_blend_vote_targets(mods, dumps, _EMPTY_TABLES)
    untouched = (mod / "blend.buf").read_bytes()
    logs: list[str] = []

    assert apply_blend_vote_remap(target, store, mods, log=logs.append) is False

    assert len(logs) == 1
    assert logs[0].startswith(
        "blend vote skipped (grid): blend.buf — nothing within the grid radius"
    )
    assert logs[0].endswith("(match 0/4)")
    assert (mod / "blend.buf").read_bytes() == untouched
    assert not blend_state_path(store, mods).exists()
    assert list(store.rglob("*.bak")) == []
