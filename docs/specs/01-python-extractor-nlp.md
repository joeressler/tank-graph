# Milestone 1: Python Extractor and NLP

## Purpose

Produce a deterministic, schema-valid `data/tanks_data.json` from approved
Wargaming sources without overloading either host. This milestone owns all
network access, source parsing, text cleanup, tactical concept extraction, and
the Python side of the interchange contract.

## Dependencies

The Python package must declare:

- `requests` for HTTP.
- `mwparserfromhell` for MediaWiki markup.
- `beautifulsoup4` with the standard-library HTML parser or an explicitly
  declared parser.
- `spacy` and the `en_core_web_sm` model for English NLP.
- A Draft 2020-12 JSON Schema validator.
- `pytest` and request/clock test doubles as development dependencies.

Dependency installation must verify that `en_core_web_sm` is available before a
collection run. The extractor must not download a model implicitly while
processing sources.

## Module responsibilities

The implementation must include `extractor.py` as the orchestration module. It
coordinates focused modules rather than combining every concern in one class.

| Responsibility | Required boundary |
| --- | --- |
| Configuration | Endpoints, User-Agent, timeouts, output path, and guide allowlist |
| HTTP policy | Shared rate limiter, retries, response-size limits, and status handling |
| Wiki taxonomy | Recursive category traversal and candidate page discovery |
| Wiki parsing | Vehicle metadata and tactical-section extraction |
| Guide parsing | Allowlisted HTML fetching, content isolation, and scope mapping |
| Segmentation | Clean tactical statement production and deduplication |
| NLP | Per-segment candidate extraction and normalization |
| Assembly | Merge source records into one record per tank |
| Output | Schema/semantic validation and atomic deterministic JSON writing |

The exact module filenames beyond `extractor.py` may follow package conventions,
but these boundaries must remain independently testable.

## Configuration contract

Required production configuration:

| Setting | Requirement |
| --- | --- |
| MediaWiki endpoint | Defaults to `https://wiki.wargaming.net/api.php` |
| Guide root | Defaults to `https://worldoftanks.com/en/content/guide/` |
| Root category | Defaults to `Category:Tanks` |
| User-Agent | `WoTGraphBot/<version> (contact: joe.a.ressler+tankgraph@gmail.com)` |
| Minimum request interval | Fixed at no less than 5.0 seconds |
| Connect timeout | Finite and separately configurable |
| Read timeout | Finite and separately configurable |
| Maximum attempts | Five total attempts per request |
| Backoff | Exponential, at least 5 seconds, bounded at 15 seconds, with injectable jitter |
| Wiki cookies | Optional `WIKI_COOKIE` from the wiki host after `#mw-content-text` loads |
| Cookie refresh | Playwright exports wiki-host cookies only on a wait-page interstitial (`0` disables) |
| Consecutive interstitial abort | Abort after 10 consecutive JS cookie/anti-bot pages |
| Output | Defaults to `data/tanks_data.json` |

Tests may inject local endpoints, fake clocks, zero-duration test intervals, and
deterministic jitter. Production configuration must reject an interval below
5.0 seconds. The contact value must contain a real email address or maintained
project URL; examples and placeholder domains are invalid.

Only `http` and `https` are valid test endpoint schemes; production endpoints
must use `https`. Redirects are allowed only when every redirect remains on the
configured host. The client must impose a finite response-size limit before
parsing.

## Shared HTTP policy

One process-wide HTTP client and one limiter serve both hosts. Collection is
sequential in this milestone; no thread, task, connection pool, or retry may
bypass the limiter.

The 5.0-second interval is measured with a monotonic clock from completion of
one network attempt to the start of the next. Every attempt counts, including a
redirect request, retry, response with an error status, timeout, or connection
failure. Requests send `Connection: close` and at most one in-flight GET.

