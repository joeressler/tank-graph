# Tank Graph

Tank Graph is a specification-first hybrid knowledge graph pipeline for World of
Tanks data. Python is responsible for respectful source collection, parsing, and
NLP enrichment. Rust is responsible for validated ingestion, directed graph
construction, queries, and benchmarks.

Milestone 1 provides the Python extractor. Later Rust milestones remain
specifications until they are implemented in order.

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

Every production request attempt shares one limiter and starts at least five
seconds after the previous attempt completed. Collection can therefore take a
long time. The default official-guide allowlist is the newcomer getting-started
page; Tank Coach video pages are omitted because they are not prose sources.
The destination is replaced atomically only after every candidate has been
processed and the complete root array passes schema and semantic validation.

Use `py -m tank_graph_extractor --help` and
`py -m tank_graph_extractor extract --help` for all configurable endpoints,
timeouts, limits, and diagnostic options.
