import pytest

from tank_graph_extractor.nlp import (
    NlpModelUnavailable,
    concepts_from_doc,
    extract_concepts,
    extract_concepts_batch,
    get_nlp,
    preflight_model,
)


class FakeToken:
    def __init__(
        self,
        text: str,
        *,
        lemma: str | None = None,
        pos: str = "NOUN",
        stop: bool = False,
        punct: bool = False,
    ) -> None:
        self.text = text
        self.lemma_ = lemma or text
        self.pos_ = pos
        self.is_stop = stop
        self.is_punct = punct
        self.is_space = False


class FakeSpan(list[FakeToken]):
    def __init__(self, *tokens: FakeToken, label: str = "") -> None:
        super().__init__(tokens)
        self.text = " ".join(token.text for token in tokens)
        self.label_ = label


class FakeDoc:
    def __init__(
        self,
        text: str,
        *,
        tokens: tuple[FakeToken, ...] = (),
        noun_chunks: tuple[FakeSpan, ...] = (),
        ents: tuple[FakeSpan, ...] = (),
    ) -> None:
        self.text = text
        self._tokens = tokens
        self.noun_chunks = noun_chunks
        self.ents = ents

    def __iter__(self):
        return iter(self._tokens)


class FakeNlp:
    def __init__(self, documents: dict[str, FakeDoc]) -> None:
        self.documents = documents
        self.batch_lengths: list[int] = []

    def pipe(self, texts: list[str], *, batch_size: int):
        self.batch_lengths.append(len(texts))
        assert len(texts) <= batch_size
        return [self.documents[text] for text in texts]


def test_candidates_lemmatize_remove_stops_and_filter_generics() -> None:
    determiner = FakeToken("the", pos="DET", stop=True)
    batteries = FakeToken("batteries", lemma="battery")
    tank = FakeToken("tank")
    document = FakeDoc(
        "the batteries help a tank",
        tokens=(batteries, tank),
        noun_chunks=(FakeSpan(determiner, batteries), FakeSpan(tank)),
    )
    assert concepts_from_doc(document) == ("battery",)


def test_required_aliases_are_present_only_when_matched() -> None:
    text = (
        "Use side scraping from a hull-down position to improve view-range, then snipe from cover."
    )
    concepts = extract_concepts(text, nlp=FakeNlp({text: FakeDoc(text)}))
    assert {"hull-down", "sidescraping", "sniping", "view range"} <= set(concepts)

    neutral = "Preserve hit points."
    assert extract_concepts(neutral, nlp=FakeNlp({neutral: FakeDoc(neutral)})) == ()


def test_canonical_example_concepts_are_stable() -> None:
    accuracy = FakeToken("accuracy")
    hull = FakeToken("hull")
    armor = FakeToken("armor")
    text = "High accuracy supports sniping; hull armor benefits from sidescraping."
    document = FakeDoc(
        text,
        tokens=(accuracy, hull, armor),
        noun_chunks=(FakeSpan(hull, armor),),
    )
    values = concepts_from_doc(document, source_text=text)
    assert {"accuracy", "hull armor", "sidescraping", "sniping"} <= set(values)
    assert values == tuple(sorted(values, key=lambda value: (value.casefold(), value)))


def test_bounded_batches_preserve_source_order() -> None:
    texts = ("Use snipe tactics.", "Improve vision range.", "Use side scrape.")
    fake = FakeNlp({text: FakeDoc(text) for text in texts})
    results = extract_concepts_batch(texts, nlp=fake, batch_size=2)
    assert results == (("sniping",), ("view range",), ("sidescraping",))
    assert fake.batch_lengths == [2, 1]


def test_model_load_is_lazy_cached_and_preflight_never_downloads(monkeypatch) -> None:
    spacy = pytest.importorskip("spacy")
    sentinel = object()
    calls = 0

    def fake_load(name: str):
        nonlocal calls
        calls += 1
        assert name == "en_core_web_sm"
        return sentinel

    get_nlp.cache_clear()
    monkeypatch.setattr(spacy, "load", fake_load)
    preflight_model()
    preflight_model()
    assert get_nlp() is sentinel
    assert calls == 1
    get_nlp.cache_clear()


def test_missing_model_has_explicit_preflight_failure(monkeypatch) -> None:
    spacy = pytest.importorskip("spacy")
    get_nlp.cache_clear()
    monkeypatch.setattr(
        spacy,
        "load",
        lambda _name: (_ for _ in ()).throw(OSError("missing model")),
    )
    with pytest.raises(NlpModelUnavailable, match="install it before extraction"):
        preflight_model()
    get_nlp.cache_clear()
