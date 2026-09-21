from __future__ import annotations

import pytest

from tank_graph_extractor.__main__ import _configuration_from_args, build_parser


def test_extract_cli_builds_noninteractive_production_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TANK_GRAPH_CONTACT", raising=False)
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


def test_extract_cli_fails_fast_without_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TANK_GRAPH_CONTACT", raising=False)
    args = build_parser().parse_args(["extract"])
    with pytest.raises(ValueError, match="missing maintainer contact"):
        _configuration_from_args(args)


def test_cli_help_contains_copyable_extract_example() -> None:
    help_text = build_parser().format_help()
    assert "tank-graph-extract extract --contact" in help_text
