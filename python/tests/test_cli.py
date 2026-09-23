from __future__ import annotations

import pytest

from tank_graph_extractor.__main__ import _configuration_from_args, build_parser


def test_extract_cli_builds_noninteractive_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TANK_GRAPH_CONTACT", raising=False)
    monkeypatch.delenv("WIKI_COOKIE", raising=False)
    args = build_parser().parse_args(
        [
            "extract",
            "--contact",
            "maintainer@tankgraph.org",
            "--output",
            "data/custom.json",
        ]
    )
    config = _configuration_from_args(args)
    assert config.contact == "maintainer@tankgraph.org"
    assert config.output_path.as_posix() == "data/custom.json"
    assert config.request_interval == 5.0
    assert config.root_category == "Category:USA Tanks"
    assert config.nations == ("USA",)
    assert config.wiki_cookie is None
    assert config.cookie_refresh_every == 5


def test_extract_cli_accepts_a_slower_request_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TANK_GRAPH_CONTACT", raising=False)
    args = build_parser().parse_args(
        [
            "extract",
            "--contact",
            "maintainer@tankgraph.org",
            "--request-interval",
            "10",
            "--cookie-refresh-every",
            "0",
        ]
    )
    config = _configuration_from_args(args)
    assert config.request_interval == 10.0
    assert config.cookie_refresh_every == 0


def test_extract_cli_reads_wiki_cookie_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TANK_GRAPH_CONTACT", raising=False)
    monkeypatch.setenv("WIKI_COOKIE", "SPSI=abc; SPSE=def")
    args = build_parser().parse_args(["extract", "--contact", "maintainer@tankgraph.org"])
    config = _configuration_from_args(args)
    assert config.wiki_cookie == "SPSI=abc; SPSE=def"


def test_extract_cli_fails_fast_without_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TANK_GRAPH_CONTACT", raising=False)
    args = build_parser().parse_args(["extract"])
    with pytest.raises(ValueError, match="missing maintainer contact"):
        _configuration_from_args(args)


def test_extract_cli_nation_all_disables_the_temporary_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TANK_GRAPH_CONTACT", raising=False)
    monkeypatch.delenv("WIKI_COOKIE", raising=False)
    args = build_parser().parse_args(
        [
            "extract",
            "--contact",
            "maintainer@tankgraph.org",
            "--root-category",
            "Category:Tanks",
            "--nation",
            "ALL",
        ]
    )
    config = _configuration_from_args(args)
    assert config.root_category == "Category:Tanks"
    assert config.nations == ()


def test_cli_help_contains_copyable_extract_example() -> None:
    help_text = build_parser().format_help()
    assert "tank-graph-extract extract --contact" in help_text