Wargaming wiki hosts may return HTTP 200 with a JS cookie/anti-bot interstitial
instead of MediaWiki. `requests` never executes that challenge. A 200 body is
blocked unless it is MediaWiki JSON (`{` or `[`) or article HTML
(`#mw-content-text` / `.mw-parser-output`). Blocked bodies are never parsed,
stored, or retried on the same URL over HTTP. Optional `WIKI_COOKIE` is the
full Cookie header from the wiki host document request after the article
loads; marketing-site `OptanonConsent` cookies and truncated values are
rejected. Playwright is not the crawler: it waits for `#mw-content-text`,
exports wiki-host cookies, and the HTTP client loads them into
`requests.Session`. Playwright runs only when HTTP receives a wait-page
interstitial, or when `--bootstrap-wiki-cookies` is used once at startup.
`--cookie-refresh-every 0` disables that recovery. Successful wiki requests
do not launch Chromium. A wait-page 200 refreshes cookies and retries that
URL once after a successful cookie export. A hard WAF page
(`Sorry, you have been blocked` / incident reference id) aborts immediately
and does not launch Playwright. Ten consecutive wait-page responses abort
the run.

Before taxonomy traversal, probe
`api.php?action=query&list=allpages&aplimit=1&format=json`. If that returns the
loading HTML, wiki-host cookies or a browser bootstrap are required. If it
returns JSON, continue discovery with `categorymembers`. Do not scrape HTML
`Special:AllPages`.

### Request pseudocode

```text
FUNCTION respectful_get(url, query):
    ASSERT url is allowed by configuration and source policy
    ASSERT request method is GET

    FOR attempt_number FROM 1 THROUGH 5:
        limiter.wait_until_at_least_5_seconds_after_previous_completion()

        TRY:
            response = session.GET(
                url,
                query,
                configured_user_agent,
                connection_close,
                wiki_host_cookie_if_configured,
                finite_connect_and_read_timeouts,
                streamed_response
            )
            response = read_no_more_than_configured_size(response)
        CATCH transient_connection_or_timeout_error AS error:
            limiter.record_attempt_completion(monotonic_now)
            recreate_session()
            IF attempt_number IS 5:
                RAISE terminal_network_error(url, attempt_number, error)
            sleep(retry_delay(attempt_number, no_retry_after))
            CONTINUE

        limiter.record_attempt_completion(monotonic_now)

        IF response.status IS 2xx AND is_bot_interstitial(body):
            RAISE blocked_interstitial_without_http_retry(url)

        IF response.status IS 2xx:
            RETURN response

        IF response.status IS 429 OR response.status IS BETWEEN 500 AND 599:
            IF attempt_number IS 5:
                RAISE retry_exhausted(url, response.status, attempt_number)
            delay = valid_retry_after(response.headers)
            IF delay IS missing:
                delay = bounded_exponential_backoff_at_least_5s_cap_15s(attempt_number)
            sleep(delay)
            CONTINUE

        RAISE non_retryable_http_error(url, response.status)
```

Waiting for backoff does not waive the limiter. The next attempt must satisfy
both delays. A valid `Retry-After` HTTP date or delta-seconds value is honored;
an invalid value is logged and ignored. Backoff jitter is bounded and
injectable so tests remain deterministic.

Logs contain the host, path, status, attempt number, and selected delay. Query
values, full response bodies, and contact configuration are not repeated in
error logs unless needed for a safe, redacted diagnostic.

## Wiki taxonomy traversal

### API request

Each category page is fetched with:

- `action=query`
- `format=json`
- `list=categorymembers`
- `cmtitle=<current category title>`
- `cmtype=page|subcat`
- `cmlimit=max`
- `cmcontinue=<token>` when returned

The crawler must consume every continuation token before moving to the next
queued category. It must not infer taxonomy from category URL strings.

### Traversal pseudocode

