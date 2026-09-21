from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).parents[1] / "src"
sys.path.insert(0, str(SRC))

import tank_graph_extractor.wiki as wiki_module  # noqa: E402
from tank_graph_extractor.models import (  # noqa: E402
    CandidateStatus,
    PageDescriptor,
    Taxonomy,
    WikiExtractionError,
    WikiRevision,
)
from tank_graph_extractor.wiki import (  # noqa: E402
    CANONICAL_NATIONS,
    canonicalize_nation,
    clean_wiki_text,
    extract_tactical_sections,
    fetch_wiki_revision,
    parse_wiki_vehicle,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.url = "https://wiki.test/api.php?redacted"
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self) -> Any:
        return self._payload


class FakeClient:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, str] | None, str]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        source_kind: str = "wiki",
    ) -> FakeResponse:
        self.calls.append((url, params, source_kind))
        return FakeResponse(self.payload)


def revision_fixtures() -> dict[str, Any]:
    return json.loads((FIXTURES / "wiki_revisions.json").read_text(encoding="utf-8"))


def test_bare_external_link_is_rendered_without_recursive_reparsing() -> None:
    assert clean_wiki_text("[https://example.net/path]") == "https://example.net/path"


def test_parser_resource_error_rejects_candidate_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taxonomy, page = taxonomy_for("category:heavy tanks")
    revision = WikiRevision(
        title=page.title,
        text="{{Vehicle}}",
        source_url="https://wiki.test/api.php",
    )

    def fail_safely(_value: object) -> object:
        raise RecursionError("malicious nesting")

    monkeypatch.setattr(wiki_module.mwparserfromhell, "parse", fail_safely)
    result = parse_wiki_vehicle(revision, page, taxonomy)
    assert result.status is CandidateStatus.REJECTED
    assert result.diagnostics[0].code == "malformed_wiki_markup"


def taxonomy_for(
    *categories: str, page_id: int = 1, title: str = "Tank:Tiger I"
) -> tuple[Taxonomy, PageDescriptor]:
    path = ("category:tanks", *categories)
    page = PageDescriptor(
        title=title,
        page_id=page_id,
        categories=categories[-1:] if categories else (),
        category_paths=(path,),
    )
    taxonomy = Taxonomy(
        root_category="Category:Tanks",
        categories=path,
        parent_links=tuple(zip(path[1:], path[:-1], strict=True)),
        pages=(page,),
    )
    return taxonomy, page


def make_revision(
    text: str,
    *,
    title: str = "Tank:Tiger I",
    display_title: str | None = None,
) -> WikiRevision:
    return WikiRevision(
        title=title,
        text=text,
        source_url="https://wiki.test/page",
        display_title=display_title,
        page_id=1,
    )


def test_revision_request_is_exact_and_legacy_shape_is_supported() -> None:
    client = FakeClient(revision_fixtures()["legacy"])

    revision = fetch_wiki_revision(
        client, "https://wiki.test/api.php", PageDescriptor("Tank:Tiger I", 1)
    )

    assert revision.title == "Tank:Tiger I"
    assert revision.text.startswith("{{ Vehicle")
    assert revision.display_title == "<span>Tiger I</span>"
    assert client.calls == [
        (
            "https://wiki.test/api.php",
            {
                "action": "query",
                "format": "json",
                "prop": "revisions|pageprops",
                "rvprop": "content",
                "redirects": "1",
                "titles": "Tank:Tiger I",
            },
            "wiki",
        )
    ]
    assert "rvslots" not in client.calls[0][1]


def test_modern_main_slot_shape_is_supported() -> None:
    revision = fetch_wiki_revision(
        FakeClient(revision_fixtures()["modern"]),
        "https://wiki.test/api.php",
        "Tank:Modern",
    )
    assert revision.page_id == 2
    assert revision.text.startswith("{{tank_data")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ([], "malformed_revision_response"),
        ({"error": {"info": "denied"}}, "wiki_api_error"),
        ({"query": {"pages": {"-1": {"title": "Tank:X", "missing": ""}}}}, "missing_wiki_page"),
        (
            {"query": {"pages": {"1": {"pageid": 1, "title": "Tank:X"}}}},
            "missing_revision",
        ),
        (
            {
                "query": {
                    "pages": {
                        "1": {
                            "pageid": 1,
                            "title": "Tank:X",
                            "revisions": [{"slots": {"main": {"bad": "shape"}}}],
                        }
                    }
                }
            },
            "unrecognized_revision_shape",
        ),
        (
            {
                "query": {
                    "redirects": [
                        {"from": "Tank:A", "to": "Tank:B"},
                        {"from": "Tank:B", "to": "Tank:A"},
                    ],
                    "pages": {},
                }
            },
            "redirect_loop",
        ),
    ],
)
def test_terminal_revision_cases(payload: Any, code: str) -> None:
    with pytest.raises(WikiExtractionError) as raised:
        fetch_wiki_revision(FakeClient(payload), "https://wiki.test/api.php", "Tank:X")
    assert raised.value.diagnostic.code == code


