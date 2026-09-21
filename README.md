# Tank Graph

Tank Graph is a specification-first hybrid knowledge graph pipeline for World of
Tanks data. Python is responsible for respectful source collection, parsing, and
NLP enrichment. Rust is responsible for validated ingestion, directed graph
construction, queries, and benchmarks.

This repository currently contains specifications only. It intentionally does
not contain executable source code, package manifests, fixtures, generated
datasets, or benchmark results.

## Canonical specifications

Read and implement the specifications in this order:

1. [Project overview](docs/specs/00-project-overview.md)
2. [Python extractor and NLP](docs/specs/01-python-extractor-nlp.md)
3. [Rust data ingestion](docs/specs/02-rust-data-ingestion.md)
4. [Graph construction](docs/specs/03-graph-construction.md)
5. [Query engine and benchmarks](docs/specs/04-query-engine-benchmarks.md)

The machine-readable boundary between Python and Rust is the
[tank data JSON Schema](docs/specs/tanks-data.schema.json).

## Milestone order

Each milestone must satisfy every exit criterion in its specification before
work begins on the next milestone. Later milestones may depend only on the
published contracts of earlier milestones, not on undocumented implementation
details.

## Source endpoints

- Wargaming MediaWiki API: `https://wiki.wargaming.net/api.php`
- Official guide root: `https://worldoftanks.com/en/content/guide/`

All live collection must identify the client, obey source policies, and pass
through the shared rate limiter and retry policy defined by Milestone 1.
