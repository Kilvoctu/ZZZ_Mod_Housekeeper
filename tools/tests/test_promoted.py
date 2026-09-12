"""Tests for tools.promoted against tmp_path state.json (never the real project root)."""

from tools import promoted, state


def test_save_and_load_round_trip(tmp_path):
    promoted.save_promoted(
        {"Cat/Foo": "preview.jpg", "Stays": "a.png"},
        tmp_path,
    )
    assert promoted.load_promoted(tmp_path) == {
        "Cat/Foo": "preview.jpg",
        "Stays": "a.png",
    }
    assert state.state_path(tmp_path).read_bytes().endswith(b"\n")


def test_load_promoted_returns_empty_on_bad_files(tmp_path):
    assert promoted.load_promoted(tmp_path) == {}
    state.state_path(tmp_path).write_text("", encoding="utf-8")
    assert promoted.load_promoted(tmp_path) == {}
    state.state_path(tmp_path).write_text("{oops", encoding="utf-8")
    assert promoted.load_promoted(tmp_path) == {}
    state.state_path(tmp_path).write_text("[1, 2]", encoding="utf-8")
    assert promoted.load_promoted(tmp_path) == {}


def test_load_promoted_drops_non_string_entries(tmp_path):
    state.state_path(tmp_path).write_text(
        '{"promoted": {"Cat/Foo": "preview.jpg", "3": 4, "Cat/Bad": 4, '
        '"Cat/Worse": ["no"]}}',
        encoding="utf-8",
    )
    assert promoted.load_promoted(tmp_path) == {"Cat/Foo": "preview.jpg"}


def test_load_promoted_canonicalizes_legacy_keys(tmp_path):
    state.state_path(tmp_path).write_text(
        '{"promoted": {"Alexandrina Sebastiane/DISABLED_Rina": "preview.jpg",'
        ' "Pack/Clean": "a.jpg"}}',
        encoding="utf-8",
    )
    assert promoted.load_promoted(tmp_path) == {
        "Alexandrina Sebastiane/Rina": "preview.jpg",
        "Pack/Clean": "a.jpg",
    }


def test_canonical_key(tmp_path):
    # Disabled leaf is stripped, with or without surrounding whitespace.
    assert promoted.canonical_key("Cat/DISABLED_Foo") == "Cat/Foo"
    assert promoted.canonical_key("  Cat/DISABLED_Foo  ") == "Cat/Foo"
    assert promoted.canonical_key("DISABLED_Foo") == "Foo"
    # No prefix and already-clean keys pass through unchanged.
    assert promoted.canonical_key("Cat/Foo") == "Cat/Foo"
    assert promoted.canonical_key("Foo") == "Foo"
    # Only the leaf is stripped, never interior components.
    assert promoted.canonical_key("DISABLED_Cat/Foo") == "DISABLED_Cat/Foo"


def test_retarget_promoted_paths_rewrites_exact_key(tmp_path):
    promoted.save_promoted({"Old": "preview.jpg", "Cat/Old2": "a.png"}, tmp_path)

    assert promoted.retarget_promoted_paths([("Old", "New")], tmp_path) == 1

    assert promoted.load_promoted(tmp_path) == {
        "New": "preview.jpg",
        "Cat/Old2": "a.png",
    }


def test_retarget_promoted_paths_rewrites_category_prefix(tmp_path):
    promoted.save_promoted(
        {"Cat/Old": "preview.jpg", "Cat/A/B": "b.png", "Other": "c.png"}, tmp_path
    )

    assert promoted.retarget_promoted_paths([("Cat", "Pets")], tmp_path) == 2

    assert promoted.load_promoted(tmp_path) == {
        "Pets/Old": "preview.jpg",
        "Pets/A/B": "b.png",
        "Other": "c.png",
    }


def test_retarget_promoted_paths_matches_disabled_leaf_form(tmp_path):
    """A swap given in on-disk DISABLED_ form retargets the canonical key."""
    promoted.save_promoted({"Cat/Foo": "preview.jpg"}, tmp_path)

    assert (
        promoted.retarget_promoted_paths([("Cat/DISABLED_Foo", "Cat/New")], tmp_path)
        == 1
    )

    assert promoted.load_promoted(tmp_path) == {"Cat/New": "preview.jpg"}


def test_retarget_promoted_paths_no_match_leaves_file_untouched(tmp_path):
    promoted.save_promoted({"Cat/Old": "preview.jpg"}, tmp_path)
    raw = state.state_path(tmp_path).read_bytes()

    assert promoted.retarget_promoted_paths([("Ghost", "New")], tmp_path) == 0

    assert state.state_path(tmp_path).read_bytes() == raw


def test_retarget_promoted_paths_missing_file_writes_nothing(tmp_path):
    assert promoted.retarget_promoted_paths([("Old", "New")], tmp_path) == 0

    assert not state.state_path(tmp_path).exists()


def test_remove_promoted_paths_exact_key(tmp_path):
    promoted.save_promoted({"Old": "preview.jpg", "Cat/Old2": "a.png"}, tmp_path)

    assert promoted.remove_promoted_paths(["Old"], tmp_path) == 1

    assert promoted.load_promoted(tmp_path) == {"Cat/Old2": "a.png"}


def test_remove_promoted_paths_category_prefix(tmp_path):
    promoted.save_promoted(
        {"Cat/Old": "preview.jpg", "Cat/A/B": "b.png", "Other": "c.png"}, tmp_path
    )

    assert promoted.remove_promoted_paths(["Cat"], tmp_path) == 2

    assert promoted.load_promoted(tmp_path) == {"Other": "c.png"}


def test_remove_promoted_paths_no_match_leaves_file_untouched(tmp_path):
    promoted.save_promoted({"Cat/Old": "preview.jpg"}, tmp_path)
    raw = state.state_path(tmp_path).read_bytes()

    assert promoted.remove_promoted_paths(["Ghost"], tmp_path) == 0

    assert state.state_path(tmp_path).read_bytes() == raw


def test_remove_promoted_paths_missing_file_writes_nothing(tmp_path):
    assert promoted.remove_promoted_paths(["Old"], tmp_path) == 0

    assert not state.state_path(tmp_path).exists()
