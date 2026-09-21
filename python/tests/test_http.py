from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import pytest
import requests

from tank_graph_extractor.config import ExtractorConfig
from tank_graph_extractor.errors import (
    ConfigurationError,
    HttpStatusError,
    NetworkError,
    RedirectError,
    RedirectPolicyError,
    ResponseTooLargeError,
    RetryExhaustedError,
    safe_url,
)
from tank_graph_extractor.http import HttpClient, parse_retry_after


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.sleeps.append(duration)
        self.value += duration

    def advance(self, duration: float) -> None:
        self.value += duration


class ScriptedSession:
    def __init__(self, events: list[requests.Response | BaseException | Callable[[], Any]]) -> None:
        self.events = list(events)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({"url": url, **kwargs})
        if not self.events:
            raise AssertionError("unexpected request")
        event = self.events.pop(0)
        if callable(event):
            event = event()
        if isinstance(event, BaseException):
            raise event
        return event

    def close(self) -> None:
        self.closed = True


def response(
    status: int = 200,
    *,
    body: bytes = b"ok",
    url: str = "https://wiki.wargaming.net/api.php",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.headers.update(headers or {})
    result._content = body
    result._content_consumed = True
    result.encoding = "utf-8"
    return result


def config(**changes: Any) -> ExtractorConfig:
    values: dict[str, Any] = {"test_mode": True}
    values.update(changes)
    return ExtractorConfig(**values)


def client_for(
    events: list[requests.Response | BaseException | Callable[[], Any]],
    *,
    clock: FakeClock | None = None,
    client_config: ExtractorConfig | None = None,
    jitter: Callable[[float], float] = lambda _base: 0.0,
    wall_clock: Callable[[], float] = lambda: 0.0,
) -> tuple[HttpClient, ScriptedSession, FakeClock]:
    fake_clock = clock or FakeClock()
    session = ScriptedSession(events)
    client = HttpClient(
        client_config or config(),
        session=session,
        clock=fake_clock,
        wall_clock=wall_clock,
        sleep=fake_clock.sleep,
        jitter=jitter,
    )
    return client, session, fake_clock


def test_first_attempt_is_immediate_and_hosts_share_completion_interval() -> None:
    clock = FakeClock()

    def timed_result() -> requests.Response:
        clock.advance(2.0)
        return response()

    client, session, _ = client_for([timed_result, response()], clock=clock)
    client.get(config().wiki_endpoint)
    client.get(config().guide_urls[0])

    assert clock.value == 7.0
    assert clock.sleeps == [5.0]
    assert [call["url"] for call in session.calls] == [
        config().wiki_endpoint,
        config().guide_urls[0],
    ]


def test_request_options_are_finite_streamed_and_redirects_are_manual() -> None:
    client, session, _ = client_for([response()])
    client.get(config().wiki_endpoint, params={"action": "query"})

    call = session.calls[0]
    assert call["params"] == {"action": "query"}
    assert call["headers"] == {"User-Agent": config().user_agent}
    assert call["timeout"] == config().timeout
    assert call["stream"] is True
    assert call["allow_redirects"] is False


@pytest.mark.parametrize("status", [429, 500, 501, 550, 599])
def test_every_retryable_status_family_retries(status: int) -> None:
    client, session, _ = client_for([response(status), response()])

    assert client.get(config().wiki_endpoint).status_code == 200
    assert len(session.calls) == 2
    assert client.retry_count == 1


def test_wiki_html_interstitial_is_retried_then_succeeds() -> None:
    html = response(
        headers={"Content-Type": "text/html; charset=UTF-8"},
        body=b"<!DOCTYPE html><title>Loading site please wait...</title>",
    )
    client, session, _ = client_for([html, response(body=b'{"ok":true}')])

    result = client.get(config().wiki_endpoint)

    assert result.status_code == 200
    assert result.content == b'{"ok":true}'
    assert len(session.calls) == 2
    assert client.retry_count == 1


def test_wiki_html_interstitial_exhausts_retry_budget() -> None:
    html = response(
        headers={"Content-Type": "text/html; charset=UTF-8"},
        body=b"<!DOCTYPE html><title>Loading site please wait...</title>",
    )
    client, session, _ = client_for([html] * 5)

    with pytest.raises(RetryExhaustedError, match="HTML interstitial") as caught:
        client.get(config().wiki_endpoint)

    assert caught.value.attempts == 5
    assert len(session.calls) == 5


def test_guide_html_is_not_treated_as_interstitial() -> None:
    client_config = config()
    html = response(
        url=client_config.guide_urls[0],
        headers={"Content-Type": "text/html; charset=UTF-8"},
        body=b"<!DOCTYPE html><title>Guide</title>",
    )
    client, session, _ = client_for([html], client_config=client_config)

    result = client.get(client_config.guide_urls[0])

    assert result.status_code == 200
    assert len(session.calls) == 1
    assert client.retry_count == 0


def test_retry_and_limiter_delays_are_both_satisfied() -> None:
    client, session, clock = client_for([response(500), response()])

    client.get(config().wiki_endpoint)

    assert len(session.calls) == 2
    assert clock.sleeps == [1.0, 4.0]
    assert clock.value == 5.0


def test_retry_exhaustion_is_exactly_five_total_attempts() -> None:
    client, session, _ = client_for([response(503) for _ in range(5)])

    with pytest.raises(RetryExhaustedError) as caught:
        client.get(config().wiki_endpoint)

    assert caught.value.attempts == 5
    assert caught.value.status_code == 503
    assert len(session.calls) == 5


@pytest.mark.parametrize("failure", [requests.Timeout(), requests.ConnectionError()])
def test_transient_network_failures_retry_and_exhaust(failure: BaseException) -> None:
    client, session, _ = client_for([failure for _ in range(5)])

    with pytest.raises(NetworkError) as caught:
        client.get(config().wiki_endpoint)

    assert caught.value.attempts == 5
    assert len(session.calls) == 5


def test_other_request_failures_are_not_retried() -> None:
    client, session, _ = client_for([requests.TooManyRedirects()])

    with pytest.raises(NetworkError) as caught:
        client.get(config().wiki_endpoint)

    assert caught.value.attempts == 1
    assert len(session.calls) == 1


def test_fatal_4xx_fails_immediately() -> None:
    client, session, _ = client_for([response(404)])

    with pytest.raises(HttpStatusError) as caught:
        client.get(config().wiki_endpoint)

    assert caught.value.status_code == 404
    assert len(session.calls) == 1


def test_explicit_accepted_status_is_returned() -> None:
    client, _, _ = client_for([response(404)])
    assert client.get(config().wiki_endpoint, accepted_statuses={404}).status_code == 404


def test_retry_after_delta_and_http_date_are_parsed() -> None:
    now = datetime(2026, 9, 21, tzinfo=UTC).timestamp()
    date = format_datetime(datetime(2026, 9, 21, 0, 1, tzinfo=UTC), usegmt=True)

    assert parse_retry_after("17", now=lambda: now) == 17.0
    assert parse_retry_after(date, now=lambda: now) == 60.0
    assert parse_retry_after("not-a-date", now=lambda: now) is None
    assert parse_retry_after("9" * 10_000, now=lambda: now) is None


def test_retry_after_larger_than_backoff_is_honored() -> None:
    client, _, clock = client_for([response(429, headers={"Retry-After": "75"}), response()])

    client.get(config().wiki_endpoint)

    assert clock.sleeps[0] == 75.0


def test_get_attempts_log_safe_wiki_titles_without_query_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _, _ = client_for([response()])

    with caplog.at_level(logging.INFO):
        client.get(
            config().wiki_endpoint,
            params={"titles": "Tank:Tiger I", "token": "top-secret"},
        )

    assert "http: wiki GET wiki.wargaming.net/api.php titles=Tank:Tiger I" in caplog.text
    assert "top-secret" not in caplog.text
    assert "token" not in caplog.text


def test_invalid_retry_after_is_safely_logged(caplog: pytest.LogCaptureFixture) -> None:
    client, _, _ = client_for(
        [response(503, headers={"Retry-After": "secret?token=value"}), response()]
    )

    with caplog.at_level(logging.WARNING):
        client.get(config().wiki_endpoint)

    assert "wiki.wargaming.net/api.php" in caplog.text
    assert "token=value" not in caplog.text


def test_backoff_and_injected_jitter_are_bounded_at_sixty() -> None:
    client, _, _ = client_for([], jitter=lambda _base: 1000.0)
    assert client._retry_delay(10, None, config().wiki_endpoint) == 60.0


def test_invalid_injected_jitter_fails_configuration_safely() -> None:
    client, _, _ = client_for([response(500)], jitter=lambda _base: float("nan"))
    with pytest.raises(ConfigurationError):
        client.get(config().wiki_endpoint)


def test_content_length_and_streamed_bytes_enforce_finite_limit() -> None:
    small_config = config(max_response_bytes=3)
    announced, _, _ = client_for(
        [response(headers={"Content-Length": "4"})],
        client_config=small_config,
    )
    streamed, _, _ = client_for(
        [response(body=b"four")],
        client_config=small_config,
    )

    with pytest.raises(ResponseTooLargeError):
        announced.get(small_config.wiki_endpoint)
    with pytest.raises(ResponseTooLargeError):
        streamed.get(small_config.wiki_endpoint)


def test_same_host_redirect_succeeds_and_counts_for_limiter() -> None:
    redirected = "https://wiki.wargaming.net/canonical-api.php"
    client, session, clock = client_for(
        [
            response(301, headers={"Location": redirected}),
            response(url=redirected),
        ]
    )

    result = client.get(config().wiki_endpoint)

    assert result.status_code == 200
    assert [call["url"] for call in session.calls] == [config().wiki_endpoint, redirected]
    assert clock.sleeps == [5.0]


def test_redirect_rejects_off_host_before_following() -> None:
    client, session, _ = client_for(
        [response(302, headers={"Location": "https://attacker.test/collect"})]
    )

    with pytest.raises(RedirectPolicyError):
        client.get(config().wiki_endpoint)

    assert len(session.calls) == 1


def test_guide_redirect_rejects_path_outside_guide_root() -> None:
    client_config = config()
    client, session, _ = client_for(
        [
            response(
                302,
                url=client_config.guide_urls[0],
                headers={"Location": "/en/store/"},
            )
        ],
        client_config=client_config,
    )

    with pytest.raises(RedirectPolicyError):
        client.get(client_config.guide_urls[0])

    assert len(session.calls) == 1


def test_redirect_loop_is_terminal() -> None:
    client, _, _ = client_for([response(302, headers={"Location": config().wiki_endpoint})])
    with pytest.raises(RedirectError, match="loop"):
        client.get(config().wiki_endpoint)


def test_guide_redirect_discards_query_and_fragment() -> None:
    client_config = config()
    guide = client_config.guide_urls[0]
    canonical = f"{guide}canonical/"
    client, session, _ = client_for(
        [
            response(
                302,
                url=guide,
                headers={"Location": f"{canonical}?utm_source=secret#section"},
            ),
            response(url=canonical),
        ],
        client_config=client_config,
    )

    client.get(guide)

    assert session.calls[1]["url"] == canonical


def test_error_text_never_contains_query_values_or_response_body() -> None:
    client, _, _ = client_for([response(400, body=b"credential=top-secret")])

    with pytest.raises(HttpStatusError) as caught:
        client.get(config().wiki_endpoint, params={"token": "top-secret"})

    rendered = str(caught.value)
    assert "top-secret" not in rendered
    assert "token" not in rendered
    assert rendered.endswith("[wiki.wargaming.net/api.php]")


def test_source_kind_hint_must_match_configured_url() -> None:
    client, session, _ = client_for([response()])

    with pytest.raises(ConfigurationError, match="source kind"):
        client.get(config().wiki_endpoint, source_kind="guide")

    assert not session.calls


def test_safe_url_handles_malformed_ports_without_leaking() -> None:
    assert safe_url("https://host.test:not-a-port/path?token=secret") == "<invalid-host>/"


def test_context_manager_closes_injected_session() -> None:
    client, session, _ = client_for([])
    with client:
        pass
    assert session.closed is True