```text
FUNCTION crawl_taxonomy(root_category):
    category_queue = FIFO_QUEUE(root_category)
    visited_categories = EMPTY_SET
    discovered_pages = MAP_FROM_PAGE_ID_TO_PAGE_DESCRIPTOR
    parent_links = EMPTY_SET

    WHILE category_queue IS NOT EMPTY:
        current = category_queue.pop_front()
        current_key = normalized_mediawiki_title(current)

        IF current_key IS IN visited_categories:
            CONTINUE
        ADD current_key TO visited_categories

        continuation = NONE
        REPEAT:
            payload = fetch_category_members(current, continuation)
            validate_mediawiki_response(payload)

            FOR member IN payload.members:
                IF member IS subcategory:
                    child_key = normalized_mediawiki_title(member.title)
                    ADD (child_key, current_key) TO parent_links
                    IF child_key IS NOT IN visited_categories:
                        category_queue.push_back(member.title)
                ELSE IF member IS candidate page:
                    UPSERT member BY stable page_id INTO discovered_pages

            continuation = payload.next_cmcontinue
        UNTIL continuation IS NONE

    RETURN taxonomy(visited_categories, parent_links, discovered_pages)
```

MediaWiki category graphs may contain cycles and a page may occur in several
categories. Category identity uses normalized full title; page identity uses
page ID, falling back to normalized full title only when an ID is unavailable.
The final page-processing order is normalized title then page ID.

The traversal records all category paths needed to derive primary classes and
subclasses. The initial canonical mapping is:

| Normalized wiki value or category leaf | Output |
| --- | --- |
| `light tank`, `light tanks`, `light`, `lt`, `lighttank` | Primary class `Light Tanks` |
| `medium tank`, `medium tanks`, `medium`, `mt`, `mediumtank` | Primary class `Medium Tanks` |
| `heavy tank`, `heavy tanks`, `heavy`, `ht`, `heavytank` | Primary class `Heavy Tanks` |
| `tank destroyer`, `tank destroyers`, `td`, `ttd`, `at-spg`, `atspg` | Primary class `Tank Destroyers` |
| `spg`, `spgs`, `artillery`, `self-propelled gun`, `self-propelled guns` | Primary class `SPGs` |
| `autoloader`, `autoloaders` | Subclass `Autoloaders` |

The canonical primary-class set is exactly `Light Tanks`, `Medium Tanks`,
`Heavy Tanks`, `Tank Destroyers`, and `SPGs`. A value outside that set is not a
primary class.

`Tanks by nation`, `Tanks by tier`, `Tanks by type`, `Premium tanks`, `Removed
tanks`, `TankTree`, `Unintroduced tanks`, `Upcoming tanks`, and their structural
parents are organizational categories, not subclasses. Nation and tier
children provide metadata evidence only. No other category becomes a subclass
until its mapping is added to this table with fixtures, preventing arbitrary
site organization from leaking into the domain model.

## Wiki vehicle extraction

### Revision request

For each candidate vehicle page, request:

- `action=query`
- `format=json`
- `prop=revisions|pageprops|categories`
- `rvprop=content`
- `cllimit=max`
- `redirects=1`
- `titles=<full page title>`

The deployed wiki uses a legacy MediaWiki revision shape. The parser must read
the revision text from the legacy `revisions[0]["*"]` field and may support a
modern slot shape for fixtures or future compatibility. It must not send
`rvslots=main` unless endpoint capability detection proves that parameter is
supported.

Missing pages, API-level errors, redirect loops, absent revisions, and an
unrecognized revision shape are terminal extraction diagnostics.

### Template selection and metadata

Parse the revision with `mwparserfromhell`. Template names are compared after
trimming, replacing underscores with spaces, and case folding. `Vehicle` is the
required logical infobox. The accepted normalized template-name aliases are
`vehicle`, `tankdata`, and `tank data`. Any additional alias requires a spec
update and fixture before use. A page with multiple competing vehicle
infoboxes is rejected rather than resolved by position.

The initial normalized parameter aliases are:

| Logical value | Accepted parameter names in precedence order |
| --- | --- |
| Internal identifier | `id`, `internal name`, `tank id`, `tank` |
| Display name | `display name`, `title`, `name` |
| Primary class | `class`, `type` |
| Nation | `nation`, `country` |
| Tier | `tier`, `level` |

