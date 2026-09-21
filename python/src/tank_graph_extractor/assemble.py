"""Merge parsed sources into deterministic interchange records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .guides import GuideSegment
from .identity import identity_key, sort_key, sorted_unique_strings
from .models import WikiVehicle
from .nlp import ConceptExtractor
from .segment import SourceText, StrategySegment, segment_sources


class AssemblyError(ValueError):
    """A complete set of source records cannot be assembled safely."""


@dataclass(frozen=True, slots=True)
class SegmentKeywords:
    """Internal attribution retained even though version-one JSON is flattened."""

    tank_name: str
    strategy: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssemblyResult:
    """Wire records and internal per-segment keyword attribution."""

    records: tuple[Mapping[str, Any], ...]
    segment_keywords: tuple[SegmentKeywords, ...]


def _wiki_sources(vehicle: WikiVehicle) -> Iterable[SourceText]:
    source_url = ""
    for section in vehicle.tactical_sections:
        yield SourceText(
            text=section.text,
            source_kind="wiki_vehicle",
            source_url=source_url,
            applicable_classes=frozenset({vehicle.metadata.primary_class}),
        )


def _applicable_guide_sources(
    primary_class: str, guide_segments: Iterable[GuideSegment]
) -> Iterable[SourceText]:
    for guide in guide_segments:
        if primary_class not in guide.applicable_classes:
            continue
        yield SourceText(
            text=guide.text,
            source_kind=guide.source_kind,
            source_url=guide.source_url,
            applicable_classes=guide.applicable_classes,
            tactical_concepts=guide.tactical_concepts,
        )


def _assemble_one(
    vehicle: WikiVehicle,
    guide_segments: Sequence[GuideSegment],
    sentence_nlp: Any,
    concepts: ConceptExtractor,
) -> tuple[dict[str, Any], tuple[SegmentKeywords, ...]]:
    metadata = vehicle.metadata
    sources = (
        *_wiki_sources(vehicle),
        *_applicable_guide_sources(metadata.primary_class, guide_segments),
    )
    segments: tuple[StrategySegment, ...] = segment_sources(sources, sentence_nlp)
    extracted = concepts.extract_batch(segments)
    if len(extracted) != len(segments):
        raise AssemblyError("concept extraction did not preserve segment cardinality")

    attribution: list[SegmentKeywords] = []
    keyword_values: list[str] = []
    for segment, segment_concepts in zip(segments, extracted, strict=True):
        normalized = tuple(sorted_unique_strings(segment_concepts))
        attribution.append(
            SegmentKeywords(
                tank_name=metadata.name,
                strategy=segment.text,
                keywords=normalized,
            )
        )
        keyword_values.extend(normalized)

    record = {
        "name": metadata.name,
        "display_name": metadata.display_name,
        "class": metadata.primary_class,
        "subclasses": list(sorted_unique_strings(metadata.subclasses)),
        "nation": metadata.nation,
        "tier": metadata.tier,
        "strategies": [segment.text for segment in segments],
        "keywords": sorted_unique_strings(keyword_values),
    }
    return record, tuple(attribution)


def assemble_records(
    wiki_vehicles: Iterable[WikiVehicle],
    guide_segments: Iterable[GuideSegment],
    *,
    sentence_nlp: Any,
    concept_extractor: ConceptExtractor | None = None,
) -> AssemblyResult:
    """Create one sorted record per unique tank and preserve NLP attribution."""

    concepts = concept_extractor or ConceptExtractor(sentence_nlp)
    guides = tuple(guide_segments)
    records: list[dict[str, Any]] = []
    attributions: list[SegmentKeywords] = []
    names: dict[str, str] = {}

    for vehicle in sorted(
        wiki_vehicles,
        key=lambda value: sort_key(value.metadata.name),
    ):
        name = vehicle.metadata.name
        normalized_name = identity_key(name)
        if normalized_name in names:
            raise AssemblyError(
                f"duplicate tank identity {name!r} conflicts with {names[normalized_name]!r}"
            )
        names[normalized_name] = name
        record, record_attributions = _assemble_one(vehicle, guides, sentence_nlp, concepts)
        records.append(record)
        attributions.extend(record_attributions)

    return AssemblyResult(
        records=tuple(records),
        segment_keywords=tuple(attributions),
    )
