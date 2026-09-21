"""Shared rate-limited HTTP transport for every extractor source."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Collection, Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests

from .config import ExtractorConfig
from .errors import (
    ConfigurationError,
    HttpStatusError,
    NetworkError,
    RedirectError,
    RedirectPolicyError,
    ResponseTooLargeError,
    RetryExhaustedError,
    safe_url,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class AttemptLimiter:
    """Separate attempt starts from the prior attempt's completion."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._previous_completion: float | None = None
        self._lock = threading.Lock()

    @property
    def previous_completion(self) -> float | None:
        return self._previous_completion

    def wait(self) -> None:
        with self._lock:
            if self._previous_completion is None:
                return
            remaining = self._interval - (self._clock() - self._previous_completion)
            if remaining > 0:
                self._sleep(remaining)

    def record_completion(self) -> None:
        with self._lock:
            self._previous_completion = self._clock()


def parse_retry_after(value: str | None, *, now: Callable[[], float] = time.time) -> float | None:
    """Parse Retry-After delta-seconds or an HTTP date."""
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isascii() and stripped.isdecimal():
        try:
            delay = float(int(stripped))
        except (OverflowError, ValueError):
            return None
        return delay if delay < float("inf") else None
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return max(0.0, parsed.timestamp() - now())
    except (OSError, OverflowError, ValueError):
        return None


def _default_jitter(base_delay: float) -> float:
    return random.uniform(0.0, min(1.0, base_delay * 0.25))


def _without_fragment(url: str) -> str:
    return urldefrag(url).url


