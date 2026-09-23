"""Typed, safely formatted failures raised by the extractor."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


def safe_url(url: str) -> str:
    """Return only the host and path suitable for logs and diagnostics."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "<invalid-host>"
        port = parsed.port
    except (TypeError, ValueError):
        return "<invalid-host>/"
    if port is not None:
        host = f"{host}:{port}"
    return f"{host}{parsed.path or '/'}"


class TankGraphError(Exception):
    """Base class for expected extractor failures."""


class ConfigurationError(TankGraphError, ValueError):
    """Configuration is unsafe or inconsistent."""


@dataclass(eq=False)
class SourceError(TankGraphError):
    """A source-specific failure with redacted URL context."""

    url: str
    detail: str

    @property
    def source(self) -> str:
        return safe_url(self.url)

    def __str__(self) -> str:
        return f"{self.detail} [{self.source}]"


@dataclass(eq=False)
class NetworkError(SourceError):
    """A request could not complete after its allowed attempts."""

    attempts: int

    def __str__(self) -> str:
        return f"{self.detail} after {self.attempts} attempts [{self.source}]"


@dataclass(eq=False)
class RetryExhaustedError(NetworkError):
    """A transient HTTP response persisted through the final attempt."""

    status_code: int

    def __str__(self) -> str:
        return (
            f"{self.detail}: HTTP {self.status_code} after {self.attempts} attempts [{self.source}]"
        )


@dataclass(eq=False)
class HttpStatusError(SourceError):
    """A non-retryable HTTP status was returned."""

    status_code: int

    def __str__(self) -> str:
        return f"{self.detail}: HTTP {self.status_code} [{self.source}]"


class RedirectError(SourceError):
    """A redirect is malformed, loops, or exceeds the request budget."""


class RedirectPolicyError(RedirectError):
    """A redirect leaves its approved host or guide path."""


@dataclass(eq=False)
class ResponseTooLargeError(SourceError):
    """A streamed response exceeded the configured finite byte limit."""

    limit_bytes: int

    def __str__(self) -> str:
        return f"{self.detail}: limit {self.limit_bytes} bytes [{self.source}]"


class PolicyError(SourceError):
    """Source policy could not be safely satisfied."""


class PolicyFetchError(PolicyError):
    """A source's robots policy could not be determined."""


class PolicyDisallowedError(PolicyError):
    """A required source is disallowed by robots policy."""


@dataclass(eq=False)
class BotInterstitialError(SourceError):
    """HTTP 200 returned the JS cookie/anti-bot page, not MediaWiki."""

    consecutive: int = 1

    def __str__(self) -> str:
        return (
            f"{self.detail} after {self.consecutive} consecutive blocked response(s) "
            f"[{self.source}]"
        )


class ConsecutiveInterstitialError(BotInterstitialError):
    """Too many consecutive interstitial responses; the crawl cannot continue."""


SecurityInterstitialError = BotInterstitialError
