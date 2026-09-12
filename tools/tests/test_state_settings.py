"""Regression tests for StateSettings, the QSettings-compatible wrapper over
the state.json "settings" section.

Headless-safe: StateSettings touches no Qt types; importing
tools.ui.main_window pulls in PySide6 but creates no widgets and needs no
QApplication.
"""

from pathlib import Path

from tools.presets import load_presets, save_preset
from tools.promoted import load_promoted, save_promoted
from tools.state import load_state, state_path
from tools.ui.main_window import StateSettings


def test_set_value_preserves_other_sections(tmp_path: Path) -> None:
    save_promoted({"Game/MyMod": "preview.jpg"}, tmp_path)
    save_preset("1", ["X"], tmp_path)
    settings = StateSettings(tmp_path)
    settings.setValue("preview_geometry", "GEOM")
    settings.setValue("window_geometry", "GEO")
    assert load_promoted(tmp_path) == {"Game/MyMod": "preview.jpg"}
    assert load_presets(tmp_path) == {"1": ["X"]}
    settings = load_state(tmp_path)["settings"]
    assert isinstance(settings, dict)
    assert settings["preview_geometry"] == "GEOM"


def test_two_instances_do_not_clobber_each_other(tmp_path: Path) -> None:
    first = StateSettings(tmp_path)
    first.setValue("first_key", "v1")
    second = StateSettings(tmp_path)
    second.setValue("second_key", 7)
    assert first.value("first_key") == "v1"
    assert second.value("second_key") == 7
    assert StateSettings(tmp_path).value("first_key") == "v1"


def test_value_round_trip_types(tmp_path: Path) -> None:
    settings = StateSettings(tmp_path)
    settings.setValue("flag", False)
    settings.setValue("count", 3)
    settings.setValue("note", "text")
    assert settings.value("flag") is False
    assert settings.value("count") == 3
    assert settings.value("note") == "text"


def test_value_default_when_state_missing(tmp_path: Path) -> None:
    settings = StateSettings(tmp_path)
    assert settings.value("window_geometry") is None
    assert settings.value("window_geometry", "fallback") == "fallback"
    assert not state_path(tmp_path).exists()
