"""Tests for tools.modinfo: toggles, swaps, overrides and mod-dir scanning."""

from pathlib import Path

from tools import modinfo

SAMPLE_INI = """[Constants]
global $active0
global persist $swapkey0 = 0

[Present]
post $active0 = 0

[KeySwap0]
condition = $active0 == 1
key = x
type = cycle
$swapkey0 = 0,1,2,

[TextureOverride_IB_Outfit_Component1]
hash = 04b53ecd
ps-t4 = Resource_Outfit_Component1_NormalMap

[TextureOverride_VB_Outfit]
hash = 67eafa06

[CommandList_IB_Outfit_Component1]
if $swapkey0 == 0
; [default] [04b53ecd-1.005] (8718)
drawindexed = 39354, 0, 0
endif
"""


def test_parse_sample_single_toggle():
    info = modinfo.parse_ini_info(SAMPLE_INI)
    assert len(info.toggles) == 1
    toggle = info.toggles[0]
    assert toggle.key == "x"
    assert toggle.variable == "$swapkey0"
    assert toggle.cycle == ("0", "1", "2")
    assert "default" in toggle.state_names
    assert toggle.section == "KeySwap0"


def test_multiple_key_lines_make_one_toggle_each():
    text = "[KeySwap]\nkey = x\nback = VK_DELETE\ntype = cycle\nkey = VK_UP\n$swapvar = 0,1,2,\n"
    info = modinfo.parse_ini_info(text)
    assert len(info.toggles) == 3
    assert [toggle.key for toggle in info.toggles] == ["x", "VK_UP", "VK_DELETE"]
    assert all(toggle.section == "KeySwap" for toggle in info.toggles)
    assert all(toggle.variable == "$swapvar" for toggle in info.toggles)
    assert all(toggle.cycle == ("0", "1", "2") for toggle in info.toggles)
    assert [toggle.label for toggle in info.toggles] == ["Toggle", "Toggle", "Toggle (Back)"]


def test_texture_swaps_captured_in_any_section():
    info = modinfo.parse_ini_info(SAMPLE_INI)
    assert info.swaps == [
        modinfo.TextureSwap(
            section="TextureOverride_IB_Outfit_Component1",
            slot="ps-t4",
            resource="Resource_Outfit_Component1_NormalMap",
        )
    ]
    text = "[CommandList_Y]\nrun = Fixed\nps-t1 = ResourceAlpha\nps-t4 = ResourceBeta\n"
    swaps = modinfo.parse_ini_info(text).swaps
    assert [(swap.section, swap.slot, swap.resource) for swap in swaps] == [
        ("CommandList_Y", "ps-t1", "ResourceAlpha"),
        ("CommandList_Y", "ps-t4", "ResourceBeta"),
    ]


def test_overrides_recorded_and_non_override_sections_ignored():
    info = modinfo.parse_ini_info(SAMPLE_INI)
    assert [(override.section, override.hash_value) for override in info.overrides] == [
        ("TextureOverride_IB_Outfit_Component1", "04b53ecd"),
        ("TextureOverride_VB_Outfit", "67eafa06"),
    ]
    assert modinfo.parse_ini_info("[ResourceThing]\nhash = abc\n").overrides == []


def test_key_without_cycle_is_toggle():
    text = "[KeyMouseDrag]\nkey = VK_LBUTTON\n"
    info = modinfo.parse_ini_info(text)
    assert len(info.toggles) == 1
    toggle = info.toggles[0]
    assert toggle.label == "MouseDrag"
    assert toggle.key_display == "LMB"
    assert toggle.variable is None
    assert toggle.cycle == ()
    assert info.swaps == []
    assert info.overrides == []


def test_override_without_hash_is_none():
    text = "[TextureOverride_IB]\nps-t0 = ResourceX\n"
    overrides = modinfo.parse_ini_info(text).overrides
    assert len(overrides) == 1
    assert overrides[0].section == "TextureOverride_IB"
    assert overrides[0].hash_value is None


def test_hash_uppercase_lowercased():
    info = modinfo.parse_ini_info("[TextureOverride_VB]\nhash = 67EAFA06\n")
    assert info.overrides[0].hash_value == "67eafa06"


