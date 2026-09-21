"""Orchestrate collection, parsing, NLP, validation, and atomic publication."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from .assemble import AssemblyResult, assemble_records
from .config import ExtractorConfig
from .errors import TankGraphError, safe_url
from .guides import GuideDiagnostic, GuideParseError, GuideSegment, parse_guide_html
from .http import HttpClient
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
from .wiki import WikiExtractor


class ExtractionRunError(TankGraphError):
    """The run accumulated candidate or source errors and cannot publish."""

    def __init__(self, message: str, diagnostics: tuple[Any, ...] = ()) -> None:
        self.diagnostics = diagnostics
        super().__init__(message)


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


def _diagnostic_text(diagnostic: Any) -> str:
    code = getattr(diagnostic, "code", None)
    reason = getattr(diagnostic, "reason", None)
    message = getattr(diagnostic, "message", str(diagnostic))
    title = getattr(diagnostic, "page_title", None)
    prefix = code or reason or "diagnostic"
    if title:
        return f"{prefix}: {title}: {message}"
    return f"{prefix}: {message}"


def _collect_guides(
    config: ExtractorConfig,
    client: HttpClient,
    policy: PolicyReport,
    log: logging.Logger,
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
    log.info("guides: fetching %d authorized page(s)", len(allowed))

    for url in direct_urls:
        if url in fetched:
            continue
        log.info("guides: GET %s", safe_url(url))
        response = client.get(url)
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
        log.info(
            "guides: retained %d segment(s) and %d diagnostic(s) from %s",
            len(parsed.segments),
            len(parsed.diagnostics),
            safe_url(response.url or url),
        )

    for url in sorted(child_urls & discovered_children):
        log.info("guides: GET discovered child %s", safe_url(url))
        response = client.get(url)
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
        log.info(
            "guides: retained %d segment(s) from %s",
            len(parsed.segments),
            safe_url(response.url or url),
        )

    log.info("guides: finished %d page(s), %d assignable segment(s)", len(fetched), len(segments))
    return tuple(segments), tuple(diagnostics), len(fetched)


def _collect_wiki(
    config: ExtractorConfig,
    client: HttpClient,
    log: logging.Logger,
) -> tuple[Any, tuple[WikiVehicle, ...], tuple[ExtractionDiagnostic, ...], int]:
    log.info("wiki: crawling taxonomy from %s", config.root_category)
    taxonomy = crawl_taxonomy(client, config.wiki_endpoint, config.root_category)
    extractor = WikiExtractor(client, config.wiki_endpoint)
    vehicles: list[WikiVehicle] = []
    diagnostics: list[ExtractionDiagnostic] = []
    rejected = 0
    excluded = 0
    total = len(taxonomy.pages)
    log.info("wiki: extracting %d candidate page(s)", total)
    started = time.monotonic()

    for index, page in enumerate(taxonomy.pages, start=1):
        elapsed = time.monotonic() - started
        remaining = ((elapsed / (index - 1)) * (total - index + 1)) if index > 1 else 0.0
        log.info(
            "wiki: %d/%d GET %s (elapsed %.0fs, ~%.0fs remaining)",
            index,
            total,
            page.title,
            elapsed,
            remaining,
        )
        result = extractor.extract(page, taxonomy)
        diagnostics.extend(result.diagnostics)
        if result.status is CandidateStatus.ACCEPTED:
            if result.vehicle is None:
                raise ExtractionRunError(f"accepted candidate {page.title!r} has no parsed vehicle")
            vehicles.append(result.vehicle)
            log.info(
                "wiki: accepted %s as %s (%s, tier %s)",
                page.title,
                result.vehicle.metadata.name,
                result.vehicle.metadata.primary_class,
                result.vehicle.metadata.tier,
            )
        elif result.status is CandidateStatus.REJECTED:
            rejected += 1
            detail = (
                _diagnostic_text(result.diagnostics[0]) if result.diagnostics else "rejected"
            )
            log.warning("wiki: rejected %s: %s", page.title, detail)
        else:
            excluded += 1
            log.info("wiki: excluded non-vehicle %s", page.title)

    log.info(
        "wiki: accepted %d, excluded %d, rejected %d",
        len(vehicles),
        excluded,
        rejected,
    )
    if rejected:
        samples = [
            _diagnostic_text(diagnostic)
            for diagnostic in diagnostics
            if getattr(diagnostic, "severity", None) is DiagnosticSeverity.ERROR
        ][:8]
        detail = "; ".join(samples) if samples else "see logs"
        raise ExtractionRunError(
            f"{rejected} vehicle candidate(s) were rejected; output was not published ({detail})",
            tuple(diagnostics),
        )
    return taxonomy, tuple(vehicles), tuple(diagnostics), rejected


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

    log.info(
        "startup: interval=%.1fs output=%s wiki=%s guides=%d",
        config.request_interval,
        config.output_path,
        config.wiki_endpoint,
        len(config.guide_paths),
    )
    # Loading before client construction guarantees a missing model cannot open sockets.
    nlp_model = sentence_nlp if sentence_nlp is not None else get_nlp()
    log.info("startup: spaCy model is available")
    owned_client = client is None
    http = client if client is not None else HttpClient(config)
    if http.config != config:
        raise ExtractionRunError("injected HTTP client uses different configuration")

    try:
        log.info("policy: checking robots.txt for required wiki and optional guides")
        policy_report = PolicyChecker(config, http, logger=log).check_sources()
        log.info(
            "policy: allowed %d guide(s), skipped %d",
            len(policy_report.allowed_guides),
            len(policy_report.skipped_guides),
        )
        guide_segments, guide_diagnostics, guide_count = _collect_guides(
            config, http, policy_report, log
        )
        taxonomy, vehicles, wiki_diagnostics, rejected = _collect_wiki(config, http, log)
        log.info(
            "assembly: merging %d tanks with %d guide segment(s)",
            len(vehicles),
            len(guide_segments),
        )
        assembly: AssemblyResult = assemble_records(
            vehicles,
            guide_segments,
            sentence_nlp=nlp_model,
            concept_extractor=ConceptExtractor(nlp_model),
        )
        log.info("output: validating and writing %s", config.output_path)
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
        log.info("%s", _diagnostic_text(diagnostic))
    log.info("output: published %s", config.output_path)

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
