"""Tests for the pure image helpers in tools.ui.main_window: ini-referenced vs
preview images, the recursive image walk, and destination dedup.

Headless-safe: importing the module pulls in PySide6 but creates no widgets
and needs no QApplication; image file contents are never parsed.
"""

from pathlib import Path

# noinspection PyPackageRequirements
from PySide6.QtGui import QColor, QImage

from tools.ui import main_window


def add_image(folder: Path, name: str) -> Path:
    """Placeholder image file; only the name matters to these helpers."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"\x00\x01")
    return path


def add_ini(folder: Path, text: str) -> Path:
    """Plain .ini file (char.ini) holding the given resource lines."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "char.ini"
    path.write_text(text, encoding="utf-8")
    return path


def relative_names(mod: Path, found) -> list[Path]:
    """Sorted-by-path results as mod-relative paths for readable assertions."""
    return [path.relative_to(mod) for path in found]


def test_mod_images_finds_images_recursively(tmp_path):
    """Images at the mod root, beside the .ini, and nested deeper all count as
    previews; .txt/.ini/.dds files do not. The .ini here has no resource
    lines, so nothing is referenced and every image survives."""
    mod = tmp_path / "mod"
    add_image(mod, "cover.png")
    (mod / "readme.txt").write_text("notes\n", encoding="utf-8")
    (mod / "tex.dds").write_bytes(b"\x00")
    add_ini(mod / "sub", "[TextureOverrideBody]\nhash = aaaa0000\n")
    add_image(mod / "sub", "char.png")
    add_image(mod / "sub" / "deep", "inner.bmp")

    expected = [
        Path("cover.png"),
        Path("sub") / "char.png",
        Path("sub") / "deep" / "inner.bmp",
    ]

    found = main_window._mod_images(mod)
    assert relative_names(mod, found) == expected

    walked = main_window._iter_image_files(mod)
    assert sorted(path.relative_to(mod) for path in walked) == expected


def test_mod_images_excludes_ini_referenced_images(tmp_path):
    mod = tmp_path / "mod"
    add_ini(
        mod,
        "filename = .\\res\\Background.png\n"
        "path = sub/overlay.jpg\n"
        'path = "My Pic.png"\n',
    )
    add_image(mod / "res", "background.png")  # case-mismatch vs the reference
    add_image(mod / "sub", "overlay.jpg")
    add_image(mod, "My Pic.png")
    add_image(mod, "preview.png")
    add_image(mod, "shot.webp")

    assert main_window._ini_referenced_images(mod) == {
        ("res", "background.png"),
        ("sub", "overlay.jpg"),
        ("my pic.png",),
    }

    found = main_window._mod_images(mod)
    assert relative_names(mod, found) == [Path("preview.png"), Path("shot.webp")]


def test_ini_referenced_images_ignores_game_root_dollar_values(tmp_path):
    """$-prefixed values point into the game root, so they are skipped
    entirely and cannot hide a same-named image inside the mod."""
    mod = tmp_path / "mod"
    add_ini(mod, "filename = $\\GameRoot\\tex.png\n")
    add_image(mod, "tex.png")

    assert main_window._ini_referenced_images(mod) == set()
    assert [path.name for path in main_window._mod_images(mod)] == ["tex.png"]


def test_backup_named_directories_are_pruned(tmp_path):
    """Fix-backup folders (either naming convention) are pruned by the walk;
    plain DISABLED_ folders are ordinary content and their images are kept."""
    mod = tmp_path / "mod"
    add_image(mod, "preview.png")
    add_image(mod / "DISABLED_versionfix_123", "old.png")
    add_image(mod / "DISABLED_BACKUP_1", "x.jpg")
    add_image(mod / "DISABLED_Something", "keep.png")

    walked = main_window._iter_image_files(mod)
    assert sorted(path.relative_to(mod) for path in walked) == [
        Path("DISABLED_Something") / "keep.png",
        Path("preview.png"),
    ]

    found = main_window._mod_images(mod)
    assert relative_names(mod, found) == [
        Path("DISABLED_Something") / "keep.png",
        Path("preview.png"),
    ]


def test_ini_references_outside_mod_do_not_hide_mod_images(tmp_path):
    """A ..\\ reference escaping mod_dir must never hide a mod-local image,
    including a sibling with the same file name."""
    mod = tmp_path / "mod"
    add_ini(mod, "path = ..\\shared\\shared.png\n")
    add_image(mod, "shared.png")

    referenced = main_window._ini_referenced_images(mod)
    assert not referenced - {("..", "shared", "shared.png")}

    found = main_window._mod_images(mod)
    assert relative_names(mod, found) == [Path("shared.png")]


def test_ini_referenced_images_keep_only_image_suffixes(tmp_path):
    """Non-image resource values (.buf/.ib/.dds) never enter the referenced
    set; only image-suffixed values do."""
    mod = tmp_path / "mod"
    add_ini(
        mod,
        "filename = mesh.buf\n"
        "filename = model.ib\n"
        "filename = tex.dds\n"
        "path = icons/hud.png\n",
    )
    add_image(mod / "icons", "hud.png")

    assert main_window._ini_referenced_images(mod) == {("icons", "hud.png")}


def test_mod_images_on_empty_or_missing_directory(tmp_path):
    """An empty or non-existent mod dir yields [] instead of raising."""
    empty = tmp_path / "empty"
    empty.mkdir()

    assert main_window._mod_images(empty) == []
    assert main_window._mod_images(tmp_path / "missing") == []


def test_unique_destination_appends_counter_before_suffix(tmp_path):
    """Fresh names pass through unchanged; collisions get ' (2)', ' (3)', ...
    inserted before the suffix, with or without one."""
    folder = tmp_path / "dest"
    folder.mkdir()

    assert main_window._unique_destination(folder, "pic.png") == folder / "pic.png"

    (folder / "pic.png").write_bytes(b"\x00")
    assert main_window._unique_destination(folder, "pic.png") == folder / "pic (2).png"

    (folder / "pic (2).png").write_bytes(b"\x00")
    assert main_window._unique_destination(folder, "pic.png") == folder / "pic (3).png"

    (folder / "archive").write_bytes(b"\x00")
    assert main_window._unique_destination(folder, "archive") == folder / "archive (2)"


def test_mod_images_without_any_ini(tmp_path):
    """A mod with no .ini files at all references nothing, so every image is
    a preview."""
    mod = tmp_path / "mod"
    add_image(mod, "a.png")
    add_image(mod / "nested", "b.jpg")

    found = main_window._mod_images(mod)
    assert relative_names(mod, found) == [Path("a.png"), Path("nested") / "b.jpg"]


def test_save_jpeg_flattens_transparency_to_white(tmp_path):
    """A fully transparent image is re-encoded without alpha, and formerly
    transparent pixels decode to (near-)white instead of black."""
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    destination = tmp_path / "flat.jpg"

    assert main_window._save_jpeg(image, destination)

    reloaded = QImage(str(destination))
    assert not reloaded.isNull()
    assert not reloaded.hasAlphaChannel()
    pixel = reloaded.pixelColor(0, 0)
    assert min(pixel.red(), pixel.green(), pixel.blue()) >= 250


def test_save_jpeg_keeps_opaque_image_size(tmp_path):
    image = QImage(6, 3, QImage.Format.Format_RGB32)
    image.fill(QColor(10, 20, 30))
    destination = tmp_path / "out.jpg"

    assert main_window._save_jpeg(image, destination)

    reloaded = QImage(str(destination))
    assert not reloaded.isNull()
    assert reloaded.size() == image.size()