def test_scan_mod_info_order_paths_and_skips_broken(tmp_path):
    mod_dir = Path(tmp_path) / "mods"
    mod_dir.mkdir()
    (mod_dir / "a.ini").write_text(SAMPLE_INI, encoding="utf-8", newline="")
    (mod_dir / "b.ini").write_text(
        "[TextureOverride_IB]\nhash = 67EAFA06\n", encoding="utf-8", newline=""
    )
    (mod_dir / "broken.ini").write_bytes(b"\x80")
    infos = modinfo.scan_mod_info(mod_dir)
    assert [info.path for info in infos] == ["a.ini", "b.ini"]
    assert [toggle.section for toggle in infos[0].toggles] == ["KeySwap0"]
    assert [override.section for override in infos[0].overrides] == [
        "TextureOverride_IB_Outfit_Component1",
        "TextureOverride_VB_Outfit",
    ]
    assert infos[1].overrides[0].hash_value == "67eafa06"


def test_state_names_dedupe_preserving_order():
    text = "[KeySwap]\nkey = x\ntype = cycle\n; [b] [x]\n; [a]\n; [b]\n; [a] [y]\n"
    info = modinfo.parse_ini_info(text)
    assert info.toggles[0].state_names == ("b", "a")


def test_scan_mod_info_nested_display_path_and_relative_fallback(tmp_path):
    mod_dir = Path(tmp_path) / "mods"
    nested = mod_dir / "IB"
    nested.mkdir(parents=True)
    (nested / "c.ini").write_text("[TextureOverride_IB]\n", encoding="utf-8", newline="")
    infos = modinfo.scan_mod_info(mod_dir)
    assert [info.path for info in infos] == ["IB/c.ini"]


def test_friendly_key_display_mapping():
    assert modinfo._key_display("alt up") == "Alt+↑"
    assert modinfo._key_display("ctrl no_shift no_alt VK_UP") == "Ctrl+↑"
    assert modinfo._key_display("VK_CONTROL VK_UP") == "Ctrl+↑"
    assert modinfo._key_display("VK_LBUTTON") == "LMB"
    assert modinfo._key_display("ctrl alt") == "Ctrl+Alt"
    assert modinfo._key_display("ctrl R") == "Ctrl+R"
    assert modinfo._key_display("=") == "="
    assert modinfo._key_display("VK_F12") == "F12"
    assert modinfo._key_display("VK_NUMPAD5") == "NUM 5"
    assert modinfo._key_display("XB_A") == "XB A"
    assert modinfo._key_display("no_ctrl no_shift") == ""


def test_toggle_label_prefixes():
    assert modinfo._toggle_label("KeySwap_arm") == "arm"
    assert modinfo._toggle_label("KeySwap0") == "0"
    assert modinfo._toggle_label("KeyHelp") == "Help"
    assert modinfo._toggle_label("KeyToggleUI") == "ToggleUI"
    assert modinfo._toggle_label("Key") == "Toggle"
    assert modinfo._toggle_label("KeySwap") == "Toggle"


def test_parse_sample_toggle_label_and_display():
    info = modinfo.parse_ini_info(SAMPLE_INI)
    assert info.toggles[0].label == "0"
    assert info.toggles[0].key_display == "X"


def test_non_key_section_with_key_line_ignored():
    text = "[SwapThing]\nkey = a\ntype = cycle\n$var = 0,1\n"
    assert modinfo.parse_ini_info(text).toggles == []


def test_back_line_row_label_and_display():
    text = "[KeySwapHair]\nkey = VK_CONTROL VK_UP\nback = VK_DOWN\n"
    info = modinfo.parse_ini_info(text)
    assert [toggle.label for toggle in info.toggles] == ["Hair", "Hair (Back)"]
    assert [toggle.key_display for toggle in info.toggles] == ["Ctrl+↑", "↓"]


def test_read_mod_author_found(tmp_path):
    (Path(tmp_path) / "mod.json").write_text('{"author": "mingtianjian"}', encoding="utf-8")
    assert modinfo.read_mod_author(Path(tmp_path)) == "mingtianjian"


def test_read_mod_author_missing_empty_or_unknown(tmp_path):
    mod_dir = Path(tmp_path)
    assert modinfo.read_mod_author(mod_dir) is None
    (mod_dir / "mod.json").write_text('{"author": "Unknown"}', encoding="utf-8")
    assert modinfo.read_mod_author(mod_dir) is None
    (mod_dir / "mod.json").write_text('{"author": ""}', encoding="utf-8")
    assert modinfo.read_mod_author(mod_dir) is None
    (mod_dir / "mod.json").write_text("not json", encoding="utf-8")
    assert modinfo.read_mod_author(mod_dir) is None
    (mod_dir / "mod.json").write_text('{"author": 5}', encoding="utf-8")
    assert modinfo.read_mod_author(mod_dir) is None
