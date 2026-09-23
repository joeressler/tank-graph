from __future__ import annotations

import logging

import pytest

from tank_graph_extractor.config import ExtractorConfig
from tank_graph_extractor.errors import ConfigurationError
from tank_graph_extractor.interstitial import (
    is_bot_interstitial,
    is_hard_waf_block,
    looks_like_mediawiki_html,
    looks_like_mediawiki_json,
    strip_marketing_cookies,
    validate_wiki_cookie,
)

INTERSTITIAL_HTML = """
<!DOCTYPE html>
<html>
<head><title>Loading site please wait...</title></head>
<body>
<div id="loading-content">
  <div id="JSCookieMSG"></div>
  <div id="sbbhscc"></div>
</div>
</body>
</html>
"""

BLOCKED_HTML = """
<html><head><title>Access</title></head>
<body><p>Sorry, you have been blocked.</p></body></html>
"""

ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Tiger I</title></head>
<body>
<div id="mw-content-text">
  <div class="mw-parser-output"><p>The Tiger I is a German heavy tank.</p></div>
</div>
</body>
</html>
"""


@pytest.mark.parametrize(
    "body",
    [
        INTERSTITIAL_HTML,
        BLOCKED_HTML,
        '<div id="loading-content"></div>',
        '<div id="JSCookieMSG"></div>',
        "sbbhscc challenge",
        "LOADING SITE PLEASE WAIT",
    ],
)
def test_interstitial_markers_are_blocked(body: str) -> None:
    assert is_bot_interstitial(body) is True
    assert looks_like_mediawiki_html(body) is False


def test_real_article_html_is_not_an_interstitial() -> None:
    assert is_bot_interstitial(ARTICLE_HTML) is False
    assert looks_like_mediawiki_html(ARTICLE_HTML) is True


def test_hard_waf_block_is_distinct_from_the_wait_spinner() -> None:
    assert is_hard_waf_block(BLOCKED_HTML) is True
    assert is_hard_waf_block("Incident Reference ID: abc") is True
    assert is_hard_waf_block(INTERSTITIAL_HTML) is False
    assert is_hard_waf_block(None) is False


def test_empty_bodies_are_not_interstitials() -> None:
    assert is_bot_interstitial(None) is False
    assert is_bot_interstitial("") is False
    assert looks_like_mediawiki_html(None) is False
    assert looks_like_mediawiki_json(None) is False


def test_mediawiki_json_is_recognized() -> None:
    assert looks_like_mediawiki_json('{"query":{"allpages":[]}}') is True
    assert looks_like_mediawiki_json(INTERSTITIAL_HTML) is False


def test_validate_wiki_cookie_rejects_marketing_and_truncated_values() -> None:
    with pytest.raises(ConfigurationError, match="truncated"):
        validate_wiki_cookie("SPSI=aaa; SPSE=bbb; ...")
    with pytest.raises(ConfigurationError, match="truncated"):
        validate_wiki_cookie("SPSI=aaa; …")
    with pytest.raises(ConfigurationError, match="marketing"):
        validate_wiki_cookie("OptanonConsent=foo; OptanonAlertBoxClosed=bar")
    assert validate_wiki_cookie("SPSI=aaa; SPSE=bbb; spcsrf=ccc") == (
        "SPSI=aaa; SPSE=bbb; spcsrf=ccc"
    )
    mixed = "SPSI=aaa; OptanonConsent=foo; SPSE=bbb"
    assert validate_wiki_cookie(mixed) == mixed
    assert strip_marketing_cookies(mixed) == "SPSI=aaa; SPSE=bbb"


def test_validate_wiki_cookie_warns_when_pass_cookies_are_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        validate_wiki_cookie("sessionid=not-a-wiki-pass")
    assert "SPSI" in caplog.text


def test_config_rejects_marketing_wiki_cookie() -> None:
    with pytest.raises(ConfigurationError, match="marketing"):
        ExtractorConfig(wiki_cookie="OptanonConsent=from-worldoftanks.com")