def _without_query_or_fragment(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _request_label(url: str, params: Mapping[str, Any] | None) -> str:
    """Describe a request for logs without query strings or response bodies."""
    label = safe_url(url)
    if not params:
        return label
    details: list[str] = []
    for key in ("cmtitle", "titles", "list"):
        value = params.get(key)
        if isinstance(value, str) and value:
            details.append(f"{key}={value}")
    if params.get("cmcontinue"):
        details.append("continued")
    if not details:
        return label
    return f"{label} {' '.join(details)}"


def _looks_like_html(response: requests.Response) -> bool:
    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().casefold()
    if content_type in {"text/html", "application/xhtml+xml"}:
        return True
    body = (response.text or "").lstrip()[:32].casefold()
    return body.startswith("<!doctype html") or body.startswith("<html")


def _unexpected_html_payload(source_kind: str, response: requests.Response) -> bool:
    return source_kind in {"wiki", "robots"} and _looks_like_html(response)


class HttpClient:
    """One session and limiter implementing all network safety rules."""

    def __init__(
        self,
        config: ExtractorConfig,
        *,
        session: requests.Session | Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = _default_jitter,
        logger: logging.Logger | None = None,
    ) -> None:
        # Dataclass construction performs all startup checks before a session can be used.
        self.config = config
        self.session = session if session is not None else requests.Session()
        self.limiter = AttemptLimiter(config.request_interval, clock=clock, sleep=sleep)
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._jitter = jitter
        self._logger = logger or logging.getLogger(__name__)
        self._request_lock = threading.Lock()
        self.retry_count = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if close is not None:
            close()

    def _read_response(self, response: requests.Response, url: str) -> None:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                announced_size = int(content_length)
            except ValueError:
                announced_size = -1
            if announced_size > self.config.max_response_bytes:
                raise ResponseTooLargeError(
                    url,
                    "response exceeds configured size",
                    self.config.max_response_bytes,
                )

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > self.config.max_response_bytes:
                raise ResponseTooLargeError(
                    url,
                    "response exceeds configured size",
                    self.config.max_response_bytes,
                )
        response._content = bytes(body)
        response._content_consumed = True

    def _retry_delay(self, attempt: int, retry_after: str | None, url: str) -> float:
        base = min(60.0, float(2 ** (attempt - 1)))
        try:
            jitter = float(self._jitter(base))
        except (TypeError, ValueError, OverflowError) as error:
            raise ConfigurationError("jitter must return a finite number") from error
        if not (0.0 <= jitter < float("inf")):
            raise ConfigurationError("jitter must return a finite non-negative number")
        backoff = min(60.0, base + jitter)
        parsed_retry_after = parse_retry_after(retry_after, now=self._wall_clock)
        if retry_after is not None and parsed_retry_after is None:
            self._logger.warning(
                "Ignoring invalid Retry-After from %s",
                safe_url(url),
            )
        return max(backoff, parsed_retry_after or 0.0)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        accepted_statuses: Collection[int] = (),
        allow_robots: bool = False,
        source_kind: str | None = None,
    ) -> requests.Response:
        """Perform a bounded GET while counting retries and redirects as attempts."""
        configured_kind = self.config.source_kind(url, allow_robots=allow_robots)
        if source_kind is not None and source_kind != configured_kind:
            raise ConfigurationError(
                f"request source kind {source_kind!r} does not match configured "
                f"kind {configured_kind!r}"
            )
        source_kind = configured_kind
        initial_url = url
        current_url = url
        current_params = params
        visited = {_without_fragment(url)}
        accepted = frozenset(accepted_statuses)

        with self._request_lock:
            for attempt in range(1, self.config.max_attempts + 1):
                self.limiter.wait()
                self._logger.info(
                    "http: %s GET %s attempt=%d/%d",
                    source_kind,
                    _request_label(current_url, current_params),
                    attempt,
                    self.config.max_attempts,
                )
                response: requests.Response | None = None
                try:
                    response = self.session.get(
                        current_url,
                        params=current_params,
                        headers={"User-Agent": self.config.user_agent},
                        timeout=self.config.timeout,
                        stream=True,
                        allow_redirects=False,
                    )
                    self._read_response(response, current_url)
                except (requests.Timeout, requests.ConnectionError):
                    if response is not None:
                        response.close()
                    self.limiter.record_completion()
                    if attempt == self.config.max_attempts:
                        raise NetworkError(
                            current_url,
                            "transient network failure",
                            attempt,
                        ) from None
                    delay = self._retry_delay(attempt, None, current_url)
                    self._logger.warning(
                        "Retrying %s after network failure attempt=%d delay=%.3f",
                        safe_url(current_url),
                        attempt,
                        delay,
                    )
                    self.retry_count += 1
                    self._sleep(delay)
                    continue
                except ResponseTooLargeError:
                    if response is not None:
                        response.close()
                    self.limiter.record_completion()
                    raise
                except requests.RequestException as error:
                    if response is not None:
                        response.close()
                    self.limiter.record_completion()
                    raise NetworkError(
                        current_url,
                        "non-retryable network failure",
                        attempt,
                    ) from error
                else:
                    response.close()
                    self.limiter.record_completion()

                status = response.status_code
                if status in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise RedirectError(current_url, "redirect has no Location header")
                    target = _without_fragment(urljoin(response.url or current_url, location))
                    if source_kind == "guide":
                        target = _without_query_or_fragment(target)
                    try:
                        self.config.validate_redirect(source_kind, initial_url, target)
                    except ConfigurationError as error:
                        raise RedirectPolicyError(
                            target,
                            "redirect leaves its approved source boundary",
                        ) from error
                    if target in visited:
                        raise RedirectError(target, "redirect loop detected")
                    if attempt == self.config.max_attempts:
                        raise RedirectError(target, "redirect exceeds the five-attempt budget")
                    visited.add(target)
                    current_url = target
                    current_params = None
                    continue

                if 200 <= status <= 299 or status in accepted:
                    if _unexpected_html_payload(source_kind, response):
                        if attempt == self.config.max_attempts:
                            raise RetryExhaustedError(
                                current_url,
                                "source returned an HTML interstitial",
                                attempt,
                                status,
                            )
                        delay = self._retry_delay(
                            attempt,
                            response.headers.get("Retry-After"),
                            current_url,
                        )
                        self._logger.warning(
                            "Retrying %s after HTML interstitial attempt=%d delay=%.3f",
                            safe_url(current_url),
                            attempt,
                            delay,
                        )
                        self.retry_count += 1
                        self._sleep(delay)
                        continue
                    return response

                if status == 429 or 500 <= status <= 599:
                    if attempt == self.config.max_attempts:
                        raise RetryExhaustedError(
                            current_url,
                            "retryable HTTP response persisted",
                            attempt,
                            status,
                        )
                    delay = self._retry_delay(
                        attempt,
                        response.headers.get("Retry-After"),
                        current_url,
                    )
                    self._logger.warning(
                        "Retrying %s status=%d attempt=%d delay=%.3f",
                        safe_url(current_url),
                        status,
                        attempt,
                        delay,
                    )
                    self.retry_count += 1
                    self._sleep(delay)
                    continue

                raise HttpStatusError(
                    current_url,
                    "non-retryable HTTP response",
                    status,
                )

        raise AssertionError("request attempt loop terminated unexpectedly")


RateLimitedHttpClient = HttpClient

_shared_lock = threading.Lock()
_shared_client: HttpClient | None = None


def get_shared_http_client(
    config: ExtractorConfig,
    **injections: Any,
) -> HttpClient:
    """Return the process-wide client used by all source modules."""
    global _shared_client
    with _shared_lock:
        if _shared_client is None:
            _shared_client = HttpClient(config, **injections)
        elif _shared_client.config != config:
            raise ConfigurationError("shared HTTP client already uses different configuration")
        return _shared_client
