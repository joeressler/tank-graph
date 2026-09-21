"""Source-aware tactical statement segmentation and deterministic deduplication."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final, Protocol

from .identity import identity_key as _shared_identity_key
from .identity import normalize_display, sort_key

MAX_STRATEGY_CHARS: Final = 1000
_LIST_BOUNDARY_RE: Final = re.compile(r"(?:\r?\n)+\s*(?:[-*+•‣▪◦]|\d{1,3}[.)])\s+")
_PARAGRAPH_BOUNDARY_RE: Final = re.compile(r"(?:\r?\n\s*){2,}")
_LEADING_BULLET_RE: Final = re.compile(r"^\s*(?:[-*+•‣▪◦]|\d{1,3}[.)])\s+")
_CITATION_RE: Final = re.compile(
    r"\s*(?:\[(?:\d+|citation needed)\]|\(\s*source\s*\))\s*",
    re.IGNORECASE,
)
_TACTICAL_RE: Final = re.compile(
    r"\b(accuracy|aim|ambush|armor|battle|camouflage|conceal|consumable|"
    r"cover|crew|damage|defen[cs]|equipment|flank|gun|hull|map|mobility|"
    r"position|reload|repair|retreat|scout|shell|shoot|side.?scrap|"
    r"snip|spot|surviv|tactic|target|terrain|track|view range|vision|"
    r"weak(?:ness| spot))\w*\b",
    re.IGNORECASE,
)
_TACTICAL_IMPERATIVE_RE: Final = re.compile(
    r"^(?:avoid|equip|keep|move|position|protect|retreat|stay|support|"
    r"take|target|use|watch)\b",
    re.IGNORECASE,
)
_NON_TACTICAL_RE: Final = re.compile(
    r"\b(account|economy|event|gold|log[ -]?in|purchase|region selector|store)\b",
    re.IGNORECASE,
)
_SOURCE_PRECEDENCE: Final = {
    "wiki": 0,
    "wiki_vehicle": 0,
    "vehicle_wiki": 0,
    "class_guide": 1,
    "guide_class": 1,
    "universal_guide": 2,
    "guide_universal": 2,
    "guide": 2,
}


class SentenceDoc(Protocol):
    """Minimal spaCy-compatible sentence document interface."""

    @property
    def sents(self) -> Iterable[Any]: ...


@dataclass(frozen=True, slots=True)
class SourceText:
    """Text and provenance supplied to the common segmenter."""

    text: str
    source_kind: str
    source_url: str = ""
    applicable_classes: frozenset[str] = frozenset()
    tactical_concepts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class StrategySegment:
    """A bounded tactical statement with assignment and provenance metadata."""

    text: str
    source_kind: str
    source_url: str = ""
    applicable_classes: frozenset[str] = frozenset()
    tactical_concepts: frozenset[str] = frozenset()


def normalize_statement(text: str) -> str:
    """Normalize display text while preserving meaningful capitalization."""

    without_citations = _CITATION_RE.sub(" ", text)
    return normalize_display(without_citations).strip("• ")


def identity_key(text: str) -> str:
    """Compute the shared canonical identity key."""

    return _shared_identity_key(normalize_statement(text))


def deterministic_key(text: str) -> tuple[str, str]:
    """Return the project-wide normalized and original string sort tuple."""

    return sort_key(normalize_statement(text))


def _source_from_value(
    value: str | SourceText | Any,
    *,
    default_source_kind: str,
) -> SourceText:
    if isinstance(value, str):
        return SourceText(text=value, source_kind=default_source_kind)
    text = getattr(value, "text", None)
    if not isinstance(text, str):
        raise TypeError("segment input must be text or expose a string 'text' value")
    return SourceText(
        text=text,
        source_kind=str(getattr(value, "source_kind", default_source_kind)),
        source_url=str(getattr(value, "source_url", "")),
        applicable_classes=frozenset(getattr(value, "applicable_classes", ())),
        tactical_concepts=frozenset(getattr(value, "tactical_concepts", ())),
    )


def _markup_blocks(text: str) -> Iterator[str]:
    for paragraph in _PARAGRAPH_BOUNDARY_RE.split(text):
        for item in _LIST_BOUNDARY_RE.split("\n" + paragraph):
            cleaned = _LEADING_BULLET_RE.sub("", item).strip()
            if cleaned:
                yield cleaned


def _pipe_documents(nlp: Any, texts: list[str], batch_size: int) -> list[Any]:
    if not texts:
        return []
    if hasattr(nlp, "pipe"):
        try:
            return list(nlp.pipe(texts, batch_size=batch_size))
        except TypeError:
            return list(nlp.pipe(texts))
    return [nlp(text) for text in texts]


def _sentences(doc: Any, fallback: str) -> Iterator[str]:
    try:
        spans = tuple(doc.sents)
    except (AttributeError, ValueError):
        spans = ()
    if not spans:
        yield fallback
        return
    for span in spans:
        text = getattr(span, "text", str(span))
        if text.strip():
            yield text


def _is_complete_tactical_statement(text: str) -> bool:
    words = re.findall(r"[^\W\d_]+", text, flags=re.UNICODE)
    if len(words) < 2 or len(text) > MAX_STRATEGY_CHARS:
        return False
    if _NON_TACTICAL_RE.search(text):
        return False
    has_tactical_signal = _TACTICAL_RE.search(text) or _TACTICAL_IMPERATIVE_RE.search(text)
    if not has_tactical_signal and (len(words) < 5 or text[-1:] not in ".!?"):
        return False
    # Short title-like fragments are accepted only when phrased as instructions.
    return not (
        len(words) <= 4 and text[-1:] not in ".!?" and not _TACTICAL_IMPERATIVE_RE.search(text)
    )


def segment_sources(
    sources: Iterable[str | SourceText | Any],
    nlp: Any,
    *,
    default_source_kind: str = "wiki",
    batch_size: int = 64,
) -> tuple[StrategySegment, ...]:
    """Segment all sources in bounded NLP batches, then deduplicate and sort."""

    if batch_size < 1:
        raise ValueError("batch_size must be at least one")
    candidates: list[tuple[SourceText, str]] = []
    for value in sources:
        source = _source_from_value(value, default_source_kind=default_source_kind)
        for block in _markup_blocks(source.text):
            candidates.append((source, block))

    segmented: list[StrategySegment] = []
    for start in range(0, len(candidates), batch_size):
        batch = candidates[start : start + batch_size]
        docs = _pipe_documents(nlp, [block for _, block in batch], batch_size)
        if len(docs) != len(batch):
            raise ValueError("NLP pipeline did not preserve input cardinality")
        for (source, block), doc in zip(batch, docs, strict=True):
            for sentence in _sentences(doc, block):
                text = normalize_statement(sentence)
                if _is_complete_tactical_statement(text):
                    segmented.append(
                        StrategySegment(
                            text=text,
                            source_kind=source.source_kind,
                            source_url=source.source_url,
                            applicable_classes=source.applicable_classes,
                            tactical_concepts=source.tactical_concepts,
                        )
                    )
    return deduplicate_segments(segmented)


def deduplicate_segments(
    segments: Iterable[StrategySegment | Any],
) -> tuple[StrategySegment, ...]:
    """Apply wiki/class/universal precedence and deterministic display sorting."""

    selected: dict[str, tuple[int, int, StrategySegment]] = {}
    for position, value in enumerate(segments):
        segment = (
            value
            if isinstance(value, StrategySegment)
            else StrategySegment(
                text=normalize_statement(str(value.text)),
                source_kind=str(getattr(value, "source_kind", "guide")),
                source_url=str(getattr(value, "source_url", "")),
                applicable_classes=frozenset(getattr(value, "applicable_classes", ())),
                tactical_concepts=frozenset(getattr(value, "tactical_concepts", ())),
            )
        )
        text = normalize_statement(segment.text)
        if not text or len(text) > MAX_STRATEGY_CHARS:
            continue
        normalized_segment = StrategySegment(
            text=text,
            source_kind=segment.source_kind,
            source_url=segment.source_url,
            applicable_classes=segment.applicable_classes,
            tactical_concepts=segment.tactical_concepts,
        )
        key = identity_key(text)
        rank = _SOURCE_PRECEDENCE.get(segment.source_kind.casefold(), 3)
        current = selected.get(key)
        if current is None or (rank, position) < (current[0], current[1]):
            selected[key] = (rank, position, normalized_segment)

    return tuple(
        sorted(
            (entry[2] for entry in selected.values()),
            key=lambda segment: deterministic_key(segment.text),
        )
    )


def segment_text(
    text: str,
    nlp: Any,
    *,
    source_kind: str = "wiki",
    source_url: str = "",
    applicable_classes: Iterable[str] = (),
    tactical_concepts: Iterable[str] = (),
) -> tuple[StrategySegment, ...]:
    """Convenience wrapper for one source text."""

    return segment_sources(
        (
            SourceText(
                text=text,
                source_kind=source_kind,
                source_url=source_url,
                applicable_classes=frozenset(applicable_classes),
                tactical_concepts=frozenset(tactical_concepts),
            ),
        ),
        nlp,
    )


segment_tactical_text = segment_text
segment_blocks = segment_sources
