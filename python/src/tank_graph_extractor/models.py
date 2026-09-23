"""Stable domain records shared by taxonomy and wiki extraction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .identity import identity_key, normalize_display, sort_key


def normalize_text(value: str) -> str:
    """Return the display-safe normalization required by the interchange contract."""

    return normalize_display(value)


def deterministic_key(value: str) -> tuple[str, str]:
    """Return the shared deterministic string ordering tuple."""

    return sort_key(value)


def normalize_mediawiki_title(value: str) -> str:
    """Normalize a full MediaWiki title without discarding its namespace."""

    return identity_key(value.replace("_", " "))


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CandidateStatus(StrEnum):
    ACCEPTED = "accepted"
    NON_VEHICLE = "non_vehicle"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ExtractionDiagnostic:
    """A serializable, deterministic parsing diagnostic with source context."""

    code: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    page_title: str | None = None
    source_url: str | None = None
    details: tuple[tuple[str, str], ...] = ()

    @classmethod
    def create(
        cls,
        code: str,
        message: str,
        *,
        severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
        page_title: str | None = None,
        source_url: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> ExtractionDiagnostic:
        stable_details = tuple(
            sorted(
                ((str(key), str(value)) for key, value in (details or {}).items()),
                key=lambda item: (item[0], item[1]),
            )
        )
        return cls(
            code=code,
            message=message,
            severity=severity,
            page_title=page_title,
            source_url=source_url,
            details=stable_details,
        )


class ExtractionError(ValueError):
    """Base exception for terminal extraction diagnostics."""

    def __init__(self, diagnostic: ExtractionDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


class TaxonomyError(ExtractionError):
    """A terminal category traversal failure."""


class WikiExtractionError(ExtractionError):
    """A terminal wiki response or vehicle parsing failure."""


@dataclass(frozen=True, slots=True)
class PageDescriptor:
    """A unique candidate page and all category evidence discovered for it."""

    title: str
    page_id: int | None = None
    categories: tuple[str, ...] = ()
    category_paths: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        title = normalize_text(self.title.replace("_", " "))
        if not title:
            raise ValueError("page title must not be empty")
        if self.page_id is not None and self.page_id < 0:
            raise ValueError("page_id must be non-negative")
        categories = tuple(sorted(set(self.categories), key=deterministic_key))
        paths = tuple(
            sorted(
                {tuple(path) for path in self.category_paths},
                key=lambda path: tuple(deterministic_key(part) for part in path),
            )
        )
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "categories", categories)
        object.__setattr__(self, "category_paths", paths)

    @property
    def identity(self) -> tuple[str, int | str]:
        if self.page_id is not None:
            return ("page_id", self.page_id)
        return ("title", normalize_mediawiki_title(self.title))


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """Complete category graph and deterministically ordered candidate pages."""

    root_category: str
    categories: tuple[str, ...]
    parent_links: tuple[tuple[str, str], ...]
    pages: tuple[PageDescriptor, ...]
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()
    _pages_by_title: Mapping[str, PageDescriptor] = field(
        init=False, repr=False, compare=False, hash=False
    )
    _pages_by_id: Mapping[int, PageDescriptor] = field(
        init=False, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        root = normalize_text(self.root_category.replace("_", " "))
        categories = tuple(sorted(set(self.categories), key=deterministic_key))
        links = tuple(
            sorted(
                set(self.parent_links),
                key=lambda link: (deterministic_key(link[0]), deterministic_key(link[1])),
            )
        )
        pages = tuple(
            sorted(
                self.pages,
                key=lambda page: (
                    deterministic_key(page.title),
                    page.page_id is None,
                    page.page_id if page.page_id is not None else -1,
                ),
            )
        )
        by_title = {normalize_mediawiki_title(page.title): page for page in pages}
        by_id = {page.page_id: page for page in pages if page.page_id is not None}
        object.__setattr__(self, "root_category", root)
        object.__setattr__(self, "categories", categories)
        object.__setattr__(self, "parent_links", links)
        object.__setattr__(self, "pages", pages)
        object.__setattr__(self, "_pages_by_title", by_title)
        object.__setattr__(self, "_pages_by_id", by_id)

    def page_for(self, page: PageDescriptor | str | int) -> PageDescriptor | None:
        if isinstance(page, PageDescriptor):
            if page.page_id is not None and page.page_id in self._pages_by_id:
                return self._pages_by_id[page.page_id]
            return self._pages_by_title.get(normalize_mediawiki_title(page.title))
        if isinstance(page, int):
            return self._pages_by_id.get(page)
        return self._pages_by_title.get(normalize_mediawiki_title(page))


@dataclass(frozen=True, slots=True)
class VehicleMetadata:
    """Validated wiki metadata ready for record assembly."""

    name: str
    display_name: str
    primary_class: str
    subclasses: tuple[str, ...]
    nation: str
    tier: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", normalize_text(self.name))
        object.__setattr__(self, "display_name", normalize_text(self.display_name))
        object.__setattr__(
            self,
            "subclasses",
            tuple(sorted(set(self.subclasses), key=deterministic_key)),
        )


@dataclass(frozen=True, slots=True)
class TacticalSection:
    """Clean tactical text retained with its source heading boundary."""

    title: str
    text: str
    level: int


@dataclass(frozen=True, slots=True)
class WikiRevision:
    """Fetched revision content and page properties needed by the parser."""

    title: str
    text: str
    source_url: str
    display_title: str | None = None
    page_id: int | None = None
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WikiVehicle:
    """A parsed vehicle with clean tactical sections and diagnostics."""

    page: PageDescriptor
    metadata: VehicleMetadata
    tactical_sections: tuple[TacticalSection, ...] = ()
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()

    @property
    def tactical_texts(self) -> tuple[str, ...]:
        return tuple(section.text for section in self.tactical_sections)


@dataclass(frozen=True, slots=True)
class WikiParseResult:
    """Classified outcome for a taxonomy candidate page."""

    status: CandidateStatus
    vehicle: WikiVehicle | None = None
    diagnostics: tuple[ExtractionDiagnostic, ...] = ()


@runtime_checkable
class HttpResponse(Protocol):
    url: str
    text: str
    content: bytes

    def json(self) -> Any: ...


@runtime_checkable
class WikiHttpClient(Protocol):
    def get(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        source_kind: str = "wiki",
    ) -> HttpResponse: ...