def test_vehicle_metadata_and_tactics_parse_from_fixture() -> None:
    revision = fetch_wiki_revision(
        FakeClient(revision_fixtures()["legacy"]),
        "https://wiki.test/api.php",
        "Tank:Tiger I",
    )
    taxonomy, page = taxonomy_for("category:heavy tanks", "category:autoloaders")

    result = parse_wiki_vehicle(revision, page, taxonomy)

    assert result.status is CandidateStatus.ACCEPTED
    assert result.vehicle is not None
    assert result.vehicle.metadata.name == "Tiger_I"
    assert result.vehicle.metadata.display_name == "Tiger I"
    assert result.vehicle.metadata.primary_class == "Heavy Tanks"
    assert result.vehicle.metadata.subclasses == ("Autoloaders",)
    assert result.vehicle.metadata.nation == "Germany"
    assert result.vehicle.metadata.tier == 7
    assert len(result.vehicle.tactical_sections) == 1
    tactical_text = result.vehicle.tactical_sections[0].text
    assert "long-range sniping" in tactical_text
    assert "Use Hull-down cover." in tactical_text
    assert "Keep nested advice." in tactical_text
    assert "hidden" not in tactical_text
    assert "history" not in tactical_text.casefold()
    assert "citation" not in tactical_text


def test_tactical_heading_bounds_both_titles_and_empty_diagnostic() -> None:
    text = """
== Performance ==
Paragraph one

* First item
=== Nested ===
Nested paragraph
== Tactics ==
[[Category:Hidden]]
[[File:Hidden.png]]
{{navigation}}
== Other ==
outside
"""
    sections, diagnostics = extract_tactical_sections(text, "Tank:X", "fixture")

    assert [section.title for section in sections] == ["Performance"]
    assert sections[0].text == "Paragraph one\nFirst item\nNested paragraph"
    assert diagnostics[0].code == "empty_tactical_section"
    assert "outside" not in sections[0].text
    assert extract_tactical_sections("== History ==\nNothing") == ((), ())


def test_non_vehicle_and_competing_infoboxes_are_classified() -> None:
    taxonomy, page = taxonomy_for("category:heavy tanks")
    non_vehicle = parse_wiki_vehicle(make_revision("== History ==\nNot a vehicle"), page, taxonomy)
    competing = parse_wiki_vehicle(
        make_revision(
            "{{Vehicle|nation=UK|tier=5|class=heavy tanks}}"
            "{{Tankdata|nation=UK|tier=5|class=heavy tanks}}"
        ),
        page,
        taxonomy,
    )

    assert non_vehicle.status is CandidateStatus.NON_VEHICLE
    assert non_vehicle.diagnostics[0].code == "non_vehicle_candidate"
    assert competing.status is CandidateStatus.REJECTED
    assert competing.diagnostics[0].code == "competing_vehicle_infoboxes"


def test_parameter_alias_conflict_rejects_candidate() -> None:
    taxonomy, page = taxonomy_for("category:heavy tanks")
    result = parse_wiki_vehicle(
        make_revision(
            "{{Vehicle|id=Tiger|name=Tiger|class=heavy tank|type=medium tank"
            "|nation=Germany|tier=7}}"
        ),
        page,
        taxonomy,
    )
    assert result.status is CandidateStatus.REJECTED
    assert result.diagnostics[0].code == "conflicting_infobox_parameters"


@pytest.mark.parametrize(
    ("infobox_class", "categories", "status", "code"),
    [
        ("cruiser", ("category:heavy tanks",), CandidateStatus.REJECTED, "unknown_infobox_class"),
        (
            "medium tanks",
            ("category:heavy tanks",),
            CandidateStatus.REJECTED,
            "primary_class_conflict",
        ),
        (
            "heavy tanks",
            ("category:heavy tanks", "category:medium tanks"),
            CandidateStatus.REJECTED,
            "contradictory_taxonomy_classes",
        ),
        (None, (), CandidateStatus.REJECTED, "missing_primary_class"),
    ],
)
def test_primary_class_reconciliation_rejections(
    infobox_class: str | None,
    categories: tuple[str, ...],
    status: CandidateStatus,
    code: str,
) -> None:
    taxonomy, page = taxonomy_for(*categories)
    class_parameter = f"|class={infobox_class}" if infobox_class else ""
    result = parse_wiki_vehicle(
        make_revision(
            "{{Vehicle|id=Tiger|name=Tiger" + class_parameter + "|nation=Germany|tier=7}}"
        ),
        page,
        taxonomy,
    )
    assert result.status is status
    assert result.diagnostics[0].code == code


def test_class_fallback_and_missing_taxonomy_diagnostics() -> None:
    taxonomy, page = taxonomy_for("category:heavy tanks")
    fallback = parse_wiki_vehicle(
        make_revision("{{Vehicle|id=Tiger|name=Tiger|nation=Germany|tier=7}}"),
        page,
        taxonomy,
    )
    no_taxonomy, no_taxonomy_page = taxonomy_for()
    infobox_only = parse_wiki_vehicle(
        make_revision("{{Vehicle|id=Tiger|name=Tiger|class=heavy tanks|nation=Germany|tier=7}}"),
        no_taxonomy_page,
        no_taxonomy,
    )
    assert fallback.vehicle is not None
    assert fallback.vehicle.metadata.primary_class == "Heavy Tanks"
    assert fallback.diagnostics[0].code == "infobox_class_fallback"
    assert infobox_only.status is CandidateStatus.ACCEPTED
    assert infobox_only.diagnostics[0].code == "missing_taxonomy_class_evidence"


