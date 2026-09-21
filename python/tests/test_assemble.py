from __future__ import annotations

from types import SimpleNamespace

import pytest

from tank_graph_extractor.assemble import AssemblyError, assemble_records
from tank_graph_extractor.guides import GuideSegment
from tank_graph_extractor.models import (
    PageDescriptor,
    TacticalSection,
    VehicleMetadata,
    WikiVehicle,
)


class SentenceNlp:
    def pipe(self, texts: list[str], batch_size: int = 64) -> list[SimpleNamespace]:
        return [SimpleNamespace(sents=(SimpleNamespace(text=text),)) for text in texts]


class Concepts:
    def extract_batch(self, segments: object) -> tuple[tuple[str, ...], ...]:
        return tuple(
            ("accuracy", "sniping")
            if "snip" in segment.text.casefold()
            else ("hull armor", "sidescraping")
            for segment in segments
        )


def vehicle(
    *,
    name: str = "G04_PzVI_Tiger_I",
    class_name: str = "Heavy Tanks",
) -> WikiVehicle:
    return WikiVehicle(
        page=PageDescriptor("Tank:Tiger I", 4),
        metadata=VehicleMetadata(
            name=name,
            display_name="Tiger I",
            primary_class=class_name,
            subclasses=(),
            nation="Germany",
            tier=7,
        ),
        tactical_sections=(
            TacticalSection(
                "Performance",
                "Use long-range sniping because the gun has high accuracy.",
                2,
            ),
        ),
    )


def test_assembly_applies_exact_class_scope_and_flattens_keywords() -> None:
    guides = (
        GuideSegment(
            text="Use sidescraping to protect the hull armor.",
            applicable_classes=frozenset({"Heavy Tanks"}),
            source_kind="class_guide",
        ),
        GuideSegment(
            text="Scout enemy positions with view range.",
            applicable_classes=frozenset({"Light Tanks"}),
            source_kind="class_guide",
        ),
    )
    result = assemble_records(
        [vehicle()],
        guides,
        sentence_nlp=SentenceNlp(),
        concept_extractor=Concepts(),  # type: ignore[arg-type]
    )
    record = result.records[0]
    assert record["strategies"] == [
        "Use long-range sniping because the gun has high accuracy.",
        "Use sidescraping to protect the hull armor.",
    ]
    assert record["keywords"] == [
        "accuracy",
        "hull armor",
        "sidescraping",
        "sniping",
    ]
    assert len(result.segment_keywords) == 2


def test_duplicate_tank_identity_is_terminal() -> None:
    with pytest.raises(AssemblyError, match="duplicate tank identity"):
        assemble_records(
            [vehicle(name="Tiger"), vehicle(name="tiger")],
            [],
            sentence_nlp=SentenceNlp(),
            concept_extractor=Concepts(),  # type: ignore[arg-type]
        )
