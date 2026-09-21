# Tank Graph Agent Instructions

These instructions apply to the entire repository.

## Sources of truth

1. Read [README.md](README.md) and the
   [project overview](docs/specs/00-project-overview.md) before changing code.
2. Implement milestone specifications in numeric order:
   [01](docs/specs/01-python-extractor-nlp.md),
   [02](docs/specs/02-rust-data-ingestion.md),
   [03](docs/specs/03-graph-construction.md), then
   [04](docs/specs/04-query-engine-benchmarks.md).
3. The overview controls shared architecture. A milestone spec controls its
   local details. The [JSON Schema](docs/specs/tanks-data.schema.json) controls
   the Python-to-Rust wire format.

## Working rules

- Reuse and extend existing modules before creating new abstractions or files.
- Work only within the requested milestone; do not implement later features.
- Satisfy every milestone exit criterion before advancing.
- Keep changes complete, focused, readable, and free of placeholders or TODOs.
- Preserve documented field names, enum names, edge directions, endpoints,
  normalization, deterministic ordering, and error behavior.
- Add or update tests with every behavior change. Unit and CI tests must not
  require live network access.
- Comments must explain purpose or constraints, not restate the code.

## Architecture boundaries

- Python owns HTTP access, source parsing, segmentation, NLP, validation, and
  deterministic `tanks_data.json` generation.
- All Python network traffic uses the shared limiter, descriptive User-Agent,
  source-policy checks, and retry rules from Milestone 1.
- JSON is the only Python/Rust boundary and remains one schema-valid root array.
- Rust owns bounded ingestion, semantic validation, directed graph
  construction, indexed traversal, stable CLI output, and benchmarks.
- Rust must not scrape, infer missing source data, silently repair invalid
  input, or panic on user-controlled data.

## Safety and verification

- Never bypass access controls, `robots.txt`, rate limits, path restrictions, or
  configured graph/input budgets.
- Keep credentials outside tracked files and redact secrets and response bodies.
- Validate generated data against the schema and semantic invariants.
- Run the narrow relevant tests first, then the completed milestone suite.
- Before handoff, verify formatting/lints, deterministic output, documented
  examples, and all applicable exit criteria.