def test_fixture_covers_all_canonical_nations_and_unknown_is_rejected() -> None:
    fixture = json.loads((FIXTURES / "wiki_nation_aliases.json").read_text(encoding="utf-8"))
    assert set(fixture) == CANONICAL_NATIONS
    for canonical, aliases in fixture.items():
        assert all(canonicalize_nation(alias) == canonical for alias in aliases)
    assert canonicalize_nation("Atlantis") is None

    taxonomy, page = taxonomy_for("category:heavy tanks")
    rejected = parse_wiki_vehicle(
        make_revision("{{Vehicle|id=Tiger|name=Tiger|class=heavy tanks|nation=Atlantis|tier=7}}"),
        page,
        taxonomy,
    )
    assert rejected.status is CandidateStatus.REJECTED
    assert rejected.diagnostics[0].code in {"unknown_infobox_class", "unknown_nation"}


@pytest.mark.parametrize("tier", ["0", "11", "IV", "1.0", ""])
def test_tier_must_be_decimal_one_through_ten(tier: str) -> None:
    taxonomy, page = taxonomy_for("category:heavy tanks")
    result = parse_wiki_vehicle(
        make_revision(
            "{{Vehicle|id=Tiger|name=Tiger|class=heavy tanks"
            + f"|nation=Germany|tier={tier}"
            + "}}"
        ),
        page,
        taxonomy,
    )
    assert result.status is CandidateStatus.REJECTED
    assert result.diagnostics[0].code in {"invalid_tier", "missing_tier"}


def test_only_documented_template_aliases_are_vehicle_infoboxes() -> None:
    taxonomy, page = taxonomy_for("category:heavy tanks")
    result = parse_wiki_vehicle(
        make_revision("{{Tank info|id=Tiger|name=Tiger|class=heavy tanks|nation=Germany|tier=7}}"),
        page,
        taxonomy,
    )
    assert result.status is CandidateStatus.NON_VEHICLE


def test_live_tankdata_uses_taxonomy_and_inthegame_parameters() -> None:
    revision = fetch_wiki_revision(
        FakeClient(revision_fixtures()["tankdata_live"]),
        "https://wiki.test/api.php",
        "Tank:A01 T1 Cunningham",
    )
    taxonomy, page = taxonomy_for(
        "category:usa tanks",
        "category:tier i tanks",
        "category:light tanks",
        title="Tank:A01 T1 Cunningham",
    )

    result = parse_wiki_vehicle(revision, page, taxonomy)

    assert result.status is CandidateStatus.ACCEPTED
    assert result.vehicle is not None
    assert result.vehicle.metadata.name == "T1_Cunningham"
    assert result.vehicle.metadata.display_name == "T1 Cunningham"
    assert result.vehicle.metadata.nation == "USA"
    assert result.vehicle.metadata.tier == 1
    assert result.vehicle.metadata.primary_class == "Light Tanks"
    titles = [section.title for section in result.vehicle.tactical_sections]
    assert titles == ["Performance", "Tactics"]
    assert "hull-down" in result.vehicle.tactical_sections[0].text
    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "infobox_class_fallback",
        "infobox_nation_fallback",
        "infobox_tier_fallback",
    }


def test_nation_and_tier_taxonomy_conflicts_are_rejected() -> None:
    taxonomy, page = taxonomy_for(
        "category:usa tanks",
        "category:uk tanks",
        "category:light tanks",
    )
    nation_conflict = parse_wiki_vehicle(
        make_revision("{{TankData|Tank=T1_Cunningham|name=T1 Cunningham}}"),
        page,
        taxonomy,
    )
    tier_taxonomy, tier_page = taxonomy_for(
        "category:usa tanks",
        "category:tier i tanks",
        "category:tier ii tanks",
        "category:light tanks",
    )
    tier_conflict = parse_wiki_vehicle(
        make_revision("{{TankData|Tank=T1_Cunningham|name=T1 Cunningham}}"),
        tier_page,
        tier_taxonomy,
    )
    usa_taxonomy, usa_page = taxonomy_for("category:usa tanks", "category:heavy tanks")
    mismatched = parse_wiki_vehicle(
        make_revision(
            "{{Vehicle|id=Tiger|name=Tiger|class=heavy tanks|nation=Germany|tier=7}}"
        ),
        usa_page,
        usa_taxonomy,
    )

    assert nation_conflict.status is CandidateStatus.REJECTED
    assert nation_conflict.diagnostics[0].code == "contradictory_taxonomy_nations"
    assert tier_conflict.status is CandidateStatus.REJECTED
    assert tier_conflict.diagnostics[0].code == "contradictory_taxonomy_tiers"
    assert mismatched.status is CandidateStatus.REJECTED
    assert mismatched.diagnostics[0].code == "nation_conflict"
