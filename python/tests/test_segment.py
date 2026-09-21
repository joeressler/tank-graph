import re

from tank_graph_extractor.segment import (
    SourceText,
    StrategySegment,
    deduplicate_segments,
    identity_key,
    normalize_statement,
    segment_sources,
)


class FakeSpan:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeDoc:
    def __init__(self, text: str) -> None:
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
        self.sents = tuple(FakeSpan(part) for part in parts)


class FakeNlp:
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    def pipe(self, texts: list[str], *, batch_size: int) -> list[FakeDoc]:
        self.batches.append(tuple(texts))
        assert len(texts) <= batch_size
        return [FakeDoc(text) for text in texts]


def test_normalization_uses_nfkc_hyphens_whitespace_and_citations() -> None:
    assert normalize_statement("  Ｕse\u00a0hull\u2011down [12]\n cover. ") == (
        "Use hull-down cover."
    )
    assert identity_key("HULL\u2013DOWN") == "hull-down"


def test_list_and_sentence_boundaries_preserve_provenance() -> None:
    nlp = FakeNlp()
    segments = segment_sources(
        (
            SourceText(
                text=(
                    "• Use cover after firing. Retreat when enemy guns reload.\n"
                    "• Angle hull armor to improve survival."
                ),
                source_kind="class_guide",
                source_url="https://example.test/guide",
                applicable_classes=frozenset({"Heavy Tanks"}),
            ),
        ),
        nlp,
        batch_size=1,
    )

    assert [segment.text for segment in segments] == [
        "Angle hull armor to improve survival.",
        "Retreat when enemy guns reload.",
        "Use cover after firing.",
    ]
    assert all(segment.source_kind == "class_guide" for segment in segments)
    assert all(segment.applicable_classes == {"Heavy Tanks"} for segment in segments)
    assert len(nlp.batches) == 2


def test_tactical_filter_and_length_bound_exclude_invalid_fragments() -> None:
    segments = segment_sources(
        (
            "Economy and store events.",
            "Armor overview",
            f"Use cover {'x' * 1000}.",
            "Use cover.",
        ),
        FakeNlp(),
    )
    assert tuple(segment.text for segment in segments) == ("Use cover.",)


def test_duplicate_precedence_is_wiki_then_class_then_universal() -> None:
    duplicates = (
        StrategySegment("Use cover after firing.", "universal_guide"),
        StrategySegment("USE COVER AFTER FIRING.", "class_guide"),
        StrategySegment("Use cover after firing.", "wiki"),
        StrategySegment("Angle hull armor.", "class_guide"),
    )
    selected = deduplicate_segments(duplicates)
    assert [(segment.text, segment.source_kind) for segment in selected] == [
        ("Angle hull armor.", "class_guide"),
        ("Use cover after firing.", "wiki"),
    ]
