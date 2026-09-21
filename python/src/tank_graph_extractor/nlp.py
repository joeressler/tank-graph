"""Lazy spaCy loading and deterministic tactical concept extraction."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from functools import lru_cache
from typing import Any, Final

from .identity import normalize_display, sort_key

MODEL_NAME: Final = "en_core_web_sm"
DEFAULT_BATCH_SIZE: Final = 64
MAX_KEYWORD_CHARS: Final = 120

TACTICAL_ALIASES: Final[Mapping[str, str]] = {
    "side scrape": "sidescraping",
    "side-scrape": "sidescraping",
    "side scraping": "sidescraping",
    "sidescrape": "sidescraping",
    "sidescraping": "sidescraping",
    "hull down": "hull-down",
    "hull-down": "hull-down",
    "hull down position": "hull-down",
    "hull-down position": "hull-down",
    "view range": "view range",
    "view-range": "view range",
    "vision range": "view range",
    "long range sniping": "sniping",
    "long-range sniping": "sniping",
    "snipe": "sniping",
    "sniping": "sniping",
}

_ALIAS_PATTERNS: Final = tuple(
    (
        re.compile(
            r"(?<!\w)"
            + re.escape(alias).replace(r"\ ", r"[\s-]+").replace(r"\-", r"[\s-]+")
            + r"(?!\w)",
            re.IGNORECASE,
        ),
        canonical,
    )
    for alias, canonical in sorted(
        TACTICAL_ALIASES.items(), key=lambda item: (-len(item[0]), item[0])
    )
)
_GENERIC_TERMS: Final = frozenset(
    {
        "battle",
        "game",
        "guide",
        "player",
        "tank",
        "thing",
        "vehicle",
        "world",
    }
)
_FALLBACK_STOP_WORDS: Final = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "with",
        "your",
    }
)
_TACTICAL_LEMMAS: Final = frozenset(
    {
        "accuracy",
        "aim",
        "ambush",
        "armor",
        "camouflage",
        "concealment",
        "cover",
        "damage",
        "equipment",
        "flank",
        "gun",
        "hull",
        "mobility",
        "position",
        "reload",
        "repair",
        "retreat",
        "scout",
        "shell",
        "shoot",
        "sidescraping",
        "snipe",
        "sniping",
        "spot",
        "survival",
        "target",
        "terrain",
        "track",
        "vision",
    }
)
_EXCLUDED_ENTITY_LABELS: Final = frozenset(
    {"CARDINAL", "DATE", "MONEY", "ORDINAL", "PERCENT", "PERSON", "QUANTITY", "TIME"}
)


class NlpModelUnavailable(RuntimeError):
    """The required installed spaCy model could not be loaded."""


@lru_cache(maxsize=1)
def get_nlp() -> Any:
    """Load and cache the required English model without network access."""

    try:
        import spacy

        return spacy.load(MODEL_NAME)
    except (ImportError, OSError) as error:
        raise NlpModelUnavailable(
            f"spaCy model {MODEL_NAME!r} is unavailable; install it before extraction"
        ) from error


def preflight_model() -> None:
    """Fail explicitly before collection if the configured model is absent."""

    get_nlp()


def _display_text(value: Any) -> str:
    return str(getattr(value, "text", value))


def _candidate_tokens(candidate: Any) -> Iterator[Any]:
    if isinstance(candidate, str):
        return iter(())
    try:
        return iter(candidate)
    except TypeError:
        return iter((candidate,))


def _normalize_raw_text(text: str) -> str:
    return normalize_display(text).casefold()


def _normalize_candidate(candidate: Any) -> str:
    tokens = list(_candidate_tokens(candidate))
    pieces: list[str] = []
    if tokens:
        for token in tokens:
            text = str(getattr(token, "text", token))
            if (
                bool(getattr(token, "is_space", False))
                or bool(getattr(token, "is_punct", False))
                or bool(getattr(token, "is_stop", False))
                or text.casefold() in _FALLBACK_STOP_WORDS
            ):
                continue
            lemma = str(getattr(token, "lemma_", "") or text)
            if lemma == "-PRON-":
                lemma = text
            pieces.append(lemma)
        value = " ".join(pieces)
    else:
        value = re.sub(r"[^\w-]+", " ", _display_text(candidate), flags=re.UNICODE)
        pieces = [piece for piece in value.split() if piece.casefold() not in _FALLBACK_STOP_WORDS]
        value = " ".join(pieces)

    value = _normalize_raw_text(value)
    value = re.sub(r"[^\w-]+", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip(" -_")
    return TACTICAL_ALIASES.get(value, value)


def _valid_keyword(value: str) -> bool:
    if not value or len(value) > MAX_KEYWORD_CHARS:
        return False
    if value in _GENERIC_TERMS:
        return False
    return re.fullmatch(r"[\d\s.,:+/-]+", value) is None


def _noun_chunks(doc: Any) -> tuple[Any, ...]:
    try:
        return tuple(doc.noun_chunks)
    except (AttributeError, NotImplementedError, ValueError):
        return ()


def _entities(doc: Any) -> Iterator[Any]:
    for entity in getattr(doc, "ents", ()):
        if str(getattr(entity, "label_", "")) not in _EXCLUDED_ENTITY_LABELS:
            yield entity


def _tactical_tokens(doc: Any) -> Iterator[Any]:
    for token in doc:
        pos = str(getattr(token, "pos_", ""))
        lemma = _normalize_raw_text(
            str(getattr(token, "lemma_", "") or getattr(token, "text", token))
        )
        if pos in {"NOUN", "PROPN"} and lemma in _TACTICAL_LEMMAS:
            yield token


def _alias_values(text: str) -> Iterator[str]:
    normalized = _normalize_raw_text(text)
    emitted: set[str] = set()
    for pattern, canonical in _ALIAS_PATTERNS:
        if pattern.search(normalized) and canonical not in emitted:
            emitted.add(canonical)
            yield canonical


def concepts_from_doc(doc: Any, *, source_text: str | None = None) -> tuple[str, ...]:
    """Extract normalized model and domain candidates from one parsed document."""

    values: set[str] = set()
    candidates: list[Any] = [
        *_entities(doc),
        *_noun_chunks(doc),
        *_tactical_tokens(doc),
    ]
    for candidate in candidates:
        normalized = _normalize_candidate(candidate)
        if _valid_keyword(normalized):
            values.add(normalized)

    text = source_text if source_text is not None else _display_text(doc)
    for alias in _alias_values(text):
        if _valid_keyword(alias):
            values.add(alias)
    return tuple(sorted(values, key=sort_key))


def _pipe(nlp: Any, texts: list[str], batch_size: int) -> list[Any]:
    if hasattr(nlp, "pipe"):
        try:
            return list(nlp.pipe(texts, batch_size=batch_size))
        except TypeError:
            return list(nlp.pipe(texts))
    return [nlp(text) for text in texts]


def extract_concepts_batch(
    segments: Iterable[str | Any],
    *,
    nlp: Any | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> tuple[tuple[str, ...], ...]:
    """Process bounded batches in source order and preserve result cardinality."""

    if batch_size < 1:
        raise ValueError("batch_size must be at least one")
    model = nlp if nlp is not None else get_nlp()
    source_values = list(segments)
    texts = [value if isinstance(value, str) else str(value.text) for value in source_values]
    results: list[tuple[str, ...]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        source_batch = source_values[start : start + batch_size]
        docs = _pipe(model, batch, batch_size)
        if len(docs) != len(batch):
            raise ValueError("NLP pipeline did not preserve input cardinality")
        for source, text, doc in zip(source_batch, batch, docs, strict=True):
            concepts = set(concepts_from_doc(doc, source_text=text))
            for annotation in getattr(source, "tactical_concepts", ()):
                normalized = _normalize_candidate(str(annotation))
                if _valid_keyword(normalized):
                    concepts.add(normalized)
            results.append(tuple(sorted(concepts, key=sort_key)))
    return tuple(results)


def extract_concepts(
    segment: str | Any,
    *,
    nlp: Any | None = None,
) -> tuple[str, ...]:
    """Extract deterministic normalized concepts from one strategy segment."""

    return extract_concepts_batch((segment,), nlp=nlp)[0]


class ConceptExtractor:
    """Injectable façade used by orchestration and offline tests."""

    def __init__(
        self,
        nlp: Any | None = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        self._nlp = nlp
        self.batch_size = batch_size

    def extract(self, segment: str | Any) -> tuple[str, ...]:
        return extract_concepts(segment, nlp=self._nlp)

    def extract_batch(self, segments: Iterable[str | Any]) -> tuple[tuple[str, ...], ...]:
        return extract_concepts_batch(
            segments,
            nlp=self._nlp,
            batch_size=self.batch_size,
        )


extract_normalized_concepts = extract_concepts
