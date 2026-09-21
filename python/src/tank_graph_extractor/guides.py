"""Parsing and policy enforcement for allowlisted official guide pages."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

from bs4 import BeautifulSoup, Tag

from .config import DEFAULT_GUIDE_PATHS, DEFAULT_GUIDE_ROOT, TANK_COACH_GUIDE_PATHS

GUIDE_ROOT_URL: Final = DEFAULT_GUIDE_ROOT
INITIAL_GUIDE_ALLOWLIST: Final = frozenset((*DEFAULT_GUIDE_PATHS, *TANK_COACH_GUIDE_PATHS))
TANK_COACH_PATH_PREFIX: Final = "/en/content/guide/tank-coach-video-guides/"
CANONICAL_CLASSES: Final = frozenset(
    {"Light Tanks", "Medium Tanks", "Heavy Tanks", "Tank Destroyers", "SPGs"}
)

# Ordered selectors make layout changes visible and independently testable.
CONTENT_ROOT_SELECTORS: Final = (
    "main",
    "article",
    ".article-content",
    ".article_content",
    ".content-guide",
    ".guide-content",
)
BOILERPLATE_SELECTORS: Final = (
    "nav",
    "footer",
    "header",
    "script",
    "style",
    "form",
    "dialog",
    ".cookie",
    ".cookie-consent",
    ".share",
    ".sharing",
    ".breadcrumbs",
    ".breadcrumb",
    ".recommendations",
    ".related-content",
    "[class*='cookie' i]",
    "[class*='share' i]",
    "[class*='breadcrumb' i]",
    "[class*='recommend' i]",
    "[class*='related' i]",
    "[aria-label*='cookie' i]",
    "[aria-label*='share' i]",
)
MIN_RELEVANT_CONTENT_CHARS: Final = 80

_TRACKING_QUERY_KEYS: Final = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "source",
        "yclid",
    }
)
_TRACKING_QUERY_PREFIXES: Final = ("utm_",)
_ACCESS_DOCUMENT_RE: Final = re.compile(
    r"\b(access denied|account login|log in to continue|sign in to continue|"
    r"verify (?:that )?you are human|captcha|security challenge|"
    r"request blocked|error\s+(?:403|404|500|502|503)|page not found)\b",
    re.IGNORECASE,
)
_RELEVANT_HEADING_RE: Final = re.compile(
    r"\b(vehicle types?|pre-battle setup|battlefield|roles?|surviv(?:al|e)|aim(?:ing)?|"
    r"equipment|consumables?|crew|tactics?|tank coach|"
    r"view range|vision|"
    r"light tanks?|medium tanks?|heavy tanks?|tank destroyers?|"
    r"spgs?|artillery|self-propelled guns?)\b",
    re.IGNORECASE,
)
_UNIVERSAL_HEADING_RE: Final = re.compile(
    r"\b(pre-battle setup|battlefield|surviv(?:al|e)|aim(?:ing)?|equipment|consumables?|"
    r"crew(?: capabilities)?|tactics?|tank coach)\b",
    re.IGNORECASE,
)
_TACTICAL_TEXT_RE: Final = re.compile(
    r"\b(aim|armor|battle|camouflage|conceal|cover|crew|damage|"
    r"equipment|flank|gun|hull|map|mobility|position|reload|repair|"
    r"retreat|scout|shell|shoot|side.?scrap|spot|surviv|tactic|"
    r"target|terrain|track|view range|vision)\w*\b",
    re.IGNORECASE,
)
_CONCEPT_PATTERNS: Final = {
    "equipment": re.compile(r"\bequipment\b", re.IGNORECASE),
    "view range": re.compile(r"\b(?:view|vision)[ -]range\b", re.IGNORECASE),
    "hull-down": re.compile(r"\bhull[ -]down\b", re.IGNORECASE),
    "sidescraping": re.compile(r"\bside[ -]scrap\w*\b", re.IGNORECASE),
}
_CLASS_PATTERNS: Final = (
    ("Tank Destroyers", re.compile(r"\btank destroyers?\b", re.IGNORECASE)),
    (
        "SPGs",
        re.compile(r"\b(?:spgs?|artillery|self-propelled guns?)\b", re.IGNORECASE),
    ),
    ("Light Tanks", re.compile(r"\blight tanks?\b", re.IGNORECASE)),
    ("Medium Tanks", re.compile(r"\bmedium tanks?\b", re.IGNORECASE)),
    ("Heavy Tanks", re.compile(r"\bheavy tanks?\b", re.IGNORECASE)),
)


class GuideParseError(ValueError):
    """A guide response failed URL, access, or layout validation."""


@dataclass(frozen=True, slots=True)
class GuideSegment:
    """One retained guide block with defensible class scope."""

    text: str
    applicable_classes: frozenset[str]
    tactical_concepts: frozenset[str] = frozenset()
    source_url: str = ""
    source_kind: str = "guide"


@dataclass(frozen=True, slots=True)
class GuideDiagnostic:
    """Relevant content retained for review but not safe to assign."""

    text: str
    reason: str
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class GuideParseResult:
    """Validated guide content plus non-assignable diagnostics."""

    title: str
    segments: tuple[GuideSegment, ...]
    diagnostics: tuple[GuideDiagnostic, ...]
    discovered_children: tuple[str, ...] = ()


def _canonical_path(path: str) -> str:
    decoded = path or "/"
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    decoded = decoded.replace("\\", "/")
    if "\x00" in decoded:
        raise GuideParseError("guide URL path contains a null byte")
    had_trailing_slash = decoded.endswith("/")
    normalized = posixpath.normpath("/" + decoded.lstrip("/"))
    if had_trailing_slash and normalized != "/":
        normalized += "/"
    return quote(normalized, safe="/:@-._~")


def normalize_guide_url(url: str, *, base_url: str = GUIDE_ROOT_URL) -> str:
    """Return a stable URL identity without fragments or tracking parameters."""

    absolute = urljoin(base_url, url)
    parts = urlsplit(absolute)
    if parts.scheme.casefold() not in {"http", "https"}:
        raise GuideParseError(f"unsupported guide URL scheme: {parts.scheme!r}")
    if not parts.hostname or parts.username or parts.password:
        raise GuideParseError("guide URL must contain a plain host")

    host = parts.hostname.casefold()
    try:
        port = parts.port
    except ValueError as error:
        raise GuideParseError("guide URL contains an invalid port") from error
    default_port = (parts.scheme.casefold() == "https" and port == 443) or (
        parts.scheme.casefold() == "http" and port == 80
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_QUERY_KEYS
        and not key.casefold().startswith(_TRACKING_QUERY_PREFIXES)
    ]
    query_items.sort(key=lambda item: (item[0].casefold(), item[0], item[1]))
    return urlunsplit(
        (
            parts.scheme.casefold(),
            netloc,
            _canonical_path(parts.path),
            urlencode(query_items, doseq=True),
            "",
        )
    )


def validate_guide_url(
    url: str,
    *,
    guide_root: str = GUIDE_ROOT_URL,
    require_allowlisted: bool = True,
    allowlist: Iterable[str] = INITIAL_GUIDE_ALLOWLIST,
) -> str:
    """Validate exact host/path boundaries and optionally the route allowlist."""

    root = normalize_guide_url(guide_root)
    normalized = normalize_guide_url(url, base_url=root)
    root_parts = urlsplit(root)
    parts = urlsplit(normalized)
    if (parts.scheme, parts.netloc) != (root_parts.scheme, root_parts.netloc):
        raise GuideParseError("guide URL resolves outside the approved host")
    root_path = root_parts.path.rstrip("/") + "/"
    if not parts.path.startswith(root_path):
        raise GuideParseError("guide URL resolves outside the approved path")

    if require_allowlisted:
        allowed_urls = {normalize_guide_url(route, base_url=root) for route in allowlist}
        request_identity = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        if request_identity not in allowed_urls:
            raise GuideParseError("guide URL is not in the configured allowlist")
    return normalized


def discover_allowlisted_children(
    html: str,
    page_url: str,
    *,
    guide_root: str = GUIDE_ROOT_URL,
    allowlist: Iterable[str] = INITIAL_GUIDE_ALLOWLIST,
) -> tuple[str, ...]:
    """Intersect discovered Tank Coach children with prior authorization."""

    page_url = validate_guide_url(page_url, guide_root=guide_root, allowlist=allowlist)
    allowed_urls = {
        validate_guide_url(
            route,
            guide_root=guide_root,
            allowlist=allowlist,
        )
        for route in allowlist
    }
    coach_prefix = urlsplit(
        normalize_guide_url("tank-coach-video-guides/", base_url=guide_root)
    ).path
    root_parts = urlsplit(normalize_guide_url(guide_root))
    locale_neutral_prefix = "/content/guide/"
    discovered: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        try:
            candidate = normalize_guide_url(anchor["href"], base_url=page_url)
            candidate_parts = urlsplit(candidate)
            if (
                (candidate_parts.scheme, candidate_parts.netloc)
                == (root_parts.scheme, root_parts.netloc)
                and candidate_parts.path.startswith(locale_neutral_prefix)
                and not candidate_parts.path.startswith(root_parts.path)
            ):
                relative = candidate_parts.path.removeprefix(locale_neutral_prefix)
                candidate = normalize_guide_url(relative, base_url=guide_root)
            validated = validate_guide_url(
                candidate,
                guide_root=guide_root,
                require_allowlisted=False,
            )
        except (GuideParseError, ValueError):
            continue
        if (
            urlsplit(validated).path.startswith(coach_prefix)
            and validated in allowed_urls
            and validated != page_url
        ):
            discovered.add(validated)
    return tuple(sorted(discovered, key=lambda value: (value.casefold(), value)))


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _classes_from_heading(heading: str) -> frozenset[str]:
    return frozenset(canonical for canonical, pattern in _CLASS_PATTERNS if pattern.search(heading))


def _scope_from_headings(headings: list[str]) -> frozenset[str]:
    for heading in reversed(headings):
        explicit = _classes_from_heading(heading)
        if explicit:
            return explicit
    for heading in reversed(headings):
        if _UNIVERSAL_HEADING_RE.search(heading):
            return CANONICAL_CLASSES
    return frozenset()


def _concepts(text: str) -> frozenset[str]:
    return frozenset(
        concept for concept, pattern in _CONCEPT_PATTERNS.items() if pattern.search(text)
    )


def _media_text(tag: Tag) -> str:
    if tag.name == "img":
        return _clean_text(tag.get("alt", ""))
    return _clean_text(tag.get_text(" ", strip=True))


def _is_qualifying_text(text: str, headings: list[str]) -> bool:
    return bool(
        text
        and any(_RELEVANT_HEADING_RE.search(heading) for heading in headings)
        and _TACTICAL_TEXT_RE.search(text)
    )


def parse_guide_html(
    html: str,
    source_url: str,
    *,
    guide_root: str = GUIDE_ROOT_URL,
    allowlist: Iterable[str] = INITIAL_GUIDE_ALLOWLIST,
    minimum_content_chars: int = MIN_RELEVANT_CONTENT_CHARS,
) -> GuideParseResult:
    """Parse one approved HTML guide without falling back to body text."""

    source_url = validate_guide_url(source_url, guide_root=guide_root, allowlist=allowlist)
    soup = BeautifulSoup(html, "html.parser")
    root = next(
        (
            selected
            for selector in CONTENT_ROOT_SELECTORS
            if (selected := soup.select_one(selector)) is not None
        ),
        None,
    )
    title_tag = (root.find("h1") if root is not None else None) or soup.find("title")
    title = _clean_text(title_tag.get_text(" ", strip=True)) if title_tag else ""
    access_text = (
        _clean_text(root.get_text(" ", strip=True))
        if root is not None
        else _clean_text(soup.get_text(" ", strip=True))
    )
    if _ACCESS_DOCUMENT_RE.search(title) or _ACCESS_DOCUMENT_RE.search(access_text):
        raise GuideParseError(f"guide response is an access or error document: {source_url}")
    if not title:
        raise GuideParseError(f"guide response is missing its expected title: {source_url}")
    if root is None:
        raise GuideParseError(f"guide response is missing its content root: {source_url}")

    for selector in BOILERPLATE_SELECTORS:
        for unwanted in root.select(selector):
            unwanted.decompose()

    headings: list[str] = []
    segments: list[GuideSegment] = []
    diagnostics: list[GuideDiagnostic] = []
    has_relevant_heading = False
    qualifying_chars = 0

    for element in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "img", "figcaption"]):
        if element.name in {"h1", "h2", "h3", "h4"}:
            level = int(element.name[1])
            heading = _clean_text(element.get_text(" ", strip=True))
            headings = headings[: level - 1]
            headings.append(heading)
            has_relevant_heading = has_relevant_heading or bool(
                _RELEVANT_HEADING_RE.search(heading)
            )
            continue

        text = _media_text(element)
        if not _is_qualifying_text(text, headings):
            continue
        qualifying_chars += len(text)
        scope = _scope_from_headings(headings)
        concepts = _concepts(" ".join((*headings, text)))
        if not scope:
            diagnostics.append(
                GuideDiagnostic(
                    text=text,
                    reason="relevant content has no explicit canonical class scope",
                    source_url=source_url,
                )
            )
            continue
        source_kind = "class_guide" if scope != CANONICAL_CLASSES else "universal_guide"
        segments.append(
            GuideSegment(
                text=text,
                applicable_classes=scope,
                tactical_concepts=concepts,
                source_url=source_url,
                source_kind=source_kind,
            )
        )

    coach_index_path = urlsplit(
        normalize_guide_url("tank-coach-video-guides/", base_url=guide_root)
    ).path
    is_coach_index = urlsplit(source_url).path.rstrip("/") == coach_index_path.rstrip("/")
    discovered = (
        discover_allowlisted_children(
            html,
            source_url,
            guide_root=guide_root,
            allowlist=allowlist,
        )
        if is_coach_index
        else ()
    )

    if (not has_relevant_heading or qualifying_chars == 0) and not (is_coach_index and discovered):
        raise GuideParseError(
            f"guide content root has no relevant heading and qualifying text: {source_url}"
        )
    if qualifying_chars and qualifying_chars < minimum_content_chars:
        raise GuideParseError(
            f"guide tactical content is implausibly short "
            f"({qualifying_chars} < {minimum_content_chars} characters): {source_url}"
        )
    return GuideParseResult(
        title=title,
        segments=tuple(segments),
        diagnostics=tuple(diagnostics),
        discovered_children=discovered,
    )


parse_guide = parse_guide_html
