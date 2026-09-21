from __future__ import annotations

from typing import Any

import pytest
import requests

from tank_graph_extractor.config import ExtractorConfig
from tank_graph_extractor.errors import (
    PolicyDisallowedError,
    PolicyFetchError,
)
from tank_graph_extractor.http import HttpClient
from tank_graph_extractor.policy import PolicyChecker


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


class ScriptedSession:
    def __init__(self, responses: list[requests.Response]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> requests.Response:
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("unexpected robots request")
        result = self.responses.pop(0)
        result.url = url
        return result

    def close(self) -> None:
        pass


def response(status: int, body: str = "") -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    result._content_consumed = True
    result.encoding = "utf-8"
    return result


def make_checker(
    responses: list[requests.Response],
    **config_changes: Any,
) -> tuple[PolicyChecker, ScriptedSession, ExtractorConfig]:
    config = ExtractorConfig(test_mode=True, request_interval=0, **config_changes)
    clock = FakeClock()
    session = ScriptedSession(responses)
    client = HttpClient(
        config,
        session=session,
        clock=clock,
        wall_clock=clock,
        sleep=clock.sleep,
        jitter=lambda _base: 0,
    )
    return PolicyChecker(config, client), session, config


def test_policy_checks_use_same_client_and_skip_only_disallowed_guides() -> None:
    wiki_robots = """
User-agent: *
Allow: /api.php
"""
    guide_robots = """
User-agent: *
Disallow: /en/content/guide/newcomers-guide/
"""
    checker, session, config = make_checker(
        [response(200, wiki_robots), response(200, guide_robots)],
        guide_paths=(
            "newcomers-guide/getting_started/",
            "tank-coach-video-guides/",
        ),
    )

    report = checker.check_sources()

    assert report.allowed_guides == config.guide_urls[1:]
    assert tuple(skip.url for skip in report.skipped_guides) == (config.guide_urls[0],)
    assert session.calls == [
        "https://wiki.wargaming.net/robots.txt",
        "https://worldoftanks.com/robots.txt",
    ]


def test_required_wiki_disallow_fails_run() -> None:
    checker, session, _ = make_checker(
        [
            response(
                200,
                """
User-agent: *
Disallow: /api.php
""",
            )
        ]
    )

    with pytest.raises(PolicyDisallowedError, match="MediaWiki root category"):
        checker.check_sources()

    assert len(session.calls) == 1


def test_unavailable_required_wiki_policy_fails_after_bounded_retries() -> None:
    checker, session, _ = make_checker([response(503) for _ in range(5)])

    with pytest.raises(PolicyFetchError, match="unable to determine"):
        checker.check_sources()

    assert len(session.calls) == 5


def test_all_guides_disallowed_fails_run() -> None:
    checker, _, _ = make_checker(
        [
            response(404),
            response(
                200,
                """
User-agent: *
Disallow: /en/content/guide/
""",
            ),
        ]
    )

    with pytest.raises(PolicyDisallowedError, match="no configured guide"):
        checker.check_sources()


def test_robots_404_means_allow_all_and_is_cached_per_origin() -> None:
    checker, session, config = make_checker([response(404), response(404)])

    report = checker.check_sources()

    assert report.allowed_guides == config.guide_urls
    assert not report.skipped_guides
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_robots_auth_denial_disallows_all_guides(status: int) -> None:
    checker, _, _ = make_checker([response(404), response(status)])

    with pytest.raises(PolicyDisallowedError, match="no configured guide"):
        checker.check_sources()


def test_optional_policy_fetch_failure_is_reported_then_all_guides_failure() -> None:
    checker, session, _ = make_checker([response(404), *[response(500) for _ in range(5)]])

    with pytest.raises(PolicyDisallowedError, match="no configured guide"):
        checker.check_sources()

    assert len(session.calls) == 6


def test_require_allowed_can_guard_individual_source_requests() -> None:
    checker, _, config = make_checker(
        [
            response(
                200,
                """
User-agent: *
Disallow: /api.php
""",
            )
        ]
    )

    with pytest.raises(PolicyDisallowedError):
        checker.require_allowed(config.wiki_endpoint, description="wiki endpoint")
