from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

from tank_graph_extractor.models import TaxonomyError  # noqa: E402
from tank_graph_extractor.taxonomy import (  # noqa: E402
    CANONICAL_PRIMARY_CLASSES,
    TaxonomyCrawler,
    canonicalize_primary_class,
    canonicalize_subclass,
    is_organizational_category,
    taxonomy_primary_classes,
    taxonomy_subclasses,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, payload: Any, url: str = "https://wiki.test/api.php") -> None:
        self._payload = payload
        self.url = url
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self) -> Any:
        return self._payload


class FixtureClient:
    def __init__(self, fixture: dict[str, list[dict[str, Any]]]) -> None:
        self.fixture = fixture
        self.calls: list[tuple[str, dict[str, str], str]] = []
        self.positions: dict[str, int] = {}

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        source_kind: str = "wiki",
    ) -> FakeResponse:
        assert params is not None
        self.calls.append((url, dict(params), source_kind))
        title = params["cmtitle"]
        position = self.positions.get(title, 0)
        self.positions[title] = position + 1
        return FakeResponse(self.fixture[title][position])


def load_fixture() -> dict[str, list[dict[str, Any]]]:
    return json.loads((FIXTURES / "taxonomy_pages.json").read_text(encoding="utf-8"))


def test_fifo_traversal_consumes_continuation_and_cycles() -> None:
    client = FixtureClient(load_fixture())

    taxonomy = TaxonomyCrawler(client, "https://wiki.test/api.php", "Category:Tanks").crawl()

    assert [page.title for page in taxonomy.pages] == [
        "Tank:Auto",
        "Tank:Medium",
        "Tank:Orphan",
        "Tank:Tiger I",
    ]
    assert len(taxonomy.categories) == 5
    assert [call[1]["cmtitle"] for call in client.calls[:3]] == [
        "Category:Tanks",
        "Category:Tanks",
        "Category:Tanks by type",
    ]
    first, continued = client.calls[:2]
    assert first[1] == {
        "action": "query",
        "format": "json",
        "list": "categorymembers",
        "cmtitle": "Category:Tanks",
        "cmtype": "page|subcat",
        "cmlimit": "max",
    }
    assert continued[1]["cmcontinue"] == "root-next"
    assert all(call[2] == "wiki" for call in client.calls)


def test_page_identity_and_all_taxonomy_paths_are_retained() -> None:
    taxonomy = TaxonomyCrawler(FixtureClient(load_fixture()), "https://wiki.test/api.php").crawl()

    tiger = taxonomy.page_for(1)
    assert tiger is not None
    assert tiger.categories == (
        "category:autoloaders",
        "category:heavy tanks",
    )
    assert taxonomy.page_for("Tank:Tiger_I") == tiger
    assert taxonomy_primary_classes(taxonomy, tiger) == frozenset({"Heavy Tanks"})
    assert taxonomy_subclasses(taxonomy, tiger) == ("Autoloaders",)
    assert (
        "category:tanks",
        "category:tanks by type",
        "category:heavy tanks",
        "category:autoloaders",
    ) in tiger.category_paths


def test_exact_class_subclass_and_organizational_maps() -> None:
    assert {
        "Light Tanks",
        "Medium Tanks",
        "Heavy Tanks",
        "Tank Destroyers",
        "SPGs",
    } == CANONICAL_PRIMARY_CLASSES
    assert canonicalize_primary_class("Category:Self-propelled_guns") == "SPGs"
    assert canonicalize_primary_class("light") == "Light Tanks"
    assert canonicalize_primary_class("td") == "Tank Destroyers"
    assert canonicalize_primary_class("scouts") is None
    assert canonicalize_subclass("Category:Autoloader") == "Autoloaders"
    assert canonicalize_subclass("Category:Premium tanks") is None
    assert is_organizational_category("Category:Tanks by nation")
    assert is_organizational_category("TankTree")
    assert not is_organizational_category("Category:Autoloaders")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], "malformed_category_response"),
        ({"error": {"info": "bad title"}}, "wiki_api_error"),
        ({"query": {}}, "malformed_category_response"),
        (
            {"query": {"categorymembers": []}, "continue": {}},
            "malformed_category_continuation",
        ),
        (
            {
                "query": {
                    "categorymembers": [{"ns": 0, "title": "Tank:Bad", "pageid": "not-an-int"}]
                }
            },
            "malformed_page_id",
        ),
    ],
)
def test_malformed_category_responses_are_terminal(payload: Any, code: str) -> None:
    client = FixtureClient({"Category:Tanks": [payload]})

    with pytest.raises(TaxonomyError) as raised:
        TaxonomyCrawler(client, "https://wiki.test/api.php").crawl()

    assert raised.value.diagnostic.code == code
