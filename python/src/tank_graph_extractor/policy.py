"""robots.txt enforcement for the required and optional source set."""

from __future__ import annotations

import logging
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from .config import ExtractorConfig
from .errors import PolicyDisallowedError, PolicyFetchError, TankGraphError, safe_url
from .http import HttpClient


@dataclass(frozen=True, slots=True)
class GuideSkip:
    """An optional guide excluded by source policy."""

    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class PolicyReport:
    """The guide set authorized for this run and explicit skips."""

    allowed_guides: tuple[str, ...]
    skipped_guides: tuple[GuideSkip, ...]


@dataclass(frozen=True, slots=True)
class _Rules:
    parser: urllib.robotparser.RobotFileParser | None
    allow_everything: bool = False
    deny_everything: bool = False

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.allow_everything:
            return True
        if self.deny_everything:
            return False
        if self.parser is None:
            return False
        return self.parser.can_fetch(user_agent, url)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port


def _robots_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))


class PolicyChecker:
    """Fetch and enforce source policies through the shared HTTP client."""

    def __init__(
        self,
        config: ExtractorConfig,
        client: HttpClient,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        if client.config != config:
            raise ValueError("policy checker and HTTP client must share configuration")
        self.config = config
        self.client = client
        self._logger = logger or logging.getLogger(__name__)
        self._cache: dict[tuple[str, str, int | None], _Rules | PolicyFetchError] = {}

    def _fetch_rules(self, source_url: str) -> _Rules:
        origin = _origin(source_url)
        cached = self._cache.get(origin)
        if isinstance(cached, PolicyFetchError):
            raise cached
        if cached is not None:
            return cached

        robots_url = _robots_url(source_url)
        try:
            response = self.client.get(
                robots_url,
                accepted_statuses={401, 403, 404},
                allow_robots=True,
            )
            if response.status_code == 404:
                rules = _Rules(None, allow_everything=True)
            elif response.status_code in {401, 403}:
                rules = _Rules(None, deny_everything=True)
            else:
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.text.splitlines())
                rules = _Rules(parser)
        except TankGraphError as error:
            failure = PolicyFetchError(
                robots_url,
                f"unable to determine robots policy ({error})",
            )
            self._cache[origin] = failure
            raise failure from error

        self._cache[origin] = rules
        return rules

    def is_allowed(self, url: str) -> bool:
        """Return whether robots policy permits this configured URL."""
        return self._fetch_rules(url).can_fetch(self.config.user_agent, url)

    def require_allowed(self, url: str, *, description: str) -> None:
        """Fail when a required source route is disallowed."""
        if not self.is_allowed(url):
            raise PolicyDisallowedError(url, f"robots policy disallows required {description}")

    def check_sources(self) -> PolicyReport:
        """Authorize required wiki access and retain every allowed optional guide."""
        wiki_query = urlencode(
            {
                "action": "query",
                "format": "json",
                "list": "categorymembers",
                "cmtitle": self.config.root_category,
            }
        )
        wiki_policy_url = f"{self.config.wiki_endpoint}?{wiki_query}"
        try:
            self.require_allowed(wiki_policy_url, description="MediaWiki root category")
        except PolicyFetchError:
            raise

        allowed: list[str] = []
        skipped: list[GuideSkip] = []
        for guide_url in self.config.guide_urls:
            try:
                if self.is_allowed(guide_url):
                    allowed.append(guide_url)
                    continue
                reason = "robots policy disallows optional guide"
            except PolicyFetchError:
                reason = "robots policy unavailable for optional guide"
            skipped.append(GuideSkip(guide_url, reason))
            self._logger.warning("%s: %s", reason, safe_url(guide_url))

        if not allowed:
            raise PolicyDisallowedError(
                self.config.guide_root,
                "no configured guide source is permitted by robots policy",
            )
        return PolicyReport(tuple(allowed), tuple(skipped))


RobotsPolicy = PolicyChecker