Underscores and whitespace are normalized when comparing parameter names. If
several aliases for one logical value are present with different cleaned
values, the page is ambiguous and rejected.

Required normalized output values:

| Output | Resolution rule |
| --- | --- |
| `name` | Stable infobox identifier when valid; otherwise the `Tank:` page title suffix with spaces normalized to underscores |
| `display_name` | Explicit display-name field, then display-title page property, then cleaned title suffix |
| `class` | Reconciled canonical infobox class/type and taxonomy evidence using the algorithm below |
| `subclasses` | Allowed taxonomy categories associated with this page, excluding the primary class |
| `nation` | Canonicalized infobox nation; if omitted, a unique nation category such as `Category:USA Tanks` |
| `tier` | Base-10 integer from the infobox, in the range 1 through 10; if omitted, a unique `Category:Tier I Tanks` (or decimal equivalent) |

Template parameter values are stripped of comments and wiki markup before
validation. The deployed wiki `TankData` infobox often omits nation, class, and
tier. Those values are applied by the template as MediaWiki categories, which
do not appear as `[[Category:...]]` wikilinks in the revision text. The
revision query therefore also requests `categories`. Those API categories, any
revision wikilinks, and taxonomy membership are eligible fallbacks when the
matching infobox field is empty.
An unrecognized non-empty infobox value is still a conflict and is not eligible
for category fallback. A required value that remains ambiguous or empty rejects
that candidate. A page in `Category:Tank articles requiring maintenance` that
still lacks nation, class, or tier is an incomplete stub: exclude it with
`incomplete_vehicle_page` and do not count it as a rejected vehicle. The
extractor reports every rejected candidate, including diagnostic codes.
Accepted vehicles are still validated and published. The run fails without
replacing output only when no vehicle is accepted, or when schema or semantic
validation fails.

### Primary-class reconciliation

Derive a set of canonical primary classes from every category path associated
with the page, then reconcile it with the canonicalized infobox value:

```text
FUNCTION resolve_primary_class(infobox_value, taxonomy_class_set):
    infobox_class = canonicalize_primary_class(infobox_value)
    taxonomy_classes = unique_canonical_primary_classes(taxonomy_class_set)

    IF taxonomy_classes.count > 1:
        REJECT page as contradictory taxonomy evidence

    IF infobox_class EXISTS AND taxonomy_classes.count IS 1:
        IF infobox_class EQUALS the only taxonomy class:
            RETURN infobox_class
        REJECT page as infobox/taxonomy class conflict

    IF infobox_class EXISTS AND taxonomy_classes IS empty:
        REPORT missing taxonomy evidence
        RETURN infobox_class

    IF infobox_class IS absent AND taxonomy_classes.count IS 1:
        REPORT infobox class fallback
        RETURN the only taxonomy class

    REJECT page as missing primary-class evidence
```

An unrecognized non-empty infobox class is treated as a conflict, not as an
absent value eligible for taxonomy fallback. This prevents source drift from
being hidden by category evidence.

### Tactical section extraction

Section titles are normalized by removing markup, trimming, collapsing
whitespace, and case folding. `Performance` and `Tactics` are accepted. A page
may contain either or both. When those headings are absent, the live `TankData`
parameters `InTheGame_performance` and `InTheGame_tactics` are accepted as the
same logical sections.

For an accepted section:

1. Exclude the heading itself and stop at the next heading of the same or
   higher level.
2. Remove templates that are navigation, maintenance, media, or citation-only.
3. Preserve visible link labels and meaningful list text.
4. Remove tables, image/file references, HTML comments, reference bodies, and
   category declarations.
5. Convert remaining markup to plain text with whitespace collapsed.
6. Preserve paragraph and list-item boundaries for segmentation.

An absent tactical section is valid and produces no wiki strategies. A present
section that becomes empty after cleanup emits a layout/content diagnostic but
does not fabricate text.

