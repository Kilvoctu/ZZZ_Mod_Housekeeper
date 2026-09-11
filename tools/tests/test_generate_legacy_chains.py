"""Tests for tools.generate_legacy_chains.harvest_key on synthetic entries.

The sentinel command classes are defined locally in each test, mirroring how
the reference modules only ever build tuples of (command_class, args).
"""

from tools.generate_legacy_chains import harvest_key


def test_harvest_key_single_version_log_yields_version_label():
    log = type("log", (), {})

    update_hash = type("update_hash", (), {})

    entries = [
        (log, ("1.0: Sample FaceA LightMap Hash (OLD)",)),
        (update_hash, ("2fa5ffa7",)),
    ]
    transitions, reason = harvest_key("AAAAAAAA", entries)
    assert reason is None
    assert transitions == [("aaaaaaaa", "2fa5ffa7", "1.0")]


def test_harvest_key_arrow_wins_over_armed_single():
    log = type("log", (), {})

    update_hash = type("update_hash", (), {})

    entries = [
        (log, ("1.0: Sample FaceA LightMap Hash (OLD)",)),
        (log, ("1.1 -> 1.2: real dated update",)),
        (update_hash, ("2fa5ffa7",)),
    ]
    transitions, reason = harvest_key("AAAAAAAA", entries)
    assert reason is None
    assert transitions == [("aaaaaaaa", "2fa5ffa7", "1.1 -> 1.2")]


def test_harvest_key_single_never_overrides_armed_arrow():
    log = type("log", (), {})

    update_hash = type("update_hash", (), {})

    entries = [
        (log, ("1.1 -> 1.2: real dated update",)),
        (log, ("2.5: Updating BelleDelicateSunlight Neck IB to 62ed56cc",)),
        (update_hash, ("2fa5ffa7",)),
    ]
    transitions, reason = harvest_key("AAAAAAAA", entries)
    assert reason is None
    assert transitions == [("aaaaaaaa", "2fa5ffa7", "1.1 -> 1.2")]


def test_harvest_key_update_hash_disarms_pending():
    log = type("log", (), {})

    update_hash = type("update_hash", (), {})

    entries = [
        (log, ("1.0: Sample FaceA LightMap Hash (OLD)",)),
        (update_hash, ("2fa5ffa7",)),
        (update_hash, ("3c3c3c3c",)),
    ]
    transitions, reason = harvest_key("AAAAAAAA", entries)
    assert reason == "undated"
    assert transitions == []


def test_harvest_key_undated_when_no_dated_log():
    log = type("log", (), {})

    update_hash = type("update_hash", (), {})

    entries = [
        (log, ("some undated note",)),
        (update_hash, ("2fa5ffa7",)),
    ]
    assert harvest_key("AAAAAAAA", entries) == ([], "undated")


def test_harvest_key_tolerates_structural_classes():
    log = type("log", (), {})

    update_hash = type("update_hash", (), {})

    add_section_if_missing = type("add_section_if_missing", (), {})

    multiply_section_if_missing = type("multiply_section_if_missing", (), {})

    add_ib_check_if_missing = type("add_ib_check_if_missing", (), {})

    entries = [
        (log, ("1.0: Sample FaceA LightMap Hash (OLD)",)),
        (add_section_if_missing, ("IB",)),
        (multiply_section_if_missing, ("IB",)),
        (add_ib_check_if_missing, ("deadbeef",)),
        (update_hash, ("2fa5ffa7",)),
    ]
    transitions, reason = harvest_key("AAAAAAAA", entries)
    assert reason is None
    assert transitions == [("aaaaaaaa", "2fa5ffa7", "1.0")]


def test_harvest_key_still_drops_buffer_coupled_and_destructive():
    log = type("log", (), {})

    update_hash = type("update_hash", (), {})

    zzz_13_remap_texcoord = type("zzz_13_remap_texcoord", (), {})

    comment_sections = type("comment_sections", (), {})

    buffer_coupled = [
        (log, ("1.0: Sample FaceA LightMap Hash (OLD)",)),
        (zzz_13_remap_texcoord, ("IB",)),
        (update_hash, ("2fa5ffa7",)),
    ]
    assert harvest_key("AAAAAAAA", buffer_coupled) == ([], "buffer-coupled")

    destructive = [
        (log, ("1.0: Sample FaceA LightMap Hash (OLD)",)),
        (comment_sections, ("IB",)),
        (update_hash, ("2fa5ffa7",)),
    ]
    assert harvest_key("AAAAAAAA", destructive) == ([], "non-chain")
