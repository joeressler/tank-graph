# Tank Graph

Tank Graph is a specification-first hybrid knowledge graph pipeline for World of
Tanks data. Python is responsible for respectful source collection, parsing, and
NLP enrichment. Rust is responsible for validated ingestion, directed graph
construction, queries, and benchmarks.

Milestone 1 provides the Python extractor. Milestone 2 provides validated Rust
ingestion. Milestone 3 builds the directed in-memory graph. Milestone 4
exposes class and keyword queries plus query-only benchmarks.

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

## Python extractor

Python 3.11 or newer is required. Create and activate a virtual environment,
then install the package and its development tools:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -e ".\python[dev,model]"
```

The `model` extra is an explicit model installation step. A collection run
verifies that `en_core_web_sm` is available but never downloads it.

Run the offline test suite:

```powershell
py -m pytest python
```

Run a production collection non-interactively:

```powershell
py -m tank_graph_extractor extract `
  --contact "joe.a.ressler+tankgraph@gmail.com" `
  --output data/tanks_data.json
```

The equivalent installed command is:

```powershell
tank-graph-extract extract --contact "joe.a.ressler+tankgraph@gmail.com" --output data/tanks_data.json
```

Live collection currently starts at `Category:USA Tanks` and keeps only USA
vehicles so a smaller schema-valid `data/tanks_data.json` can feed Rust
ingestion. Restore the full tree with `--root-category Category:Tanks --nation ALL`.

Every production request attempt shares one limiter and starts at least five
seconds after the previous attempt completed. Collection can therefore take a
long time. The destination is replaced atomically only after every candidate
has been processed and the complete root array passes schema and semantic
validation.

Live wiki collection should stay slow and single-client. Use `--request-interval 10`
(or higher) so each attempt waits longer than the 5.0s floor. Do not run two
extracts at once, do not retry a 403, and stop if the page says
"Sorry, you have been blocked." A wait spinner is not a hard block; that
block page is. Wait until a normal browser can load `#mw-content-text` before
starting another extract.

Wargaming wikis may return HTTP 200 JS cookie/anti-bot HTML instead of
MediaWiki. Capture the full Cookie header from the `wiki.wargaming.net` (or
`wiki.worldoftanks.com`) document request after `#mw-content-text` loads, then
pass it as `WIKI_COOKIE` or `--wiki-cookie`. Marketing-site `OptanonConsent`
cookies will not pass the gate. HTTP remains the crawler. Playwright opens
only when that HTTP response is a wait-page interstitial (or when you pass
`--bootstrap-wiki-cookies` once at startup). `--cookie-refresh-every 0`
disables Playwright recovery entirely. Install the optional extra and Chromium
first:

```powershell
py -m pip install -e ".\python[playwright]"
py -m playwright install chromium
```

`--bootstrap-wiki-cookies` still exports an initial cookie if you want one
before the first request.

Use `py -m tank_graph_extractor --help` and
`py -m tank_graph_extractor extract --help` for all configurable endpoints,
timeouts, limits, and diagnostic options.

## Rust graph engine

Rust 1.86 or newer is required. From `rust/`:

```powershell
cargo test
```

Query a dataset. `--data` and `--format` are required:

```powershell
cargo run -- --data ..\data\tanks_data.json --format text query class --name "Heavy Tanks"
cargo run -- --data ..\data\tanks_data.json --format json query keyword --name "sidescraping"
```

Benchmark query-only time after the graph is built. Defaults are 1,000 warm-ups
and 10,000 measured iterations:

```powershell
cargo run --release -- --data ..\data\tanks_data.json --format json benchmark class --name "Heavy Tanks" --warmup 1000 --iterations 10000
```

The controlled p95 gate uses the small fixture, not production data:

```powershell
cargo test --release --test performance
```

