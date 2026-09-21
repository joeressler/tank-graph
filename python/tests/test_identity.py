import pytest

from tank_graph_extractor.identity import (
    duplicate_identities,
    identity_key,
    normalize_display,
    sort_key,
    sorted_strings,
    sorted_unique_strings,
)


def test_identity_applies_nfkc_whitespace_hyphen_and_full_casefold() -> None:
    value = "\u3000Ｓｔｒａße\u00a0\u2011\u2003ＴＡＮＫ\u3000"

    assert normalize_display(value) == "Straße - TANK"
    assert identity_key(value) == "strasse - tank"


@pytest.mark.parametrize("hyphen", ["\u2010", "\u2011", "\u2013", "\u2014", "\u2212", "\uff0d"])
def test_common_unicode_hyphens_are_canonicalized(hyphen: str) -> None:
    assert identity_key(f"hull{hyphen}down") == "hull-down"


def test_other_punctuation_is_preserved() -> None:
    assert identity_key("  Aim: weak-spots!  ") == "aim: weak-spots!"


def test_sort_uses_original_unicode_scalar_sequence_as_tie_breaker() -> None:
    values = ["tAnk", "Tank", "tank", "Alpha"]

    assert sorted_strings(values) == ["Alpha", "Tank", "tAnk", "tank"]
    assert sort_key("Tank") == ("tank", "Tank")


def test_unique_sorting_uses_normalized_identity() -> None:
    values = ["Hull\u2011Down", "hull-down", "View Range", " view\u00a0range "]

    assert sorted_unique_strings(values) == ["Hull\u2011Down", " view\u00a0range "]
    assert duplicate_identities(values) == {"hull-down", "view range"}


def test_normalize_display_handles_empty_and_whitespace_only_values() -> None:
    assert normalize_display("") == ""
    assert normalize_display("\t\r\n\u2003") == ""
