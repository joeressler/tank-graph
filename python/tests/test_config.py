from pathlib import Path

import pytest

from tank_graph_extractor.config import (
    DEFAULT_GUIDE_ROOT,
    DEFAULT_USER_AGENT,
    DEFAULT_WIKI_ENDPOINT,
    ExtractorConfig,
    normalized_url_path,
    validate_user_agent,
)
from tank_graph_extractor.errors import ConfigurationError


def test_defaults_match_milestone_contract() -> None:
    config = ExtractorConfig()

    assert config.wiki_endpoint == DEFAULT_WIKI_ENDPOINT
    assert config.guide_root == DEFAULT_GUIDE_ROOT
    assert config.root_category == "Category:Tanks"
    assert config.nations == ()
    assert config.user_agent == DEFAULT_USER_AGENT
    assert config.contact == "joe.a.ressler+tankgraph@gmail.com"
    assert config.request_interval == 5.0
    assert config.cookie_refresh_every == 5
    assert config.timeout == (10.0, 30.0)
    assert config.max_attempts == 5
    assert config.output_path == Path("data/tanks_data.json")
    assert config.guide_paths == ("newcomers-guide/getting_started/",)


@pytest.mark.parametrize(
    "user_agent",
    [
        "",
        "python-requests/2",
        "WoTGraphBot/1.0 (contact: )",
        "WoTGraphBot/1.0 (contact: user@example.com)",
        "WoTGraphBot/1.0 (contact: user@subdomain.invalid)",
        "WoTGraphBot/1.0 (contact: changeme@real.test)",
        "WoTGraphBot/1.0 (contact: http://project.invalid/contact)",
        "WoTGraphBot/1.0 (contact: https://localhost/contact)",
    ],
)
def test_user_agent_rejects_missing_malformed_or_placeholder_contact(user_agent: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_user_agent(user_agent)


@pytest.mark.parametrize(
    "contact",
    ["maintainer@tankgraph.dev", "https://github.com/joeressler/tank-graph"],
)
def test_user_agent_accepts_real_contact_forms(contact: str) -> None:
    value = f"WoTGraphBot/2.4.1 (contact: {contact})"
    assert validate_user_agent(value) == value


def test_production_rejects_http_and_short_interval() -> None:
    with pytest.raises(ConfigurationError, match="MediaWiki endpoint"):
        ExtractorConfig(wiki_endpoint="http://wiki.test/api.php")
    with pytest.raises(ConfigurationError, match="at least 5.0"):
        ExtractorConfig(request_interval=4.999)


def test_test_mode_allows_http_and_zero_interval() -> None:
    config = ExtractorConfig(
        wiki_endpoint="http://wiki.test/api.php",
        guide_root="http://guide.test/en/content/guide/",
        request_interval=0,
        test_mode=True,
    )

    assert config.request_interval == 0
    assert config.source_kind("http://wiki.test/api.php") == "wiki"
    assert config.source_kind("http://wiki.test/robots.txt", allow_robots=True) == "robots"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connect_timeout", 0),
        ("read_timeout", float("inf")),
        ("request_interval", float("nan")),
        ("request_interval", True),
        ("connect_timeout", True),
        ("max_response_bytes", 0),
        ("max_response_bytes", 1.5),
        ("max_attempts", 4),
        ("cookie_refresh_every", True),
        ("cookie_refresh_every", -1),
    ],
)
def test_numeric_safety_constraints(field: str, value: object) -> None:
    with pytest.raises(ConfigurationError):
        ExtractorConfig(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "guide_path",
    [
        "",
        "/outside/",
        "../outside/",
        r"..\outside",
        "safe/%2e%2e/outside/",
    ],
)
def test_guide_paths_cannot_escape_root(guide_path: str) -> None:
    with pytest.raises(ConfigurationError):
        ExtractorConfig(guide_paths=(guide_path,))


def test_blank_nation_filter_entries_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="nation filter"):
        ExtractorConfig(nations=("USA", "  "))


def test_url_classification_rejects_unapproved_routes_and_credentials() -> None:
    config = ExtractorConfig()

    with pytest.raises(ConfigurationError):
        config.source_kind("https://wiki.wargaming.net/other")
    with pytest.raises(ConfigurationError):
        config.source_kind("https://user:secret@wiki.wargaming.net/api.php")
    with pytest.raises(ConfigurationError):
        config.source_kind("https://worldoftanks.com/en/content/guide/not-allowlisted/")


def test_redirect_rules_preserve_host_and_guide_root() -> None:
    config = ExtractorConfig()
    guide = config.guide_urls[0]

    config.validate_redirect("guide", guide, f"{guide}?tracking=discarded")
    with pytest.raises(ConfigurationError, match="host"):
        config.validate_redirect("wiki", config.wiki_endpoint, "https://attacker.test/api.php")
    with pytest.raises(ConfigurationError, match="guide path"):
        config.validate_redirect("guide", guide, "https://worldoftanks.com/en/store/")


def test_path_normalization_decodes_encoded_traversal() -> None:
    assert normalized_url_path("https://host.test/a/%252e%252e/b/") == "/b/"
