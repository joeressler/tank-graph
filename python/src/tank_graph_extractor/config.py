"""Validated configuration for respectful source collection."""

from __future__ import annotations

import math
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from .errors import ConfigurationError

DEFAULT_WIKI_ENDPOINT = "https://wiki.wargaming.net/api.php"
DEFAULT_GUIDE_ROOT = "https://worldoftanks.com/en/content/guide/"
DEFAULT_USER_AGENT = "WoTGraphBot/0.1.0 (contact: joe.a.ressler+tankgraph@gmail.com)"
DEFAULT_GUIDE_PATHS = ("newcomers-guide/getting_started/",)
TANK_COACH_GUIDE_PATHS = (
    "tank-coach-video-guides/",
    "tank-coach-video-guides/tank-coach-research/",
)
PRODUCTION_REQUEST_INTERVAL = 5.0

_USER_AGENT_RE = re.compile(
    r"\AWoTGraphBot/(?P<version>[0-9A-Za-z][0-9A-Za-z._-]*) "
    r"\(contact: (?P<contact>[^()\s]+)\)\Z"
)
_EMAIL_RE = re.compile(
    r"\A[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z"
)
_PLACEHOLDER_MARKERS = frozenset({"placeholder", "changeme", "your-email", "your_contact", "todo"})
_RESERVED_CONTACT_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "invalid", "localhost", "test"}
)


def _is_reserved_contact_host(host: str) -> bool:
    folded = host.casefold().rstrip(".")
    return any(
        folded == reserved or folded.endswith(f".{reserved}")
        for reserved in _RESERVED_CONTACT_DOMAINS
    )


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _authority(url: str) -> tuple[str, int | None]:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").encode("idna").decode("ascii").casefold()
    port = parsed.port
    if (parsed.scheme.casefold(), port) in {("https", 443), ("http", 80)}:
        port = None
    return host, port


def normalized_url_path(url: str) -> str:
    """Canonicalize a URL path before applying an allowlist boundary."""
    path = urlsplit(url).path or "/"
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if "\\" in path or "\x00" in path:
        raise ConfigurationError("URL paths must not contain backslashes or NUL bytes")
    had_trailing_slash = path.endswith("/")
    path = posixpath.normpath("/" + path.lstrip("/"))
    if had_trailing_slash and path != "/":
        path += "/"
    return path


def _validate_endpoint(name: str, url: str, *, test_mode: bool) -> None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise ConfigurationError(f"{name} is not a valid URL") from error
    schemes = {"http", "https"} if test_mode else {"https"}
    if parsed.scheme.casefold() not in schemes:
        allowed = "http or https" if test_mode else "https"
        raise ConfigurationError(f"{name} must use {allowed}")
    if not parsed.hostname:
        raise ConfigurationError(f"{name} must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{name} must not contain credentials")
    if parsed.fragment or parsed.query:
        raise ConfigurationError(f"{name} must not contain a query or fragment")
    normalized_url_path(url)


def _validate_request_target(name: str, url: str, *, test_mode: bool) -> None:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise ConfigurationError(f"{name} is not a valid URL") from error
    schemes = {"http", "https"} if test_mode else {"https"}
    if parsed.scheme.casefold() not in schemes or not parsed.hostname:
        raise ConfigurationError(f"{name} has an unapproved scheme or missing host")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(f"{name} must not contain credentials")
    normalized_url_path(url)


def validate_user_agent(user_agent: str) -> str:
    """Validate and return the configured descriptive User-Agent."""
    match = _USER_AGENT_RE.fullmatch(user_agent)
    if match is None:
        raise ConfigurationError(
            "User-Agent must match 'WoTGraphBot/<version> (contact: <contact>)'"
        )
    contact = match.group("contact")
    folded_contact = contact.casefold()
    if any(marker in folded_contact for marker in _PLACEHOLDER_MARKERS):
        raise ConfigurationError("User-Agent contact must not be a placeholder")
    if _EMAIL_RE.fullmatch(contact):
        if _is_reserved_contact_host(contact.rsplit("@", 1)[1]):
            raise ConfigurationError("User-Agent contact must not use a reserved domain")
        return user_agent

    parsed = urlsplit(contact)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or _is_reserved_contact_host(parsed.hostname)
        or parsed.hostname.casefold() in {"127.0.0.1", "::1"}
    ):
        raise ConfigurationError(
            "User-Agent contact must be a real email address or maintained HTTPS URL"
        )
    return user_agent


