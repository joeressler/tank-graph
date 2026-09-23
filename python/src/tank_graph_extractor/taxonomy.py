"""Recursive MediaWiki category traversal and canonical taxonomy evidence."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .models import (
    ExtractionDiagnostic,
    PageDescriptor,
    Taxonomy,
    TaxonomyError,
    WikiHttpClient,
    deterministic_key,
    normalize_mediawiki_title,
    normalize_text,
)

PRIMARY_CLASS_ALIASES: Mapping[str, str] = {
    "light tank": "Light Tanks",
    "light tanks": "Light Tanks",
    "medium tank": "Medium Tanks",
    "medium tanks": "Medium Tanks",
    "heavy tank": "Heavy Tanks",
    "heavy tanks": "Heavy Tanks",
    "tank destroyer": "Tank Destroyers",
    "tank destroyers": "Tank Destroyers",
    "spg": "SPGs",
    "spgs": "SPGs",
    "artillery": "SPGs",
    "self-propelled gun": "SPGs",
    "self-propelled guns": "SPGs",
    "light": "Light Tanks",
    "lt": "Light Tanks",
    "lighttank": "Light Tanks",
    "medium": "Medium Tanks",
    "mt": "Medium Tanks",
    "mediumtank": "Medium Tanks",
    "heavy": "Heavy Tanks",
    "ht": "Heavy Tanks",
    "heavytank": "Heavy Tanks",
    "td": "Tank Destroyers",
    "ttd": "Tank Destroyers",
    "at-spg": "Tank Destroyers",
    "atspg": "Tank Destroyers",
}
SUBCLASS_ALIASES: Mapping[str, str] = {
    "autoloader": "Autoloaders",
    "autoloaders": "Autoloaders",
}
CANONICAL_PRIMARY_CLASSES = frozenset(PRIMARY_CLASS_ALIASES.values())
ORGANIZATIONAL_CATEGORIES = frozenset(
    {
        "tanks",
        "tanks by nation",
        "tanks by tier",
        "tanks by type",
        "premium tanks",
        "removed tanks",
        "tanktree",
        "unintroduced tanks",
        "upcoming tanks",
    }
)


def category_leaf(value: str) -> str:
    """Return a normalized category value without the MediaWiki namespace."""

    normalized = normalize_mediawiki_title(value)
    if normalized.startswith("category:"):
        normalized = normalized.split(":", 1)[1]
    return normalized


def canonicalize_primary_class(value: str | None) -> str | None:
    if value is None:
        return None
    return PRIMARY_CLASS_ALIASES.get(category_leaf(value))


def canonicalize_subclass(value: str | None) -> str | None:
    if value is None:
        return None
    return SUBCLASS_ALIASES.get(category_leaf(value))


def is_organizational_category(value: str) -> bool:
    return category_leaf(value) in ORGANIZATIONAL_CATEGORIES


def taxonomy_primary_classes(
    taxonomy: Taxonomy, page: PageDescriptor | str | int
) -> frozenset[str]:
    descriptor = taxonomy.page_for(page)
    if descriptor is None:
        return frozenset()
    evidence: set[str] = set()
    for path in descriptor.category_paths:
        for category in path:
            canonical = canonicalize_primary_class(category)
            if canonical is not None:
                evidence.add(canonical)
    for category in descriptor.categories:
        canonical = canonicalize_primary_class(category)
        if canonical is not None:
            evidence.add(canonical)
    return frozenset(evidence)


def taxonomy_subclasses(taxonomy: Taxonomy, page: PageDescriptor | str | int) -> tuple[str, ...]:
    descriptor = taxonomy.page_for(page)
    if descriptor is None:
        return ()
    evidence: set[str] = set()
    for path in descriptor.category_paths:
        for category in path:
            canonical = canonicalize_subclass(category)
            if canonical is not None:
                evidence.add(canonical)
    for category in descriptor.categories:
        canonical = canonicalize_subclass(category)
        if canonical is not None:
            evidence.add(canonical)
    return tuple(sorted(evidence, key=deterministic_key))


@dataclass(slots=True)
class _MutablePage:
    title: str
    page_id: int | None
    categories: set[str]


class TaxonomyCrawler:
    """FIFO crawler that consumes all pagination before advancing categories."""

    def __init__(
        self,
        client: WikiHttpClient,
        endpoint: str,
        root_category: str = "Category:Tanks",
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._root_category = normalize_text(root_category.replace("_", " "))

    def crawl(self) -> Taxonomy:
        root_key = normalize_mediawiki_title(self._root_category)
        queue: deque[tuple[str, str]] = deque([(root_key, self._root_category)])
        queued = {root_key}
        visited: set[str] = set()
        category_titles: dict[str, str] = {root_key: self._root_category}
        parent_links: set[tuple[str, str]] = set()
        pages: dict[tuple[str, int | str], _MutablePage] = {}
        title_identities: dict[str, tuple[str, int | str]] = {}
        log = logging.getLogger(__name__)
        log.info("taxonomy: starting crawl of %s", self._root_category)

        while queue:
            current_key, current_title = queue.popleft()
            if current_key in visited:
                continue
            visited.add(current_key)
            log.info(
                "taxonomy: fetching %s (visited %d, queued %d, candidates %d)",
                current_title,
                len(visited),
                len(queue),
                len(pages),
            )
            continuation: str | None = None
            seen_continuations: set[str] = set()
            while True:
                payload, source_url = self._fetch_members(current_title, continuation)
                members, continuation = self._validate_members_payload(
                    payload, current_title, source_url
                )
                if continuation is not None:
                    if continuation in seen_continuations:
                        self._fail(
                            "repeated_category_continuation",
                            "category response repeated a continuation token",
                            page_title=current_title,
                            source_url=source_url,
                        )
                    seen_continuations.add(continuation)
                for member in members:
                    title = member.get("title")
                    if not isinstance(title, str) or not normalize_text(title):
                        self._fail(
                            "malformed_category_member",
                            "category member is missing a non-empty title",
                            page_title=current_title,
                            source_url=source_url,
                        )
                    member_title = normalize_text(title.replace("_", " "))
                    member_key = normalize_mediawiki_title(member_title)
                    if _is_subcategory(member):
                        category_titles.setdefault(member_key, member_title)
                        parent_links.add((member_key, current_key))
                        if member_key not in visited and member_key not in queued:
                            queue.append((member_key, member_title))
                            queued.add(member_key)
                    else:
                        self._upsert_page(
                            pages,
                            title_identities,
                            member,
                            member_title,
                            member_key,
                            current_key,
                            source_url,
                        )
                if continuation is None:
                    break

        descriptors = self._build_descriptors(pages, root_key, parent_links)
        log.info(
            "taxonomy: finished %d categories and %d candidate pages",
            len(visited),
            len(descriptors),
        )
        return Taxonomy(
            root_category=category_titles[root_key],
            categories=tuple(sorted(visited, key=deterministic_key)),
            parent_links=tuple(parent_links),
            pages=descriptors,
        )

    def _fetch_members(self, category_title: str, continuation: str | None) -> tuple[Any, str]:
        params = {
            "action": "query",
            "format": "json",
            "list": "categorymembers",
            "cmtitle": category_title,
            "cmtype": "page|subcat",
            "cmlimit": "max",
        }
        if continuation is not None:
            params["cmcontinue"] = continuation
        response = self._client.get(self._endpoint, params=params, source_kind="wiki")
        source_url = getattr(response, "url", self._endpoint)
        try:
            return response.json(), source_url
        except (TypeError, ValueError) as error:
            self._fail(
                "invalid_category_json",
                f"category response is not valid JSON: {error}",
                page_title=category_title,
                source_url=source_url,
            )

    def _validate_members_payload(
        self, payload: Any, category_title: str, source_url: str
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        if not isinstance(payload, dict):
            self._fail(
                "malformed_category_response",
                "category response root must be an object",
                page_title=category_title,
                source_url=source_url,
            )
        if "error" in payload:
            error = payload["error"]
            message = error.get("info") if isinstance(error, dict) else str(error)
            self._fail(
                "wiki_api_error",
                f"MediaWiki category request failed: {message}",
                page_title=category_title,
                source_url=source_url,
            )
        query = payload.get("query")
        members = query.get("categorymembers") if isinstance(query, dict) else None
        if not isinstance(members, list) or not all(isinstance(member, dict) for member in members):
            self._fail(
                "malformed_category_response",
                "category response is missing query.categorymembers",
                page_title=category_title,
                source_url=source_url,
            )
        continuation: str | None = None
        continuation_object = payload.get("continue")
        if continuation_object is not None:
            if not isinstance(continuation_object, dict):
                self._fail(
                    "malformed_category_continuation",
                    "category continuation must be an object",
                    page_title=category_title,
                    source_url=source_url,
                )
            token = continuation_object.get("cmcontinue")
            if not isinstance(token, str) or not token:
                self._fail(
                    "malformed_category_continuation",
                    "category continuation is missing cmcontinue",
                    page_title=category_title,
                    source_url=source_url,
                )
            continuation = token
        return members, continuation

    def _upsert_page(
        self,
        pages: dict[tuple[str, int | str], _MutablePage],
        title_identities: dict[str, tuple[str, int | str]],
        member: Mapping[str, Any],
        title: str,
        title_key: str,
        category_key: str,
        source_url: str,
    ) -> None:
        raw_page_id = member.get("pageid")
        page_id: int | None
        if raw_page_id is None:
            page_id = None
        elif isinstance(raw_page_id, int) and raw_page_id >= 0:
            page_id = raw_page_id
        else:
            self._fail(
                "malformed_page_id",
                "candidate page ID must be a non-negative integer",
                page_title=title,
                source_url=source_url,
            )

        identity: tuple[str, int | str]
        if page_id is not None:
            identity = ("page_id", page_id)
            fallback_identity = title_identities.get(title_key)
            if (
                fallback_identity is not None
                and fallback_identity[0] == "title"
                and fallback_identity in pages
            ):
                fallback = pages.pop(fallback_identity)
                existing = pages.get(identity)
                if existing is None:
                    fallback.page_id = page_id
                    pages[identity] = fallback
                else:
                    existing.categories.update(fallback.categories)
        else:
            identity = title_identities.get(title_key, ("title", title_key))

        existing = pages.get(identity)
        if existing is None:
            existing = _MutablePage(title=title, page_id=page_id, categories=set())
            pages[identity] = existing
        elif deterministic_key(title) < deterministic_key(existing.title):
            existing.title = title
        existing.categories.add(category_key)
        title_identities[title_key] = identity

    def _build_descriptors(
        self,
        pages: Mapping[tuple[str, int | str], _MutablePage],
        root_key: str,
        parent_links: Iterable[tuple[str, str]],
    ) -> tuple[PageDescriptor, ...]:
        parents: dict[str, set[str]] = defaultdict(set)
        for child, parent in parent_links:
            parents[child].add(parent)

        def paths_to_root(category: str, trail: frozenset[str]) -> set[tuple[str, ...]]:
            if category == root_key:
                return {(root_key,)}
            if category in trail:
                return set()
            paths: set[tuple[str, ...]] = set()
            category_parents = sorted(parents.get(category, ()), key=deterministic_key)
            if not category_parents:
                return {(category,)}
            for parent in category_parents:
                for prefix in paths_to_root(parent, trail | {category}):
                    paths.add((*prefix, category))
            return paths

        descriptors: list[PageDescriptor] = []
        for page in pages.values():
            paths: set[tuple[str, ...]] = set()
            for category in page.categories:
                paths.update(paths_to_root(category, frozenset()))
            descriptors.append(
                PageDescriptor(
                    title=page.title,
                    page_id=page.page_id,
                    categories=tuple(page.categories),
                    category_paths=tuple(paths),
                )
            )
        return tuple(descriptors)

    @staticmethod
    def _fail(
        code: str,
        message: str,
        *,
        page_title: str,
        source_url: str,
    ) -> None:
        raise TaxonomyError(
            ExtractionDiagnostic.create(
                code,
                message,
                page_title=page_title,
                source_url=source_url,
            )
        )


def _is_subcategory(member: Mapping[str, Any]) -> bool:
    return member.get("type") == "subcat" or member.get("ns") == 14


def crawl_taxonomy(
    client: WikiHttpClient,
    endpoint: str,
    root_category: str = "Category:Tanks",
) -> Taxonomy:
    """Collect one complete taxonomy using the stable functional API."""

    return TaxonomyCrawler(client, endpoint, root_category).crawl()
