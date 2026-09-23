from pathlib import Path

import pytest

from tank_graph_extractor.guides import (
    CANONICAL_CLASSES,
    GuideParseError,
    discover_allowlisted_children,
    normalize_guide_url,
    parse_guide_html,
    validate_guide_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = "https://worldoftanks.com/en/content/guide/"
NEWCOMER = f"{ROOT}newcomers-guide/getting_started/"
COACH = f"{ROOT}tank-coach-video-guides/"
RESEARCH = f"{ROOT}tank-coach-video-guides/tank-coach-research/"
COACH_ALLOWLIST = (
    "newcomers-guide/getting_started/",
    "tank-coach-video-guides/",
    "tank-coach-video-guides/tank-coach-research/",
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_url_identity_strips_fragment_and_tracking_but_enforces_boundaries() -> None:
    assert normalize_guide_url(f"{NEWCOMER}?utm_source=test&gclid=abc#aiming") == NEWCOMER
    assert validate_guide_url(f"{NEWCOMER}?utm_campaign=test") == NEWCOMER
    with pytest.raises(GuideParseError, match="approved host"):
        validate_guide_url("https://example.org/en/content/guide/newcomers-guide/getting_started/")
    with pytest.raises(GuideParseError, match="approved path"):
        validate_guide_url(f"{ROOT}%2e%2e/private/")


def test_discovery_intersects_links_with_the_existing_allowlist() -> None:
    children = discover_allowlisted_children(
        fixture("guide_tank_coach.html"),
        COACH,
        allowlist=COACH_ALLOWLIST,
    )
    assert children == (RESEARCH,)


def test_current_tank_coach_index_is_valid_only_when_it_discovers_allowlisted_child() -> None:
    result = parse_guide_html(
        fixture("guide_tank_coach_current.html"),
        COACH,
        allowlist=COACH_ALLOWLIST,
    )

    assert result.segments == ()
    assert result.discovered_children == (RESEARCH,)


def test_parser_removes_chrome_and_assigns_only_explicit_scope() -> None:
    result = parse_guide_html(fixture("guide_newcomer.html"), NEWCOMER)

    assert result.title == "Getting Started"
    assert any(
        segment.applicable_classes == frozenset({"Heavy Tanks"}) for segment in result.segments
    )
    assert any(segment.applicable_classes == CANONICAL_CLASSES for segment in result.segments)
    assert any("view range" in diagnostic.text.casefold() for diagnostic in result.diagnostics)
    retained = " ".join(segment.text for segment in result.segments).casefold()
    assert "store events account" not in retained
    assert "premium vehicles" not in retained


def test_current_newcomer_layout_keeps_only_universal_loadout_advice() -> None:
    result = parse_guide_html(fixture("guide_newcomer_current.html"), NEWCOMER)

    assert [segment.text for segment in result.segments] == [
        (
            "Choose crew members, equipment, ammunition, and consumables that "
            "improve your vehicle's performance before entering battle."
        )
    ]
    assert result.segments[0].applicable_classes == CANONICAL_CLASSES
    retained = " ".join(segment.text for segment in result.segments).casefold()
    assert "bootcamp" not in retained
    assert "store" not in retained
    assert "daily objectives" not in retained


def test_parser_rejects_access_documents_and_layout_drift() -> None:
    with pytest.raises(GuideParseError, match="access or error"):
        parse_guide_html(fixture("guide_access_denied.html"), NEWCOMER)
    with pytest.raises(GuideParseError, match="content root"):
        parse_guide_html(
            "<html><head><title>Guide</title></head><body>Tactics and armor.</body></html>",
            NEWCOMER,
        )
    with pytest.raises(GuideParseError, match="interstitial"):
        parse_guide_html(
            '<html><title>Loading site please wait...</title>'
            '<div id="loading-content"></div></html>',
            NEWCOMER,
        )
    assert (
        discover_allowlisted_children(
            (
                '<html><title>Loading site please wait...</title>'
                '<a href="tank-coach-video-guides/tank-coach-research/">x</a>'
            ),
            NEWCOMER,
        )
        == ()
    )
    with pytest.raises(GuideParseError, match="implausibly short"):
        parse_guide_html(
            "<html><title>Guide</title><main><h1>Tactics</h1>"
            "<p>Use armor and cover.</p></main></html>",
            NEWCOMER,
        )
