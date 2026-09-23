"""Shared rate-limited HTTP transport for every extractor source."""

from __future__ import annotations

import contextlib
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
    BotInterstitialError,
    ConfigurationError,
    ConsecutiveInterstitialError,
    HttpStatusError,
    NetworkError,
    RedirectError,
    RedirectPolicyError,
    ResponseTooLargeError,
    RetryExhaustedError,
    safe_url,
)
from .interstitial import (
    FetchResult,
    cookie_pairs,
    is_bot_interstitial,
    is_hard_waf_block,
    looks_like_mediawiki_json,
    strip_marketing_cookies,
)

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _RetryAfterCookieRefresh(Exception):
    """Redo the same wiki GET once after a required Playwright cookie export."""


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
        cookie_refresher: Callable[[], str] | None = None,
    ) -> None:
        # Dataclass construction performs all startup checks before a session can be used.
        self.config = config
        self._owns_session = session is None
        self.session = session if session is not None else requests.Session()
        self.limiter = AttemptLimiter(config.request_interval, clock=clock, sleep=sleep)
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._jitter = jitter
        self._logger = logger or logging.getLogger(__name__)
        self._request_lock = threading.Lock()
        self.retry_count = 0
        self.consecutive_blocks = 0
        self._cookie_refresher = cookie_refresher
        self._cookie_broker: Any | None = None
        self._wiki_cookie = config.wiki_cookie
        self._session_wiki_cookie_names: set[str] = set()
        if self._wiki_cookie:
            self._apply_wiki_cookie(self._wiki_cookie)

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
        broker = self._cookie_broker
        self._cookie_broker = None
        if broker is not None:
            closer = getattr(broker, "close", None)
            if closer is not None:
                closer()
        close = getattr(self.session, "close", None)
        if close is not None:
            close()

    def _recreate_session(self) -> None:
        if not self._owns_session:
            return
        close = getattr(self.session, "close", None)
        if close is not None:
            with contextlib.suppress(OSError, requests.RequestException):
                close()
        self.session = requests.Session()
        if self._wiki_cookie:
            self._apply_wiki_cookie(self._wiki_cookie)

    def pause_between_phases(self, reason: str) -> None:
        """Insert one extra limiter slot after a discovery burst."""
        self._logger.info("%s", reason)
        if self.config.request_interval <= 0:
            return
        self.limiter.wait()
        self.limiter.record_completion()

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

    def _request_headers(self, url: str) -> dict[str, str]:
        headers = {
            "User-Agent": self.config.user_agent,
            "Connection": "close",
        }
        if self._wiki_cookie and self.config.is_wiki_host(url):
            headers["Cookie"] = self._wiki_cookie
        return headers

    def _cookie_refresh_enabled(self) -> bool:
        if self.config.cookie_refresh_every <= 0:
            return False
        if self._cookie_refresher is not None:
            return True
        return not self.config.test_mode

    def _needs_wiki_cookies(self, source_kind: str, url: str) -> bool:
        """Wiki API and wiki-host robots.txt share the same Imperva cookie gate."""
        return source_kind == "wiki" or (
            source_kind == "robots" and self.config.is_wiki_host(url)
        )

    def _apply_wiki_cookie(self, header: str) -> None:
        cleaned = strip_marketing_cookies(header)
        self._wiki_cookie = cleaned or None
        jar = getattr(self.session, "cookies", None)
        setter = getattr(jar, "set", None)
        clearer = getattr(jar, "clear", None)
        domain = urlsplit(self.config.wiki_endpoint).hostname
        if setter is None or domain is None:
            return
        for name in list(self._session_wiki_cookie_names):
            if clearer is not None:
                with contextlib.suppress(KeyError, ValueError, TypeError):
                    clearer(domain, "/", name)
        self._session_wiki_cookie_names.clear()
        if not cleaned:
            return
        for name, value in cookie_pairs(cleaned):
            setter(name, value, domain=domain, path="/")
            self._session_wiki_cookie_names.add(name)

    def _playwright_refresh(self, *, force_navigate: bool = False) -> str:
        if self._cookie_broker is None:
            from .browser import PlaywrightCookieBroker

            self._cookie_broker = PlaywrightCookieBroker(self.config)
        return self._cookie_broker.refresh(
            existing_cookie=self._wiki_cookie,
            force_navigate=force_navigate,
        )

    def _refresh_wiki_cookies(self, *, force_navigate: bool = False) -> None:
        self._logger.info("refreshing wiki-host cookies via Playwright")
        self.limiter.wait()
        try:
            try:
                if self._cookie_refresher is not None:
                    header = self._cookie_refresher()
                else:
                    header = self._playwright_refresh(force_navigate=force_navigate)
            except (BotInterstitialError, ConfigurationError) as error:
                raise ConsecutiveInterstitialError(
                    getattr(error, "url", self.config.wiki_endpoint),
                    "aborting because wiki cookie refresh failed",
                    max(self.consecutive_blocks, 1),
                ) from error
            self._apply_wiki_cookie(header)
        finally:
            self.limiter.record_completion()

    def _response_text(self, response: requests.Response) -> str:
        try:
            text = response.text
        except (LookupError, UnicodeError, AttributeError):
            text = (response.content or b"").decode("utf-8", "replace")
        return text if isinstance(text, str) else ""

    def _inspect(self, response: requests.Response, url: str) -> FetchResult:
        body = self._response_text(response)
        return FetchResult(
            url=url,
            status_code=response.status_code,
            body=body,
            blocked=is_bot_interstitial(body),
            content_type=str(response.headers.get("Content-Type", "")),
        )

    def _raise_blocked(self, result: FetchResult) -> None:
        self.consecutive_blocks += 1
        self._logger.warning(
            "Blocked wiki/JS interstitial at %s consecutive=%d; "
            "not parsing body and not retrying this URL over HTTP. "
            "Capture WIKI_COOKIE from the wiki host after #mw-content-text loads.",
            safe_url(result.url),
            self.consecutive_blocks,
        )
        if self.consecutive_blocks >= self.config.max_consecutive_blocks:
            raise ConsecutiveInterstitialError(
                result.url,
                "aborting after consecutive JS cookie/anti-bot interstitials",
                self.consecutive_blocks,
            )
        raise BotInterstitialError(
            result.url,
            "HTTP 200 is a JS cookie/anti-bot interstitial, not MediaWiki",
            self.consecutive_blocks,
        )

    def _accept_success(
        self,
        response: requests.Response,
        url: str,
        source_kind: str,
        *,
        allow_cookie_retry: bool,
    ) -> requests.Response:
        result = self._inspect(response, url)
        if result.blocked:
            if is_hard_waf_block(result.body):
                self.consecutive_blocks += 1
                raise ConsecutiveInterstitialError(
                    result.url,
                    "aborting because the wiki WAF blocked this client; "
                    "do not retry until a normal browser can load the article",
                    self.consecutive_blocks,
                )
            if (
                self._needs_wiki_cookies(source_kind, url)
                and allow_cookie_retry
                and self._cookie_refresh_enabled()
            ):
                self.consecutive_blocks += 1
                self._logger.warning(
                    "Blocked wiki/JS interstitial at %s consecutive=%d; "
                    "refreshing wiki-host cookies and retrying this URL once",
                    safe_url(result.url),
                    self.consecutive_blocks,
                )
                if self.consecutive_blocks >= self.config.max_consecutive_blocks:
                    raise ConsecutiveInterstitialError(
                        result.url,
                        "aborting after consecutive JS cookie/anti-bot interstitials",
                        self.consecutive_blocks,
                    )
                self._refresh_wiki_cookies(force_navigate=True)
                raise _RetryAfterCookieRefresh
            self._raise_blocked(result)
        self.consecutive_blocks = 0
        if source_kind == "wiki" and not looks_like_mediawiki_json(result.body):
            raise HttpStatusError(url, "unexpected HTML from MediaWiki API", 200)
        return response

    def _retry_delay(self, attempt: int, retry_after: str | None, url: str) -> float:
        parsed_retry_after = parse_retry_after(retry_after, now=self._wall_clock)
        if retry_after is not None and parsed_retry_after is None:
            self._logger.warning(
                "Ignoring invalid Retry-After from %s",
                safe_url(url),
            )
        if parsed_retry_after is not None:
            return parsed_retry_after
        base = min(15.0, max(5.0, float(2 ** (attempt - 1))))
        try:
            jitter = float(self._jitter(base))
        except (TypeError, ValueError, OverflowError) as error:
            raise ConfigurationError("jitter must return a finite number") from error
        if not (0.0 <= jitter < float("inf")):
            raise ConfigurationError("jitter must return a finite non-negative number")
        return min(15.0, base + jitter)

    def _log_get(
        self,
        source_kind: str,
        url: str,
        params: Mapping[str, Any] | None,
    ) -> None:
        if source_kind not in {"wiki", "robots"}:
            return
        title = None
        if params:
            raw_title = params.get("titles") or params.get("cmtitle")
            if isinstance(raw_title, str) and raw_title:
                title = raw_title
        extra = f" titles={title}" if title else ""
        self._logger.info("http: %s GET %s%s", source_kind, safe_url(url), extra)

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
        cookie_retry_allowed = True

        with self._request_lock:
            for attempt in range(1, self.config.max_attempts + 1):
                self.limiter.wait()
                response: requests.Response | None = None
                try:
                    self._log_get(source_kind, current_url, current_params)
                    response = self.session.get(
                        current_url,
                        params=current_params,
                        headers=self._request_headers(current_url),
                        timeout=self.config.timeout,
                        stream=True,
                        allow_redirects=False,
                    )
                    self._read_response(response, current_url)
                except (requests.Timeout, requests.ConnectionError):
                    if response is not None:
                        response.close()
                    self.limiter.record_completion()
                    self._recreate_session()
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
                        if 200 <= status <= 299:
                            try:
                                return self._accept_success(
                                    response,
                                    current_url,
                                    source_kind,
                                    allow_cookie_retry=cookie_retry_allowed,
                                )
                            except _RetryAfterCookieRefresh:
                                cookie_retry_allowed = False
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