## Official guide scraping

### Allowlist

The collector fetches only configured URLs whose normalized paths are beneath
the guide root. The production allowlist is:

- `newcomers-guide/getting_started/`

Tank Coach video-guide routes are not collected. Tests may still exercise those
layouts when they are explicitly configured.

Following links is not a general crawl. A child page is fetched only when its
path is under the guide root, matches the configured Tank Coach path prefix,
is discovered by the index parser, and is already present in the configured
allowlist. Discovery does not authorize a new route. URL fragments and tracking
query parameters are discarded for identity and requests.

### Content isolation

BeautifulSoup must parse the HTML and identify a semantic content root using a
tested selector set headed by `main` or `article`, followed by documented
site-specific content containers. Before text extraction, remove navigation,
footer, header, scripts, styles, forms, dialogs, cookie controls, share
controls, breadcrumbs, and unrelated recommendation blocks.

Within the content root, retain:

- Relevant `h1` through `h4` headings.
- Paragraphs and list items under headings about vehicle types, battlefield
  roles, survival, aiming, equipment, consumables, crew capabilities, or Tank
  Coach tactics.
- Useful text alternatives or captions for tactical media when present.

Do not treat menus, legal text, login prompts, region selectors, or video
transcripts outside the approved content block as strategy content.

### Layout-drift checks

A guide response is rejected with a source-specific error when any of these
conditions holds:

- Expected title and content root are absent.
- The content root has no relevant heading and no qualifying text block.
- Extracted content is implausibly short compared with the fixture-defined
  minimum.
- The page resolves outside the approved host/path.
- The page is an access-denied, login, challenge, or generic error document.

Falling back to all `body` text is prohibited because it can silently pollute
the graph.

### Guide scope mapping

Every retained guide segment receives internal scope metadata:

- `applicable_classes`, a non-empty set of canonical primary classes. A
  universally applicable battlefield tactic uses all five canonical classes.
  A class-specific heading uses only the classes it explicitly addresses.
- `tactical_concepts`, a possibly empty set of canonical annotations such as
  equipment use, view range, hull-down positioning, or sidescraping.

Concept annotations guide keyword normalization but do not independently decide
which tanks receive a segment. A concept-only segment with no defensible class
scope is retained in extraction diagnostics and is not assembled into tank
records.

Scope matching is exact:

```text
FUNCTION scope_matches(guide_segment, tank_metadata):
    RETURN tank_metadata.primary_class
           IS IN guide_segment.applicable_classes
```

Onboarding, economy, store, event, and account-management text is not tactical
and is excluded. A segment with no deterministic class scope is retained only
in diagnostics, not assigned to every tank.

## Strategy segmentation and assembly

Segmentation is source-aware but yields one common strategy representation.

### Segment normalization

1. Normalize Unicode to NFKC.
2. Replace non-semantic line breaks and repeated whitespace with one space.
3. Split list items as separate candidates.
4. Split prose into sentences with spaCy sentence boundaries.
5. Join fragments only when the source markup proves they are one sentence.
6. Remove citation markers, empty bullets, standalone headings, and fragments
   without tactical meaning.
7. Retain original capitalization for user-facing strategy text.
8. Require a complete, bounded statement no longer than the schema maximum.
9. Deduplicate by the shared canonical identity key from the project overview.

When duplicate advice occurs, source precedence is wiki vehicle-specific text,
class-specific official guidance, then universal official tactical guidance.
The retained display text comes from the highest-precedence source; all
provenance remains available in internal diagnostics.

### Record assembly pseudocode

