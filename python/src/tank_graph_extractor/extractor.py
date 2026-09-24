"""Orchestrate collection, parsing, NLP, validation, and atomic publication."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .assemble import AssemblyResult, assemble_records
from .config import ExtractorConfig
from .errors import BotInterstitialError, ConsecutiveInterstitialError, TankGraphError
from .guides import GuideDiagnostic, GuideParseError, GuideSegment, parse_guide_html
from .http import HttpClient
from .identity import identity_key, sort_key
from .models import (
    CandidateStatus,
    DiagnosticSeverity,
    ExtractionDiagnostic,
    WikiVehicle,
)
from .nlp import ConceptExtractor, get_nlp
from .output import write_dataset_atomic
from .policy import PolicyChecker, PolicyReport
from .taxonomy import crawl_taxonomy
from .wiki import WikiExtractor, canonicalize_nation, probe_mediawiki_api


class ExtractionRunError(TankGraphError):
    """The run accumulated candidate or source errors and cannot publish."""

    def __init__(self, message: str, diagnostics: tuple[Any, ...] = ()) -> None:
        self.diagnostics = diagnostics
        super().__init__(message)

    def __str__(self) -> str:
        text = super().__str__()
        counts: dict[str, int] = {}
        for item in self.diagnostics:
            code = getattr(item, "code", None)
            if not isinstance(code, str) or not code:
                continue
            counts[code] = counts.get(code, 0) + 1
        if not counts:
            return text
        summary = ", ".join(f"{code}={count}" for code, count in sorted(counts.items()))
        return f"{text} ({summary})"


def _allowed_nations(config: ExtractorConfig) -> frozenset[str]:
    allowed: set[str] = set()
    for nation in config.nations:
        canonical = canonicalize_nation(nation)
        if canonical is None:
            raise ExtractionRunError(f"nation filter is not canonical: {nation}")
        allowed.add(canonical)
    return frozenset(allowed)


@dataclass(frozen=True, slots=True)
class RunSummary:
    category_count: int
    candidate_page_count: int
    accepted_tank_count: int
    rejected_candidate_count: int
    guide_page_count: int
    strategy_count: int
    keyword_count: int
    retry_count: int
    output_path: Path
    skipped_guide_count: int = 0

    def to_text(self) -> str:
        return "\n".join(
            (
                f"categories: {self.category_count}",
                f"candidate_pages: {self.candidate_page_count}",
                f"accepted_tanks: {self.accepted_tank_count}",
                f"rejected_candidates: {self.rejected_candidate_count}",
                f"guide_pages: {self.guide_page_count}",
                f"skipped_guides: {self.skipped_guide_count}",
                f"strategies: {self.strategy_count}",
                f"keywords: {self.keyword_count}",
                f"retries: {self.retry_count}",
                f"output: {self.output_path}",
            )
        )


def _collect_guides(
    config: ExtractorConfig,
    client: HttpClient,
    policy: PolicyReport,
    *,
    logger: logging.Logger,
) -> tuple[tuple[GuideSegment, ...], tuple[GuideDiagnostic, ...], int]:
    segments: list[GuideSegment] = []
    diagnostics: list[GuideDiagnostic] = []
    fetched: set[str] = set()
    allowed = set(policy.allowed_guides)
    index_url = urljoin(config.guide_root, "tank-coach-video-guides/")
    coach_prefix = urlsplit(index_url).path.rstrip("/") + "/"
    child_urls = {
        url for url in allowed if url != index_url and urlsplit(url).path.startswith(coach_prefix)
    }
    direct_urls = sorted(allowed - child_urls)
    discovered_children: set[str] = set()

    for url in direct_urls:
        if url in fetched:
            continue
        try:
            response = client.get(url)
        except ConsecutiveInterstitialError:
            raise
        except BotInterstitialError:
            logger.warning("skipping blocked guide page %s", url)
            continue
        try:
            parsed = parse_guide_html(
                response.text,
                response.url or url,
                guide_root=config.guide_root,
                allowlist=config.guide_paths,
            )
        except GuideParseError as error:
            raise ExtractionRunError(f"guide parsing failed for {url}: {error}") from error
        fetched.add(url)
        segments.extend(parsed.segments)
        diagnostics.extend(parsed.diagnostics)
        discovered_children.update(parsed.discovered_children)

    for url in sorted(child_urls & discovered_children):
        try:
            response = client.get(url)
        except ConsecutiveInterstitialError:
            raise
        except BotInterstitialError:
            logger.warning("skipping blocked guide page %s", url)
            continue
        try:
            parsed = parse_guide_html(
                response.text,
                response.url or url,
                guide_root=config.guide_root,
                allowlist=config.guide_paths,
            )
        except GuideParseError as error:
            raise ExtractionRunError(f"guide parsing failed for {url}: {error}") from error
        fetched.add(url)
        segments.extend(parsed.segments)
        diagnostics.extend(parsed.diagnostics)

    return tuple(segments), tuple(diagnostics), len(fetched)


def _collect_wiki(
    config: ExtractorConfig,
    client: HttpClient,
    *,
    logger: logging.Logger,
) -> tuple[Any, tuple[WikiVehicle, ...], tuple[ExtractionDiagnostic, ...], int]:
    allowed_nations = _allowed_nations(config)
    taxonomy = crawl_taxonomy(client, config.wiki_endpoint, config.root_category)
    pause = getattr(client, "pause_between_phases", None)
    if callable(pause):
        pause("pausing after taxonomy discovery before article fetches")
    extractor = WikiExtractor(client, config.wiki_endpoint)
    vehicles: list[WikiVehicle] = []
    diagnostics: list[ExtractionDiagnostic] = []
    rejection_diagnostics: list[ExtractionDiagnostic] = []
    rejected = 0

    for page in taxonomy.pages:
        try:
            result = extractor.extract(page, taxonomy)
        except ConsecutiveInterstitialError:
            raise
        except BotInterstitialError:
            logger.warning("skipping blocked wiki page %s", page.title)
            continue
        diagnostics.extend(result.diagnostics)
        if result.status is CandidateStatus.ACCEPTED:
            if result.vehicle is None:
                raise ExtractionRunError(f"accepted candidate {page.title!r} has no parsed vehicle")
            nation = result.vehicle.metadata.nation
            if allowed_nations and nation not in allowed_nations:
                logger.info(
                    "skipping wiki page %s nation=%s (temporary nation filter %s)",
                    page.title,
                    nation,
                    ",".join(config.nations),
                )
                continue
            vehicles.append(result.vehicle)
        elif result.status is CandidateStatus.REJECTED:
            rejected += 1
            rejection_diagnostics.extend(result.diagnostics)
            for diagnostic in result.diagnostics:
                logger.warning(
                    "rejected wiki page %s: %s (%s)",
                    page.title,
                    diagnostic.code,
                    diagnostic.message,
                )

    vehicles, duplicate_rejected, duplicate_diagnostics = _unique_wiki_vehicles(
        vehicles, logger=logger
    )
    rejected += duplicate_rejected
    diagnostics.extend(duplicate_diagnostics)
    rejection_diagnostics.extend(duplicate_diagnostics)

    if rejected and not vehicles:
        raise ExtractionRunError(
            f"{rejected} vehicle candidate(s) were rejected; output was not published",
            tuple(rejection_diagnostics),
        )
    if rejected:
        logger.warning(
            "%d vehicle candidate(s) were rejected; publishing %d accepted vehicle(s)",
            rejected,
            len(vehicles),
        )
    return taxonomy, tuple(vehicles), tuple(diagnostics), rejected


def _unique_wiki_vehicles(
    vehicles: list[WikiVehicle],
    *,
    logger: logging.Logger,
) -> tuple[list[WikiVehicle], int, tuple[ExtractionDiagnostic, ...]]:
    """Keep one accepted vehicle per identity so a wiki collision cannot drop the batch."""

    ordered = sorted(
        vehicles,
        key=lambda vehicle: (
            sort_key(vehicle.metadata.name),
            sort_key(vehicle.page.title),
            vehicle.page.page_id if vehicle.page.page_id is not None else -1,
        ),
    )
    kept: list[WikiVehicle] = []
    seen: dict[str, WikiVehicle] = {}
    diagnostics: list[ExtractionDiagnostic] = []
    skipped = 0
    for vehicle in ordered:
        key = identity_key(vehicle.metadata.name)
        previous = seen.get(key)
        if previous is None:
            seen[key] = vehicle
            kept.append(vehicle)
            continue
        skipped += 1
        message = (
            f"duplicate tank identity {vehicle.metadata.name!r} on {vehicle.page.title!r} "
            f"conflicts with {previous.metadata.name!r} on {previous.page.title!r}; "
            "keeping the first in canonical order"
        )
        logger.warning("%s", message)
        diagnostics.append(
            ExtractionDiagnostic.create(
                "duplicate_tank_identity",
                message,
                severity=DiagnosticSeverity.WARNING,
                page_title=vehicle.page.title,
                details={
                    "kept_page": previous.page.title,
                    "kept_name": previous.metadata.name,
                    "dropped_name": vehicle.metadata.name,
                },
            )
        )
    return kept, skipped, tuple(diagnostics)


def run_extraction(
    config: ExtractorConfig,
    *,
    schema_path: Path = Path("docs/specs/tanks-data.schema.json"),
    client: HttpClient | None = None,
    sentence_nlp: Any | None = None,
    logger: logging.Logger | None = None,
) -> RunSummary:
    """Run the complete extraction pipeline after all startup preflights."""

    log = logger or logging.getLogger(__name__)
    schema_path = Path(schema_path)
    if not schema_path.is_file():
        raise ExtractionRunError(f"JSON Schema does not exist: {schema_path}")

    # Loading before client construction guarantees a missing model cannot open sockets.
    nlp_model = sentence_nlp if sentence_nlp is not None else get_nlp()
    owned_client = client is None
    http = client if client is not None else HttpClient(config)
    if http.config != config:
        raise ExtractionRunError("injected HTTP client uses different configuration")

    try:
        policy_report = PolicyChecker(config, http, logger=log).check_sources()
        try:
            probe_mediawiki_api(http, config.wiki_endpoint)
        except ConsecutiveInterstitialError:
            raise
        except BotInterstitialError as error:
            raise ExtractionRunError(
                "MediaWiki API returned a JS cookie/anti-bot interstitial instead of JSON. "
                "Capture WIKI_COOKIE from the wiki host document request after "
                "#mw-content-text loads."
            ) from error
        guide_segments, guide_diagnostics, guide_count = _collect_guides(
            config, http, policy_report, logger=log
        )
        taxonomy, vehicles, wiki_diagnostics, rejected = _collect_wiki(
            config, http, logger=log
        )
        assembly: AssemblyResult = assemble_records(
            vehicles,
            guide_segments,
            sentence_nlp=nlp_model,
            concept_extractor=ConceptExtractor(nlp_model),
        )
        write_dataset_atomic(
            assembly.records,
            config.output_path,
            schema_path,
            require_nonempty=True,
        )
    finally:
        if owned_client:
            http.close()

    for diagnostic in (*wiki_diagnostics, *guide_diagnostics):
        log.info("%s: %s", getattr(diagnostic, "reason", "diagnostic"), diagnostic)

    records = assembly.records
    return RunSummary(
        category_count=len(taxonomy.categories),
        candidate_page_count=len(taxonomy.pages),
        accepted_tank_count=len(records),
        rejected_candidate_count=rejected,
        guide_page_count=guide_count,
        strategy_count=sum(len(record["strategies"]) for record in records),
        keyword_count=sum(len(record["keywords"]) for record in records),
        retry_count=int(getattr(http, "retry_count", 0)),
        output_path=config.output_path,
        skipped_guide_count=len(policy_report.skipped_guides),
    )
