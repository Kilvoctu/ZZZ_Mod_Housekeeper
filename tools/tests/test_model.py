"""Tests for tools.model: name normalization."""

from tools.model import normalize_name


def test_normalize_mixed_cjk_latin_with_fullwidth_parens():
    assert normalize_name("哲-皮肤WiseSkin（头发共用）") == "哲皮肤wiseskin"


def test_normalize_latin_with_space():
    assert normalize_name("Sigrid Sportswear") == "sigridsportswear"


def test_normalize_keeps_cjk_and_latin_letters_lowercased():
    assert normalize_name("安比Anby") == "安比anby"


def test_normalize_drops_whole_parenthetical_segments():
    assert normalize_name("莱特Lighter（武器）") == "莱特lighter"
    assert normalize_name("Wise (Swim)") == "wise"
    assert normalize_name("仪玄-皮肤YixuanSkin(Night)") == "仪玄皮肤yixuanskin"


def test_normalize_empty_parens_only():
    assert normalize_name("（）") == ""