```text
FUNCTION assemble_records(taxonomy, wiki_pages, guide_segments):
    records_by_name = EMPTY_MAP

    FOR page IN wiki_pages ORDERED BY normalized_title:
        metadata = extract_vehicle_metadata(page, taxonomy)
        wiki_segments = extract_and_segment_tactical_sections(page)
        applicable_guides = guide_segments
                            WHERE scope_matches(guide_segment, metadata)

        ordered_segments = deduplicate_with_source_precedence(
            wiki_segments,
            applicable_guides
        )

        segment_keywords = EMPTY_MAP
        FOR segment IN ordered_segments:
            segment_keywords[segment] = extract_normalized_concepts(segment)

        record = {
            name: metadata.name,
            display_name: metadata.display_name,
            class: metadata.primary_class,
            subclasses: sort_unique(metadata.subclasses),
            nation: metadata.nation,
            tier: metadata.tier,
            strategies: ordered_segment_text_sorted_by_shared_order,
            keywords: sorted_union_by_shared_order(segment_keywords.values)
        }

        IF record.name ALREADY EXISTS:
            RAISE duplicate_tank_identity(record.name, both_source_pages)
        records_by_name[record.name] = record

    RETURN records_by_name.values SORTED BY normalized_name
```

The version-one interchange schema stores a record-level keyword union rather
than a per-strategy map. Per-segment associations must still be computed in
Python for testability and diagnostics. Milestone 3 defines the deliberate
record-level graph mapping used after this association is flattened.

## NLP concept extraction

Load `en_core_web_sm` exactly once per process. Process segments in bounded
batches while preserving their source order.

### Candidate sources

For every segment, candidates are the union of:

- spaCy named entities that represent a meaningful game concept.
- spaCy noun chunks after removal of determiners and stop-word boundaries.
- Individual noun/proper-noun lemmas when they carry tactical meaning.
- Matches from a small, version-controlled tactical alias map needed for domain
  expressions the general model may split or miss.

The alias map canonicalizes, at minimum:

| Variant | Canonical keyword |
| --- | --- |
| `side scrape`, `side-scrape`, `side scraping` | `sidescraping` |
| `hull down`, `hull-down position` | `hull-down` |
| `view-range`, `vision range` | `view range` |
| `long range sniping`, `snipe` | `sniping` |

The map supplements the model; it does not assign a keyword absent from the
segment.

### Normalization pseudocode

```text
FUNCTION extract_normalized_concepts(segment):
    document = spacy_model(segment)
    candidates = entities(document)
               UNION noun_phrases(document)
               UNION tactical_noun_lemmas(document)
               UNION alias_matches(segment)

    normalized = EMPTY_SET
    FOR candidate IN candidates:
        tokens = lemmatize(candidate.tokens)
        tokens = remove_space_punctuation_and_stop_words(tokens)
        phrase = unicode_normalize_and_casefold(tokens)
        phrase = apply_tactical_alias_map(phrase)
        phrase = normalize_hyphens_and_internal_spaces(phrase)

        IF phrase IS empty:
            CONTINUE
        IF phrase IS only numeric:
            CONTINUE
        IF phrase IS a generic term such as "tank", "vehicle", or "game":
            CONTINUE
        IF phrase exceeds schema limits:
            CONTINUE

        ADD phrase TO normalized

    RETURN normalized SORTED BY shared_canonical_order
```

Tests must prove that inflections collapse to one lemma, stop words are
removed, domain compounds remain meaningful, and the canonical examples
`sniping`, `accuracy`, `hull armor`, `sidescraping`, and `view range` are stable.

## Validation and output

Before writing:

1. Validate the entire array against
   [`tanks-data.schema.json`](tanks-data.schema.json).
2. Enforce normalized uniqueness for tank names and every repeated-value array.
3. Verify deterministic record and array ordering.
4. Verify that an empty `strategies` array implies an empty `keywords` array.
5. Reject control characters, non-finite numeric values, unknown fields, and
   metadata outside canonical mappings.
6. Serialize as UTF-8 JSON with stable formatting and a final newline.

Write to a temporary file in the destination directory, flush it, close it,
and atomically replace the destination only after all validation succeeds.
Failure must leave any prior valid dataset untouched. Temporary files are
removed on handled failure.

## Error behavior

