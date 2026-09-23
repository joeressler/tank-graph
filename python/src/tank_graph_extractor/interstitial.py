"""Detect Wargaming JS cookie/anti-bot interstitials and validate wiki-host cookies.

requests never executes the challenge script. A 200 body that matches this
page is blocked, not MediaWiki, and must not be parsed or stored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .errors import ConfigurationError

DEFAULT_MAX_CONSECUTIVE_BLOCKS = 10
WIKI_PASS_COOKIE_NAMES = frozenset({"SPSI", "SPSE", "spcsrf", "UTGv2"})
_MARKETING_COOKIE_NAMES = frozenset({"optanonconsent", "optanonalertboxclosed"})


@dataclass(frozen=True, slots=True)
class FetchResult:
    """One bounded GET after size limits, before any source parser runs."""

    url: str
    status_code: int
    body: str
    blocked: bool
    content_type: str


def is_bot_interstitial(body: str | None) -> bool:
    """Return True when a 200 body is the wiki JS wait/block page."""

    if not body:
        return False
    head = body[:20000].lower()
    return is_hard_waf_block(body) or (
        "loading site please wait" in head
        or 'id="loading-content"' in head
        or "jscookiemsg" in head
        or "sbbhscc" in head
    )


def is_hard_waf_block(body: str | None) -> bool:
    """True when Imperva has banned the client; further requests deepen the block."""

    if not body:
        return False
    head = body[:20000].lower()
    return (
        "sorry, you have been blocked" in head
        or "incident reference id" in head
    )


def looks_like_mediawiki_html(body: str | None) -> bool:
    """Real article HTML includes the MediaWiki content root; the interstitial does not."""

    if not body:
        return False
    head = body[:20000].lower()
    return (
        'id="mw-content-text"' in head
        or "id='mw-content-text'" in head
        or "mw-parser-output" in head
    )


def looks_like_mediawiki_json(body: str | None) -> bool:
    """MediaWiki API success is JSON, never the wait-page HTML."""

    if not body:
        return False
    stripped = body.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def cookie_pairs(header: str) -> tuple[tuple[str, str], ...]:
    """Split a Cookie header into name/value pairs without logging values."""

    pairs: list[tuple[str, str]] = []
    for part in header.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            pairs.append((name, value.strip()))
    return tuple(pairs)


def _cookie_names(header: str) -> set[str]:
    return {name for name, _value in cookie_pairs(header)}


def is_marketing_cookie_name(name: str) -> bool:
    return name.casefold() in _MARKETING_COOKIE_NAMES


def strip_marketing_cookies(header: str) -> str:
    """Drop OneTrust/marketing pairs; keep wiki-host cookies."""

    kept = [
        f"{name}={value}"
        for name, value in cookie_pairs(header)
        if not is_marketing_cookie_name(name)
    ]
    return "; ".join(kept)


def validate_wiki_cookie(cookie: str, *, logger: logging.Logger | None = None) -> str:
    """Accept a full wiki-host Cookie header; reject marketing or truncated values."""

    stripped = cookie.strip()
    if not stripped:
        raise ConfigurationError("wiki cookie must not be blank")
    if "..." in stripped or "…" in stripped:
        raise ConfigurationError(
            "wiki cookie looks truncated; copy the full Cookie header from "
            "the wiki host document request, not a shortened DevTools preview"
        )

    names = _cookie_names(stripped)
    folded = {name.casefold() for name in names}
    pass_names = {item.casefold() for item in WIKI_PASS_COOKIE_NAMES}
    has_pass = any(name.casefold() in pass_names for name in names)
    has_marketing = bool(folded & _MARKETING_COOKIE_NAMES)
    log = logger or logging.getLogger(__name__)

    if has_marketing and not has_pass:
        raise ConfigurationError(
            "wiki cookie looks like worldoftanks.com marketing/consent cookies "
            "(OptanonConsent) rather than wiki-host pass cookies (SPSI/SPSE)"
        )
    if not has_pass:
        log.warning(
            "wiki cookie is missing SPSI/SPSE/spcsrf/UTGv2; capture it from "
            "wiki.wargaming.net or wiki.worldoftanks.com after the article loads"
        )
    return stripped
