//! Shared Unicode identity and deterministic ordering primitives.
//!
//! The algorithm must match `python/src/tank_graph_extractor/identity.py`.
//! Keys detect violations; the loader never rewrites stored strings.

use icu_casemap::CaseMapper;
use unicode_normalization::UnicodeNormalization;

/// Hyphen scalars that Python maps to ASCII `-` before whitespace collapse.
const HYPHEN_VARIANTS: &[char] = &[
    '\u{058a}', // Armenian hyphen
    '\u{05be}', // Hebrew punctuation maqaf
    '\u{1400}', // Canadian syllabics hyphen
    '\u{1806}', // Mongolian todo soft hyphen
    '\u{2010}', // Hyphen
    '\u{2011}', // Non-breaking hyphen
    '\u{2012}', // Figure dash
    '\u{2013}', // En dash
    '\u{2014}', // Em dash
    '\u{2015}', // Horizontal bar
    '\u{2212}', // Minus sign
    '\u{2e17}', // Double oblique hyphen
    '\u{2e1a}', // Hyphen with diaeresis
    '\u{2e3a}', // Two-em dash
    '\u{2e3b}', // Three-em dash
    '\u{2e40}', // Double hyphen
    '\u{301c}', // Wave dash
    '\u{3030}', // Wavy dash
    '\u{30a0}', // Katakana-hiragana double hyphen
    '\u{fe31}', // Presentation form for vertical em dash
    '\u{fe32}', // Presentation form for vertical en dash
    '\u{fe58}', // Small em dash
    '\u{fe63}', // Small hyphen-minus
    '\u{ff0d}', // Fullwidth hyphen-minus
];

fn is_hyphen_variant(character: char) -> bool {
    HYPHEN_VARIANTS.contains(&character)
}

/// NFKC, hyphen canonicalization, and collapsed Unicode whitespace.
pub fn canonical_emitted(value: &str) -> String {
    let nfkc: String = value.nfkc().collect();
    let mapped: String = nfkc
        .chars()
        .map(|character| {
            if is_hyphen_variant(character) {
                '-'
            } else {
                character
            }
        })
        .collect();
    mapped.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Canonical identity used for uniqueness and ordering.
pub fn identity_key(value: &str) -> String {
    CaseMapper::new()
        .fold_string(&canonical_emitted(value))
        .into_owned()
}

/// Deterministic sort tuple: identity key, then original scalar sequence.
pub fn sort_key(value: &str) -> (String, String) {
    (identity_key(value), value.to_owned())
}

/// Locale-independent full case folding for already-canonical strings.
pub fn case_fold(value: &str) -> String {
    CaseMapper::new().fold_string(value).into_owned()
}

#[cfg(test)]
mod tests {
    use super::{canonical_emitted, identity_key, sort_key};

    #[test]
    fn identity_applies_nfkc_whitespace_hyphen_and_full_casefold() {
        let value = "\u{3000}Ｓｔｒａße\u{00a0}\u{2011}\u{2003}ＴＡＮＫ\u{3000}";
        assert_eq!(canonical_emitted(value), "Straße - TANK");
        assert_eq!(identity_key(value), "strasse - tank");
    }

    #[test]
    fn common_unicode_hyphens_are_canonicalized() {
        for hyphen in [
            '\u{2010}', '\u{2011}', '\u{2013}', '\u{2014}', '\u{2212}', '\u{ff0d}',
        ] {
            assert_eq!(identity_key(&format!("hull{hyphen}down")), "hull-down");
        }
    }

    #[test]
    fn other_punctuation_is_preserved() {
        assert_eq!(identity_key("  Aim: weak-spots!  "), "aim: weak-spots!");
    }

    #[test]
    fn sort_uses_original_unicode_scalar_sequence_as_tie_breaker() {
        let mut values = ["tAnk", "Tank", "tank", "Alpha"];
        values.sort_by_key(|value| sort_key(value));
        assert_eq!(values, ["Alpha", "Tank", "tAnk", "tank"]);
        assert_eq!(sort_key("Tank"), ("tank".to_owned(), "Tank".to_owned()));
    }

    #[test]
    fn canonical_emitted_handles_empty_and_whitespace_only_values() {
        assert_eq!(canonical_emitted(""), "");
        assert_eq!(canonical_emitted("\t\r\n\u{2003}"), "");
    }
}