| Condition | Behavior |
| --- | --- |
| Invalid startup configuration | Fail before the first request |
| Policy disallows an optional guide route | Skip and report it; do not attempt a workaround |
| Policy disallows a required source or all guide routes | Fail the run and do not publish replacement output |
| Redirect leaves the approved host/path | Fail the request with safe URL context |
| `429` or `5xx` | Retry according to the bounded policy |
| Other `4xx` | Fail immediately |
| Transient timeout/connection failure | Retry according to the bounded policy |
| Malformed API/HTML response | Fail with source and parser-stage context |
| Non-vehicle category page | Exclude with a classified diagnostic |
| Candidate vehicle missing required metadata | Accumulate rejection; publish accepted vehicles; fail without replacement output only when none are accepted |
| Maintenance stub missing nation, class, or tier | Exclude as `incomplete_vehicle_page`; do not count it as a rejection |
| Missing tactical sections | Accept the tank with empty tactical arrays if no guide applies |
| Schema or semantic validation failure | Do not replace existing output |

The final run summary reports category count, candidate page count, accepted
tank count, rejected candidate count, guide page count, strategy count,
keyword count, retry count, and output path.

## Test matrix

| Area | Required cases |
| --- | --- |
| Limiter | First call immediate; subsequent calls at least 5.0 seconds after prior completion; retries and both hosts share the interval |
| Retry | `429`, every `5xx` family fixture, `Retry-After`, timeout, cap 15s, exhaustion, and fatal `4xx` |
| Interstitial | Spinner/blocked HTML vs MediaWiki HTML/JSON; no HTTP retry of the spinner; abort after consecutive blocks; wiki-host cookies only; Playwright only on wait-page or explicit bootstrap |
| Categories | Pagination, duplicate pages, cycles, repeated subcategories, deterministic ordering |
| Revisions | Legacy content shape, modern compatibility shape, redirect, missing page, missing revision, API error |
| Templates | Parameter order, whitespace/case aliases, wiki markup, missing field, ambiguous multiple infoboxes |
| Sections | Performance only, Tactics only, both, nested headings, markup removal, absent section |
| Guides | Each allowlisted layout, child-link filtering, removed boilerplate, layout drift, off-host redirect |
| Segmentation | Lists, paragraphs, abbreviations, duplicates across sources, precedence |
| NLP | Lemmas, stop words, noun chunks, entities, aliases, generic-term filter, deterministic order |
| Output | Schema pass/fail, normalized duplicates, atomic replacement, stable repeated-run bytes |

All network tests use recorded minimal fixtures or mocked responses. Clock,
sleep, random jitter, filesystem destination, and spaCy loading are injectable.
At least one integration test runs the complete extractor pipeline from local
fixtures to an in-memory or temporary JSON destination.

## Deliverables

- Installable Python package rooted under `python/`.
- `extractor.py` orchestration module and focused supporting modules.
- Documented non-interactive extraction entry point.
- Recorded minimal source fixtures and automated tests.
- Schema-valid `data/tanks_data.json` from an explicitly invoked live run.

The generated production dataset is an output, not a hand-edited source file.

## Exit criteria

- [ ] A missing or placeholder bot contact fails before network access.
- [ ] Every sequential network attempt is separated from the previous
      completion by at least 5.0 seconds in production configuration.
- [ ] `429` and `5xx` retries are bounded, honor valid `Retry-After`, and cannot
      bypass the shared limiter.
- [ ] `Category:Tanks` traversal handles continuation and cycles and produces
      unique, deterministically ordered candidates.
- [ ] Vehicle metadata and Performance/Tactics text parse from recorded wiki
      fixtures.
- [ ] Approved official guide fixtures produce only core tactical content and
      layout drift fails visibly.
- [ ] spaCy and tactical aliases produce deterministic normalized concepts.
- [ ] The complete output validates against the shared schema and semantic
      invariants.
- [ ] A failed run cannot corrupt or replace the last valid dataset.
- [ ] The full automated suite passes with network access disabled.
