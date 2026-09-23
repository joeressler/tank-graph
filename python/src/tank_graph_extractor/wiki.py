"""MediaWiki revision retrieval and strict vehicle-page parsing."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import mwparserfromhell
from mwparserfromhell.nodes import (
    Argument,
    Comment,
    ExternalLink,
    Heading,
    HTMLEntity,
    Tag,
    Template,
    Text,
    Wikilink,
)
from mwparserfromhell.parser import ParserError
from mwparserfromhell.wikicode import Wikicode

from .errors import ConfigurationError
from .models import (
    CandidateStatus,
    DiagnosticSeverity,
    ExtractionDiagnostic,
    PageDescriptor,
    TacticalSection,
    Taxonomy,
    VehicleMetadata,
    WikiExtractionError,
    WikiHttpClient,
    WikiParseResult,
    WikiRevision,
    WikiVehicle,
    identity_key,
    normalize_text,
)
from .taxonomy import (
    canonicalize_primary_class,
    category_leaf,
    taxonomy_primary_classes,
    taxonomy_subclasses,
)

INFOBOX_ALIASES = frozenset({"vehicle", "tankdata", "tank data"})
PARAMETER_ALIASES: Mapping[str, tuple[str, ...]] = {
    "identifier": ("id", "internal name", "tank id", "tank"),
    "display_name": ("display name", "title", "name"),
    "primary_class": ("class", "type"),
    "nation": ("nation", "country"),
    "tier": ("tier", "level"),
}
TACTICAL_SECTION_TITLES = frozenset({"performance", "tactics"})
INFOBOX_TACTICAL_ALIASES: Mapping[str, tuple[str, ...]] = {
    "Performance": ("inthegame performance", "in the game performance"),
    "Tactics": ("inthegame tactics", "in the game tactics"),
}
_VALID_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_INCOMPLETE_METADATA_CODES = frozenset({"missing_primary_class", "missing_nation", "missing_tier"})
_MAINTENANCE_CATEGORY = "tank articles requiring maintenance"
_TABLE_RE = re.compile(r"\{\|.*?\|\}", re.DOTALL)
_CITATION_RE = re.compile(r"\[(?:\s*\d+\s*|citation needed)\]", re.IGNORECASE)
_LIST_PREFIX_RE = re.compile(r"^\s*[*#;:]+\s*")
_SPACE_RE = re.compile(r"[^\S\r\n]+")
_EMPTY_LINES_RE = re.compile(r"\n{3,}")
_TIER_CATEGORY_RE = re.compile(r"\Atier (?P<token>[ivxlcdm]+|\d+)(?: tanks?)?\Z")
_ROMAN_TIERS: Mapping[str, int] = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
}


NATION_ALIASES: Mapping[str, str] = {
    "ussr": "USSR",
    "u.s.s.r": "USSR",
    "u.s.s.r.": "USSR",
    "soviet": "USSR",
    "soviet union": "USSR",
    "russia": "USSR",
    "russian": "USSR",
    "germany": "Germany",
    "german": "Germany",
    "usa": "USA",
    "u.s.a": "USA",
    "u.s.a.": "USA",
    "us": "USA",
    "u.s.": "USA",
    "united states": "USA",
    "united states of america": "USA",
    "america": "USA",
    "american": "USA",
    "france": "France",
    "french": "France",
    "uk": "UK",
    "u.k": "UK",
    "u.k.": "UK",
    "united kingdom": "UK",
    "great britain": "UK",
    "britain": "UK",
    "british": "UK",
    "china": "China",
    "chinese": "China",
    "prc": "China",
    "p.r.c.": "China",
    "japan": "Japan",
    "japanese": "Japan",
    "czechoslovakia": "Czechoslovakia",
    "czechoslovak": "Czechoslovakia",
    "czech": "Czechoslovakia",
    "czech republic": "Czechoslovakia",
    "sweden": "Sweden",
    "swedish": "Sweden",
    "poland": "Poland",
    "polish": "Poland",
    "italy": "Italy",
    "italian": "Italy",
}
CANONICAL_NATIONS = frozenset(NATION_ALIASES.values())


def canonicalize_nation(value: str | None) -> str | None:
    if value is None:
        return None
    return NATION_ALIASES.get(identity_key(value))


def nation_from_category(category: str) -> str | None:
    """Map Category:USA Tanks and similar nation buckets onto a canonical nation."""

    leaf = category_leaf(category)
    found = canonicalize_nation(leaf)
    if found is not None:
        return found
    for suffix in (" tanks", " tank"):
        if leaf.endswith(suffix):
            found = canonicalize_nation(leaf[: -len(suffix)])
            if found is not None:
                return found
    return None


def tier_from_category(category: str) -> int | None:
    """Map Category:Tier I Tanks onto a 1-10 tier."""

    match = _TIER_CATEGORY_RE.fullmatch(category_leaf(category))
    if match is None:
        return None
    token = match.group("token")
    if token.isdigit():
        tier = int(token, 10)
    else:
        tier = _ROMAN_TIERS.get(token)
        if tier is None:
            return None
    return tier if 1 <= tier <= 10 else None


def _wikicode_categories(code: Wikicode) -> tuple[str, ...]:
    categories: list[str] = []
    seen: set[str] = set()
    for link in code.filter_wikilinks():
        target = normalize_text(str(link.title).replace("_", " "))
        if not identity_key(target).startswith("category:"):
            continue
        key = identity_key(target)
        if key in seen:
            continue
        seen.add(key)
        categories.append(target)
    return tuple(categories)


def _evidence_categories(
    page: PageDescriptor,
    code: Wikicode,
    revision_categories: Sequence[str] = (),
) -> tuple[str, ...]:
    categories: list[str] = []
    seen: set[str] = set()
    for category in (*page.categories, *revision_categories, *_wikicode_categories(code)):
        key = identity_key(category)
        if key in seen:
            continue
        seen.add(key)
        categories.append(category)
    return tuple(categories)


def resolve_nation_filter(values: Sequence[str]) -> tuple[str, ...]:
    """Canonicalize CLI nation filters; ``ALL`` disables the temporary subset."""

    if any(identity_key(value) == "all" for value in values):
        return ()
    resolved: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = canonicalize_nation(value)
        if canonical is None:
            raise ConfigurationError(f"unknown nation filter: {value}")
        if canonical in seen:
            continue
        seen.add(canonical)
        resolved.append(canonical)
    return tuple(resolved)


def probe_mediawiki_api(client: WikiHttpClient, endpoint: str) -> None:
    """Confirm api.php returns JSON, not the JS cookie interstitial HTML."""

    params = {
        "action": "query",
        "format": "json",
        "list": "allpages",
        "aplimit": "1",
    }
    response = client.get(endpoint, params=params, source_kind="wiki")
    source_url = getattr(response, "url", endpoint)
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        _raise_wiki(
            "invalid_allpages_json",
            f"allpages probe is not valid JSON: {error}",
            "allpages",
            source_url,
        )
    if not isinstance(payload, dict):
        _raise_wiki(
            "malformed_allpages_response",
            "allpages probe root must be an object",
            "allpages",
            source_url,
        )
    if "error" in payload:
        error = payload["error"]
        message = error.get("info") if isinstance(error, dict) else str(error)
        _raise_wiki(
            "wiki_api_error",
            f"MediaWiki allpages probe failed: {message}",
            "allpages",
            source_url,
        )
    query = payload.get("query")
    if not isinstance(query, dict) or "allpages" not in query:
        _raise_wiki(
            "malformed_allpages_response",
            "allpages probe is missing query.allpages",
            "allpages",
            source_url,
        )


def fetch_wiki_revision(
    client: WikiHttpClient,
    endpoint: str,
    page: PageDescriptor | str,
) -> WikiRevision:
    """Fetch one page using the exact legacy-compatible revision request."""

    title = page.title if isinstance(page, PageDescriptor) else page
    params = {
        "action": "query",
        "format": "json",
        "prop": "revisions|pageprops|categories",
        "rvprop": "content",
        "cllimit": "max",
        "redirects": "1",
        "titles": title,
    }
    response = client.get(endpoint, params=params, source_kind="wiki")
    source_url = getattr(response, "url", endpoint)
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        _raise_wiki(
            "invalid_revision_json",
            f"revision response is not valid JSON: {error}",
            title,
            source_url,
        )
    return _revision_from_payload(payload, title, source_url)


def _revision_from_payload(payload: Any, requested_title: str, source_url: str) -> WikiRevision:
    if not isinstance(payload, dict):
        _raise_wiki(
            "malformed_revision_response",
            "revision response root must be an object",
            requested_title,
            source_url,
        )
    if "error" in payload:
        error = payload["error"]
        message = error.get("info") if isinstance(error, dict) else str(error)
        _raise_wiki(
            "wiki_api_error",
            f"MediaWiki revision request failed: {message}",
            requested_title,
            source_url,
        )
    query = payload.get("query")
    if not isinstance(query, dict):
        _raise_wiki(
            "malformed_revision_response",
            "revision response is missing query",
            requested_title,
            source_url,
        )
    _reject_redirect_cycles(query.get("redirects"), requested_title, source_url)
    raw_pages = query.get("pages")
    if isinstance(raw_pages, dict):
        pages = list(raw_pages.values())
    elif isinstance(raw_pages, list):
        pages = raw_pages
    else:
        _raise_wiki(
            "malformed_revision_response",
            "revision response is missing query.pages",
            requested_title,
            source_url,
        )
    if len(pages) != 1 or not isinstance(pages[0], dict):
        _raise_wiki(
            "malformed_revision_response",
            "revision request must resolve to exactly one page",
            requested_title,
            source_url,
        )
    page = pages[0]
    resolved_title = page.get("title")
    if not isinstance(resolved_title, str) or not normalize_text(resolved_title):
        resolved_title = requested_title
    if "missing" in page or page.get("pageid") == -1:
        _raise_wiki(
            "missing_wiki_page",
            "MediaWiki reports that the requested page does not exist",
            resolved_title,
            source_url,
        )
    revisions = page.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        _raise_wiki(
            "missing_revision",
            "wiki page has no revision content",
            resolved_title,
            source_url,
        )
    if not isinstance(revisions[0], dict):
        _raise_wiki(
            "unrecognized_revision_shape",
            "wiki revision is not an object",
            resolved_title,
            source_url,
        )
    text = _revision_text(revisions[0])
    if text is None:
        _raise_wiki(
            "unrecognized_revision_shape",
            "wiki revision has neither legacy content nor a supported main slot",
            resolved_title,
            source_url,
        )
    pageprops = page.get("pageprops")
    display_title = (
        pageprops.get("displaytitle")
        if isinstance(pageprops, dict) and isinstance(pageprops.get("displaytitle"), str)
        else None
    )
    page_id = page.get("pageid")
    if not isinstance(page_id, int) or page_id < 0:
        page_id = None
    return WikiRevision(
        title=resolved_title,
        text=text,
        source_url=source_url,
        display_title=display_title,
        page_id=page_id,
        categories=_page_categories(page),
    )


def _page_categories(page: Mapping[str, Any]) -> tuple[str, ...]:
    raw = page.get("categories")
    if not isinstance(raw, list):
        return ()
    titles: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        normalized = normalize_text(title.replace("_", " "))
        key = identity_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        titles.append(normalized)
    return tuple(titles)


def _revision_text(revision: Mapping[str, Any]) -> str | None:
    legacy = revision.get("*")
    if isinstance(legacy, str):
        return legacy
    slots = revision.get("slots")
    if not isinstance(slots, dict):
        return None
    main = slots.get("main")
    if not isinstance(main, dict):
        return None
    for key in ("*", "content"):
        value = main.get(key)
        if isinstance(value, str):
            return value
    return None


def _reject_redirect_cycles(raw_redirects: Any, requested_title: str, source_url: str) -> None:
    if raw_redirects is None:
        return
    if not isinstance(raw_redirects, list):
        _raise_wiki(
            "malformed_redirects",
            "MediaWiki redirects must be a list",
            requested_title,
            source_url,
        )
    edges: dict[str, str] = {}
    for redirect in raw_redirects:
        if not isinstance(redirect, dict):
            _raise_wiki(
                "malformed_redirects",
                "MediaWiki redirect entry must be an object",
                requested_title,
                source_url,
            )
        source = redirect.get("from")
        target = redirect.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            _raise_wiki(
                "malformed_redirects",
                "MediaWiki redirect entry is missing from/to titles",
                requested_title,
                source_url,
            )
        edges[identity_key(source.replace("_", " "))] = identity_key(target.replace("_", " "))
    for start in edges:
        seen: set[str] = set()
        current = start
        while current in edges:
            if current in seen:
                _raise_wiki(
                    "redirect_loop",
                    "MediaWiki redirect chain contains a loop",
                    requested_title,
                    source_url,
                )
            seen.add(current)
            current = edges[current]


class WikiVehicleParser:
    """Classify and parse one fetched taxonomy candidate."""

    def parse(
        self,
        revision: WikiRevision,
        page: PageDescriptor,
        taxonomy: Taxonomy,
    ) -> WikiParseResult:
        evidence_categories: tuple[str, ...] = ()
        try:
            code = mwparserfromhell.parse(revision.text)
            infoboxes = [
                template
                for template in code.filter_templates(recursive=True)
                if _normalized_template_name(template) in INFOBOX_ALIASES
            ]
            if not infoboxes:
                diagnostic = ExtractionDiagnostic.create(
                    "non_vehicle_candidate",
                    "candidate page has no accepted vehicle infobox",
                    severity=DiagnosticSeverity.INFO,
                    page_title=revision.title,
                    source_url=revision.source_url,
                )
                return WikiParseResult(CandidateStatus.NON_VEHICLE, diagnostics=(diagnostic,))
            if len(infoboxes) != 1:
                _raise_wiki(
                    "competing_vehicle_infoboxes",
                    "candidate page contains multiple accepted vehicle infoboxes",
                    revision.title,
                    revision.source_url,
                )

            values = _logical_parameter_values(infoboxes[0], revision.title, revision.source_url)
            diagnostics: list[ExtractionDiagnostic] = []
            evidence_categories = _evidence_categories(page, code, revision.categories)
            metadata = self._metadata(
                values, revision, page, taxonomy, diagnostics, evidence_categories
            )
            sections, section_diagnostics = extract_tactical_sections(
                code, revision.title, revision.source_url
            )
            diagnostics.extend(section_diagnostics)
            if not sections:
                infobox_sections, infobox_diagnostics = _tactical_sections_from_infobox(
                    infoboxes[0], revision
                )
                sections = infobox_sections
                diagnostics.extend(infobox_diagnostics)
            vehicle = WikiVehicle(
                page=page,
                metadata=metadata,
                tactical_sections=sections,
                diagnostics=tuple(diagnostics),
            )
            return WikiParseResult(
                CandidateStatus.ACCEPTED,
                vehicle=vehicle,
                diagnostics=tuple(diagnostics),
            )
        except WikiExtractionError as error:
            if _is_incomplete_maintenance_page(error.diagnostic.code, evidence_categories):
                diagnostic = ExtractionDiagnostic.create(
                    "incomplete_vehicle_page",
                    "maintenance stub has no canonical vehicle metadata and was excluded",
                    severity=DiagnosticSeverity.INFO,
                    page_title=revision.title,
                    source_url=revision.source_url,
                )
                return WikiParseResult(CandidateStatus.NON_VEHICLE, diagnostics=(diagnostic,))
            return WikiParseResult(CandidateStatus.REJECTED, diagnostics=(error.diagnostic,))
        except (ParserError, RecursionError, TypeError, ValueError) as error:
            diagnostic = ExtractionDiagnostic.create(
                "malformed_wiki_markup",
                f"wiki markup could not be parsed safely ({type(error).__name__})",
                page_title=revision.title,
                source_url=revision.source_url,
            )
            return WikiParseResult(CandidateStatus.REJECTED, diagnostics=(diagnostic,))

    def _metadata(
        self,
        values: Mapping[str, str | None],
        revision: WikiRevision,
        page: PageDescriptor,
        taxonomy: Taxonomy,
        diagnostics: list[ExtractionDiagnostic],
        evidence_categories: Sequence[str],
    ) -> VehicleMetadata:
        suffix = _tank_title_suffix(revision.title)
        identifier = values["identifier"]
        if identifier is not None and _VALID_IDENTIFIER_RE.fullmatch(identifier):
            name = identifier
        else:
            if suffix is None:
                _raise_wiki(
                    "missing_vehicle_identifier",
                    "vehicle has neither a valid infobox identifier nor a Tank: title",
                    revision.title,
                    revision.source_url,
                )
            name = normalize_text(suffix).replace(" ", "_")
            if not _VALID_IDENTIFIER_RE.fullmatch(name):
                _raise_wiki(
                    "invalid_vehicle_identifier",
                    "title fallback does not form a valid stable vehicle identifier",
                    revision.title,
                    revision.source_url,
                )
            if identifier is not None:
                diagnostics.append(
                    ExtractionDiagnostic.create(
                        "invalid_infobox_identifier_fallback",
                        "invalid infobox identifier was replaced by the Tank: title suffix",
                        severity=DiagnosticSeverity.WARNING,
                        page_title=revision.title,
                        source_url=revision.source_url,
                    )
                )

        display_name = values["display_name"]
        if display_name is None and revision.display_title is not None:
            display_name = clean_wiki_text(revision.display_title)
        if display_name is None or not display_name:
            if suffix is None:
                _raise_wiki(
                    "missing_display_name",
                    "vehicle has no display name or Tank: title fallback",
                    revision.title,
                    revision.source_url,
                )
            display_name = normalize_text(suffix)

        primary_class = _resolve_primary_class(
            values["primary_class"] or None,
            (
                *taxonomy_primary_classes(taxonomy, page),
                *(
                    canonical
                    for category in evidence_categories
                    if (canonical := canonicalize_primary_class(category)) is not None
                ),
            ),
            revision,
            diagnostics,
        )

        raw_nation = values["nation"] or None
        if raw_nation is None:
            inferred_nations = {
                nation
                for category in evidence_categories
                if (nation := nation_from_category(category)) is not None
            }
            if len(inferred_nations) > 1:
                _raise_wiki(
                    "contradictory_taxonomy_nations",
                    "page categories provide more than one canonical nation",
                    revision.title,
                    revision.source_url,
                )
            if len(inferred_nations) == 1:
                nation = next(iter(inferred_nations))
                diagnostics.append(
                    ExtractionDiagnostic.create(
                        "infobox_nation_fallback",
                        "taxonomy evidence supplied the missing infobox nation",
                        severity=DiagnosticSeverity.WARNING,
                        page_title=revision.title,
                        source_url=revision.source_url,
                    )
                )
            else:
                _raise_wiki(
                    "missing_nation",
                    "vehicle infobox has no nation",
                    revision.title,
                    revision.source_url,
                )
        else:
            nation = canonicalize_nation(raw_nation)
            if nation is None:
                _raise_wiki(
                    "unknown_nation",
                    f"vehicle infobox nation is not canonical: {raw_nation}",
                    revision.title,
                    revision.source_url,
                )

        raw_tier = values["tier"] or None
        if raw_tier is None:
            inferred_tiers = {
                tier
                for category in evidence_categories
                if (tier := tier_from_category(category)) is not None
            }
            if len(inferred_tiers) > 1:
                _raise_wiki(
                    "contradictory_taxonomy_tiers",
                    "page categories provide more than one vehicle tier",
                    revision.title,
                    revision.source_url,
                )
            if len(inferred_tiers) == 1:
                tier = next(iter(inferred_tiers))
                diagnostics.append(
                    ExtractionDiagnostic.create(
                        "infobox_tier_fallback",
                        "taxonomy evidence supplied the missing infobox tier",
                        severity=DiagnosticSeverity.WARNING,
                        page_title=revision.title,
                        source_url=revision.source_url,
                    )
                )
            else:
                _raise_wiki(
                    "missing_tier",
                    "vehicle infobox has no tier",
                    revision.title,
                    revision.source_url,
                )
        else:
            if not re.fullmatch(r"[0-9]+", raw_tier):
                _raise_wiki(
                    "invalid_tier",
                    "vehicle tier must be a base-10 integer",
                    revision.title,
                    revision.source_url,
                )
            tier = int(raw_tier, 10)
            if not 1 <= tier <= 10:
                _raise_wiki(
                    "invalid_tier",
                    "vehicle tier must be between 1 and 10",
                    revision.title,
                    revision.source_url,
                )

        return VehicleMetadata(
            name=name,
            display_name=display_name,
            primary_class=primary_class,
            subclasses=taxonomy_subclasses(taxonomy, page),
            nation=nation,
            tier=tier,
        )


def _resolve_primary_class(
    infobox_value: str | None,
    taxonomy_classes: Iterable[str],
    revision: WikiRevision,
    diagnostics: list[ExtractionDiagnostic],
) -> str:
    classes = frozenset(taxonomy_classes)
    if len(classes) > 1:
        _raise_wiki(
            "contradictory_taxonomy_classes",
            "taxonomy paths provide more than one canonical primary class",
            revision.title,
            revision.source_url,
        )
    infobox_class = canonicalize_primary_class(infobox_value)
    if infobox_value is not None and infobox_class is None:
        _raise_wiki(
            "unknown_infobox_class",
            f"vehicle infobox class is not canonical: {infobox_value}",
            revision.title,
            revision.source_url,
        )
    if infobox_class is not None and len(classes) == 1:
        taxonomy_class = next(iter(classes))
        if infobox_class == taxonomy_class:
            return infobox_class
        _raise_wiki(
            "primary_class_conflict",
            "infobox and taxonomy primary classes disagree",
            revision.title,
            revision.source_url,
        )
    if infobox_class is not None:
        diagnostics.append(
            ExtractionDiagnostic.create(
                "missing_taxonomy_class_evidence",
                "primary class was accepted without taxonomy class evidence",
                severity=DiagnosticSeverity.WARNING,
                page_title=revision.title,
                source_url=revision.source_url,
            )
        )
        return infobox_class
    if len(classes) == 1:
        diagnostics.append(
            ExtractionDiagnostic.create(
                "infobox_class_fallback",
                "taxonomy evidence supplied the missing infobox primary class",
                severity=DiagnosticSeverity.WARNING,
                page_title=revision.title,
                source_url=revision.source_url,
            )
        )
        return next(iter(classes))
    _raise_wiki(
        "missing_primary_class",
        "vehicle has no canonical primary-class evidence",
        revision.title,
        revision.source_url,
    )


def _logical_parameter_values(
    template: Template, page_title: str, source_url: str
) -> dict[str, str | None]:
    parameters: dict[str, list[str]] = {}
    for parameter in template.params:
        name = _normalized_parameter_name(str(parameter.name))
        parameters.setdefault(name, []).append(clean_wiki_text(str(parameter.value)))

    values: dict[str, str | None] = {}
    for logical_name, aliases in PARAMETER_ALIASES.items():
        present: list[tuple[str, str]] = []
        for alias in aliases:
            for value in parameters.get(alias, ()):
                present.append((alias, value))
        if not present:
            values[logical_name] = None
            continue
        distinct = {identity_key(value) for _, value in present}
        if len(distinct) > 1:
            _raise_wiki(
                "conflicting_infobox_parameters",
                f"conflicting aliases for {logical_name}",
                page_title,
                source_url,
            )
        chosen: str | None = None
        for alias in aliases:
            alias_values = parameters.get(alias)
            if alias_values:
                chosen = alias_values[0]
                break
        values[logical_name] = chosen if chosen else None
    return values


def _normalized_template_name(template: Template) -> str:
    return identity_key(str(template.name).replace("_", " "))


def _normalized_parameter_name(value: str) -> str:
    return identity_key(value.replace("_", " "))


def _tank_title_suffix(title: str) -> str | None:
    namespace, separator, suffix = normalize_text(title.replace("_", " ")).partition(":")
    if separator and identity_key(namespace) == "tank" and suffix:
        return suffix
    return None


def clean_wiki_text(value: str) -> str:
    """Remove wiki markup and comments while retaining visible labels."""

    rendered = _render_nodes(mwparserfromhell.parse(value).nodes)
    return normalize_text(html.unescape(rendered))


def extract_tactical_sections(
    code_or_text: Wikicode | str,
    page_title: str = "",
    source_url: str = "",
) -> tuple[tuple[TacticalSection, ...], tuple[ExtractionDiagnostic, ...]]:
    """Extract accepted heading ranges and preserve tactical block boundaries."""

    code = (
        code_or_text if isinstance(code_or_text, Wikicode) else mwparserfromhell.parse(code_or_text)
    )
    nodes = list(code.nodes)
    sections: list[TacticalSection] = []
    diagnostics: list[ExtractionDiagnostic] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Heading):
            continue
        title = clean_wiki_text(str(node.title))
        if identity_key(title) not in TACTICAL_SECTION_TITLES:
            continue
        body: list[Any] = []
        for following in nodes[index + 1 :]:
            if isinstance(following, Heading) and following.level <= node.level:
                break
            body.append(following)
        text = _clean_tactical_body(body)
        if text:
            sections.append(TacticalSection(title=title, text=text, level=node.level))
        else:
            diagnostics.append(
                ExtractionDiagnostic.create(
                    "empty_tactical_section",
                    f"{title} section is empty after markup cleanup",
                    severity=DiagnosticSeverity.WARNING,
                    page_title=page_title or None,
                    source_url=source_url or None,
                )
            )
    return tuple(sections), tuple(diagnostics)


def _tactical_sections_from_infobox(
    template: Template, revision: WikiRevision
) -> tuple[tuple[TacticalSection, ...], tuple[ExtractionDiagnostic, ...]]:
    """Live TankData stores Performance/Tactics in infobox fields, not headings."""

    parameters: dict[str, list[str]] = {}
    for parameter in template.params:
        name = _normalized_parameter_name(str(parameter.name))
        parameters.setdefault(name, []).append(str(parameter.value))

    sections: list[TacticalSection] = []
    diagnostics: list[ExtractionDiagnostic] = []
    for title, aliases in INFOBOX_TACTICAL_ALIASES.items():
        raw = next((parameters[alias][0] for alias in aliases if parameters.get(alias)), None)
        if raw is None:
            continue
        text = _clean_tactical_body(list(mwparserfromhell.parse(raw).nodes))
        if text:
            sections.append(TacticalSection(title=title, text=text, level=2))
        else:
            diagnostics.append(
                ExtractionDiagnostic.create(
                    "empty_tactical_section",
                    f"{title} section is empty after markup cleanup",
                    severity=DiagnosticSeverity.WARNING,
                    page_title=revision.title,
                    source_url=revision.source_url,
                )
            )
    return tuple(sections), tuple(diagnostics)


def _clean_tactical_body(nodes: Sequence[Any]) -> str:
    source = _TABLE_RE.sub("", "".join(str(node) for node in nodes))
    parsed = mwparserfromhell.parse(source)
    rendered = _render_nodes(parsed.nodes, tactical=True)
    rendered = _CITATION_RE.sub("", rendered)
    blocks: list[str] = []
    paragraph_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph_lines:
            block = normalize_text(" ".join(paragraph_lines))
            if block:
                blocks.append(block)
            paragraph_lines.clear()

    for raw_line in rendered.splitlines():
        line = _SPACE_RE.sub(" ", raw_line).strip()
        if not line:
            flush_paragraph()
            continue
        if _LIST_PREFIX_RE.match(line):
            flush_paragraph()
            item = normalize_text(_LIST_PREFIX_RE.sub("", line))
            if item:
                blocks.append(item)
        else:
            paragraph_lines.append(line)
    flush_paragraph()
    return _EMPTY_LINES_RE.sub("\n\n", "\n".join(blocks)).strip()


def _render_nodes(nodes: Iterable[Any], tactical: bool = False) -> str:
    output: list[str] = []
    for node in nodes:
        if isinstance(node, Text):
            output.append(str(node))
        elif isinstance(node, HTMLEntity):
            output.append(node.normalize())
        elif isinstance(node, (Comment, Template, Heading)):
            continue
        elif isinstance(node, Wikilink):
            target = normalize_text(str(node.title))
            namespace = identity_key(target.partition(":")[0]) if ":" in target else ""
            if namespace in {"category", "file", "image"}:
                continue
            label = node.text if node.text is not None else node.title
            output.append(_render_nodes(mwparserfromhell.parse(str(label)).nodes, tactical))
        elif isinstance(node, ExternalLink):
            if node.title is None:
                output.append(str(node.url))
            else:
                output.append(
                    _render_nodes(mwparserfromhell.parse(str(node.title)).nodes, tactical)
                )
        elif isinstance(node, Tag):
            tag_name = identity_key(str(node.tag))
            if tag_name in {
                "ref",
                "references",
                "gallery",
                "imagemap",
                "math",
                "table",
            }:
                continue
            if tag_name in {"br", "hr"}:
                output.append("\n")
            elif node.contents is not None:
                contents = _render_nodes(mwparserfromhell.parse(str(node.contents)).nodes, tactical)
                if tag_name in {"blockquote", "dd", "div", "dt", "li", "p"}:
                    output.extend(("\n", contents, "\n"))
                else:
                    output.append(contents)
        elif isinstance(node, Argument):
            if node.default is not None:
                output.append(
                    _render_nodes(mwparserfromhell.parse(str(node.default)).nodes, tactical)
                )
        elif not tactical:
            output.append(str(node))
    return "".join(output)


def parse_wiki_vehicle(
    revision: WikiRevision,
    page: PageDescriptor,
    taxonomy: Taxonomy,
) -> WikiParseResult:
    """Parse and classify one revision using the stable functional API."""

    return WikiVehicleParser().parse(revision, page, taxonomy)


@dataclass(slots=True)
class WikiExtractor:
    """Small adapter combining revision retrieval and candidate classification."""

    client: WikiHttpClient
    endpoint: str

    def extract(self, page: PageDescriptor, taxonomy: Taxonomy) -> WikiParseResult:
        try:
            revision = fetch_wiki_revision(self.client, self.endpoint, page)
        except WikiExtractionError as error:
            return WikiParseResult(CandidateStatus.REJECTED, diagnostics=(error.diagnostic,))
        return parse_wiki_vehicle(revision, page, taxonomy)


def _is_incomplete_maintenance_page(code: str, categories: Sequence[str]) -> bool:
    """Broken TankData stubs are category members, not vehicles missing a class."""

    if code not in _INCOMPLETE_METADATA_CODES:
        return False
    return any(category_leaf(category) == _MAINTENANCE_CATEGORY for category in categories)


def _raise_wiki(code: str, message: str, page_title: str, source_url: str) -> None:
    raise WikiExtractionError(
        ExtractionDiagnostic.create(
            code,
            message,
            page_title=page_title,
            source_url=source_url,
        )
    )