@dataclass(frozen=True, slots=True)
class ExtractorConfig:
    """Complete startup configuration, validated before client construction."""

    wiki_endpoint: str = DEFAULT_WIKI_ENDPOINT
    guide_root: str = DEFAULT_GUIDE_ROOT
    root_category: str = "Category:Tanks"
    user_agent: str = DEFAULT_USER_AGENT
    request_interval: float = PRODUCTION_REQUEST_INTERVAL
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_attempts: int = 5
    max_response_bytes: int = 10 * 1024 * 1024
    output_path: Path = Path("data/tanks_data.json")
    guide_paths: tuple[str, ...] = DEFAULT_GUIDE_PATHS
    test_mode: bool = False

    def __post_init__(self) -> None:
        _validate_endpoint("MediaWiki endpoint", self.wiki_endpoint, test_mode=self.test_mode)
        _validate_endpoint("guide root", self.guide_root, test_mode=self.test_mode)
        validate_user_agent(self.user_agent)

        if not self.root_category.strip():
            raise ConfigurationError("root category must not be blank")
        minimum_interval = 0.0 if self.test_mode else PRODUCTION_REQUEST_INTERVAL
        if not _is_finite_number(self.request_interval) or self.request_interval < minimum_interval:
            raise ConfigurationError(
                f"request interval must be finite and at least {minimum_interval:.1f} seconds"
            )
        for name, value in (
            ("connect timeout", self.connect_timeout),
            ("read timeout", self.read_timeout),
        ):
            if not _is_finite_number(value) or value <= 0:
                raise ConfigurationError(f"{name} must be finite and greater than zero")
        if self.max_attempts != 5:
            raise ConfigurationError("maximum attempts is fixed at five")
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or self.max_response_bytes <= 0
        ):
            raise ConfigurationError("maximum response bytes must be a positive integer")
        if not isinstance(self.output_path, Path):
            object.__setattr__(self, "output_path", Path(self.output_path))

        root_path = normalized_url_path(self.guide_root)
        if not root_path.endswith("/"):
            raise ConfigurationError("guide root path must end with '/'")
        normalized_guides: list[str] = []
        for guide_path in self.guide_paths:
            if not guide_path or guide_path.startswith(("/", "\\")):
                raise ConfigurationError("guide paths must be non-empty relative paths")
            decoded_path = guide_path
            for _ in range(3):
                decoded = unquote(decoded_path)
                if decoded == decoded_path:
                    break
                decoded_path = decoded
            if "\\" in decoded_path or ".." in decoded_path.split("/"):
                raise ConfigurationError("guide paths must not contain traversal segments")
            guide_url = urljoin(self.guide_root, guide_path)
            _validate_endpoint("guide URL", guide_url, test_mode=self.test_mode)
            if _authority(guide_url) != _authority(self.guide_root):
                raise ConfigurationError("guide paths must remain on the configured guide host")
            guide_url_path = normalized_url_path(guide_url)
            if not guide_url_path.startswith(root_path) or guide_url_path == root_path:
                raise ConfigurationError("guide paths must remain beneath the guide root")
            normalized_guides.append(guide_url)
        if len(set(normalized_guides)) != len(normalized_guides):
            raise ConfigurationError("guide paths must be unique")

    @property
    def timeout(self) -> tuple[float, float]:
        return self.connect_timeout, self.read_timeout

    @property
    def guide_urls(self) -> tuple[str, ...]:
        return tuple(urljoin(self.guide_root, path) for path in self.guide_paths)

    @property
    def contact(self) -> str:
        match = _USER_AGENT_RE.fullmatch(self.user_agent)
        if match is None:  # Defensive; construction has already validated this.
            raise ConfigurationError("invalid User-Agent")
        return match.group("contact")

    def source_kind(self, url: str, *, allow_robots: bool = False) -> str:
        """Classify an initial request URL or reject it before network access."""
        _validate_endpoint("request URL", url, test_mode=self.test_mode)
        path = normalized_url_path(url)
        authority = _authority(url)
        if (
            allow_robots
            and path == "/robots.txt"
            and authority
            in {
                _authority(self.wiki_endpoint),
                _authority(self.guide_root),
            }
        ):
            return "robots"
        if authority == _authority(self.wiki_endpoint) and path == normalized_url_path(
            self.wiki_endpoint
        ):
            return "wiki"
        if (
            authority == _authority(self.guide_root)
            and url.split("#", 1)[0].split("?", 1)[0] in self.guide_urls
        ):
            return "guide"
        raise ConfigurationError("request URL is outside configured source endpoints")

    def validate_redirect(self, original_kind: str, original_url: str, target_url: str) -> None:
        """Enforce host boundaries and the guide-root path on every redirect."""
        _validate_request_target("redirect target", target_url, test_mode=self.test_mode)
        if _authority(target_url) != _authority(original_url):
            raise ConfigurationError("redirect target leaves the configured host")
        if original_kind == "guide":
            root_path = normalized_url_path(self.guide_root)
            target_path = normalized_url_path(target_url)
            if not target_path.startswith(root_path) or target_path == root_path:
                raise ConfigurationError("redirect target leaves the configured guide path")


Config = ExtractorConfig
