"""Shared Unicode identity and deterministic ordering primitives."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable

_HYPHEN_VARIANTS = frozenset(
    {
        "\u058a",  # Armenian hyphen
        "\u05be",  # Hebrew punctuation maqaf
        "\u1400",  # Canadian syllabics hyphen
        "\u1806",  # Mongolian todo soft hyphen
        "\u2010",  # Hyphen
        "\u2011",  # Non-breaking hyphen
        "\u2012",  # Figure dash
        "\u2013",  # En dash
        "\u2014",  # Em dash
        "\u2015",  # Horizontal bar
        "\u2212",  # Minus sign
        "\u2e17",  # Double oblique hyphen
        "\u2e1a",  # Hyphen with diaeresis
        "\u2e3a",  # Two-em dash
        "\u2e3b",  # Three-em dash
        "\u2e40",  # Double hyphen
        "\u301c",  # Wave dash
        "\u3030",  # Wavy dash
        "\u30a0",  # Katakana-hiragana double hyphen
        "\ufe31",  # Presentation form for vertical em dash
        "\ufe32",  # Presentation form for vertical en dash
        "\ufe58",  # Small em dash
        "\ufe63",  # Small hyphen-minus
        "\uff0d",  # Fullwidth hyphen-minus
    }
)


def normalize_display(value: str) -> str:
    """Normalize emitted text while preserving display capitalization."""
    normalized = unicodedata.normalize("NFKC", value)
    canonical = ("-" if character in _HYPHEN_VARIANTS else character for character in normalized)
    return " ".join("".join(canonical).split())


def identity_key(value: str) -> str:
    """Compute the canonical identity used across Python and Rust."""
    return normalize_display(value).casefold()


def sort_key(value: str) -> tuple[str, str]:
    """Return the exact deterministic string ordering tuple."""
    return identity_key(value), value


def sorted_strings(values: Iterable[str]) -> list[str]:
    """Sort strings by canonical identity and original scalar sequence."""
    return sorted(values, key=sort_key)


def sorted_unique_strings(values: Iterable[str]) -> list[str]:
    """Deduplicate identities, retaining the smallest original scalar spelling."""
    ordered = sorted_strings(values)
    result: list[str] = []
    previous_key: str | None = None
    for value in ordered:
        current_key = identity_key(value)
        if current_key != previous_key:
            result.append(value)
            previous_key = current_key
    return result


def duplicate_identities(values: Iterable[str]) -> set[str]:
    """Return canonical keys that occur more than once."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        key = identity_key(value)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    return duplicates
