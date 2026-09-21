# Milestone 4: Query Engine and Benchmarks

## Purpose

Expose deterministic, non-interactive class and keyword queries over the
Milestone 3 graph, then measure warmed query-only traversal separately from
loading, validation, and graph construction.

The command-line interface is the supported user boundary. Query logic remains
in the library so correctness and performance tests do not need to spawn the
binary.

## Command-line contract

Use a maintained Rust argument parser with generated, layered help. No command
may prompt, open a menu, require timed input, or silently choose a data file.

### Query commands

```text
tank-graph --data <path> --format <text|json> query class --name <class>
tank-graph --data <path> --format <text|json> query keyword --name <keyword>
```

Examples shown in `--help`:

```text
tank-graph --data data/tanks_data.json --format text query class --name "Heavy Tanks"
tank-graph --data data/tanks_data.json --format json query keyword --name "sidescraping"
```

`--data`, `--format`, the query kind, and `--name` are required. Missing or
invalid arguments fail immediately with a correct example invocation. Empty or
whitespace-only names are usage errors.

Top-level help lists the `query` and `benchmark` command families only.
`query --help`, `query class --help`, and `query keyword --help` provide local
options and examples without printing the complete manual.

### Benchmark commands

```text
tank-graph --data <path> --format <text|json> benchmark class \
    --name <class> --warmup <count> --iterations <count> \
    [--max-p95-us <microseconds>]

tank-graph --data <path> --format <text|json> benchmark keyword \
    --name <keyword> --warmup <count> --iterations <count> \
    [--max-p95-us <microseconds>]
```

Defaults are 1,000 warm-up iterations and 10,000 measured iterations. Warm-up
count must be between 1 and 1,000,000; measured iteration count must be between
1 and 10,000,000. The optional threshold makes a controlled performance-gate
run exit unsuccessfully when p95 exceeds the supplied value.

The benchmark command is read-only and safe to rerun. No dry-run or
confirmation flag is necessary.

## Exit statuses

| Status | Meaning |
| --- | --- |
| `0` | Query or benchmark completed successfully |
| `2` | Command-line usage error |
| `3` | Dataset open, parse, or semantic validation failure |
| `4` | Requested class or keyword does not exist |
| `5` | Graph preflight or construction failure |
| `6` | Graph/query invariant failure |
| `7` | Explicit benchmark threshold not met |

Errors go to standard error; successful data goes to standard output. JSON mode
uses a stable JSON error object once `--format json` has been parsed. Parser
errors that occur before output-format resolution may use the argument parser's
plain diagnostic.

An absent query node is an error, not a successful empty result. An existing
node with no matching tanks is a successful empty result and indicates a graph
state worth reporting.

## Query normalization

Normalize the supplied query using the same identity rules as ingestion:

1. NFKC-normalize.
2. Trim outer whitespace and collapse internal Unicode-whitespace runs.
3. Convert Unicode hyphen variants to ASCII `-`.
4. Apply Unicode full case folding.
5. Preserve all other punctuation.

Query normalization is tolerant; stored node values remain strict and are not
modified. Keyword queries do not invent semantic synonyms. For example,
capitalization and redundant spaces may normalize, but `flanking` does not
implicitly mean `mobility`.

The class and keyword lookup maps in `GraphBundle` resolve the normalized key
in constant expected time. A missing value error includes the normalized query,
the query kind, and a valid example command. It may include a bounded,
deterministically sorted list of available exact values but must not dump the
entire graph.

## Query result model

Each matching tank result contains:

- `name`
- `display_name`
- `nation`
- `tier`
- `matched_strategies`

`matched_strategies` is empty for class queries. For keyword queries it contains
the sorted unique Strategy values that formed a valid path from that tank to
the requested Keyword.

Results are unique by Tank `NodeIndex`, then sorted by normalized
`display_name`, normalized `name`, nation, and tier. Graph node indexes and
insertion order are never exposed as stable public identifiers.

## Class traversal

Class queries are exact membership queries. They intentionally do not traverse
`IsSubclassOf`.

```text
FUNCTION query_class(graph_bundle, supplied_class):
    key = normalize_class_query(supplied_class)
    class_index = graph_bundle.class_lookup.get(key)
        OR RETURN QueryValueNotFound("class", supplied_class, key)

    ASSERT graph[class_index] IS Class
    matched_tanks = EMPTY_MAP_FROM_NODE_INDEX_TO_RESULT

    FOR edge IN graph.edges_directed(class_index, Incoming):
        IF edge.type IS NOT IsAClassOf:
            CONTINUE

        source_index = edge.source
        source_node = graph[source_index]
        IF source_node IS NOT Tank:
            RETURN GraphInvariantFailure(edge)

        matched_tanks.insert_if_absent(
            source_index,
            tank_result(source_node, empty_matched_strategies)
        )

    RETURN values(matched_tanks) SORTED BY public_result_order
```

This returns tanks directly assigned to `Heavy Tanks`, and a query for
`Autoloaders` returns tanks directly tagged with that subclass. It does not use
the subclass-to-parent edge to infer transitive primary membership.

## Keyword traversal

Keyword queries perform two incoming hops:

```text
Keyword <- AssociatedWithKeyword - Strategy
Strategy <- RecommendedTactic - Tank
```

### Traversal pseudocode

```text
FUNCTION query_keyword(graph_bundle, supplied_keyword):
    key = normalize_keyword_query(supplied_keyword)
    keyword_index = graph_bundle.keyword_lookup.get(key)
        OR RETURN QueryValueNotFound("keyword", supplied_keyword, key)

    ASSERT graph[keyword_index] IS Keyword
    matched_tanks = EMPTY_MAP_FROM_NODE_INDEX_TO_MUTABLE_RESULT

    FOR keyword_edge IN graph.edges_directed(keyword_index, Incoming):
        IF keyword_edge.type IS NOT AssociatedWithKeyword:
            CONTINUE

        strategy_index = keyword_edge.source
        strategy_node = graph[strategy_index]
        IF strategy_node IS NOT Strategy:
            RETURN GraphInvariantFailure(keyword_edge)

        FOR tactic_edge IN graph.edges_directed(strategy_index, Incoming):
            IF tactic_edge.type IS NOT RecommendedTactic:
                CONTINUE

            tank_index = tactic_edge.source
            tank_node = graph[tank_index]
            IF tank_node IS NOT Tank:
                RETURN GraphInvariantFailure(tactic_edge)

            result = matched_tanks.get_or_insert(
                tank_index,
                tank_result(tank_node, empty_matched_strategies)
            )
            result.matched_strategies.add(strategy_node.text)

    FOR result IN matched_tanks:
        sort result.matched_strategies BY normalized_strategy_order

    RETURN values(matched_tanks) SORTED BY public_result_order
```

The result map deduplicates tanks reached through several strategies. Traversal
checks edge types and endpoint variants instead of assuming every neighboring
node is valid.

### Complexity

Let `C` be incoming class-membership edges for a class. Class query work is
`O(C + R log R)`, where `R` is result count.

Let `S` be strategies attached to a keyword and `T` be the total incoming tank
recommendations across those strategies. Keyword query work is
`O(S + T + R log R + M log M)`, where `M` covers per-result matched-strategy
sorting. Lookup before traversal is expected `O(1)`.

## Successful output

### JSON

JSON output is one object:

```json
{
  "query": {
    "kind": "keyword",
    "value": "sidescraping",
    "normalized_value": "sidescraping"
  },
  "result_count": 1,
  "results": [
    {
      "name": "G04_PzVI_Tiger_I",
      "display_name": "Tiger I",
      "nation": "Germany",
      "tier": 7,
      "matched_strategies": [
        "Hull armor is flat and easily penetrated; rely on sidescraping configurations."
      ]
    }
  ],
  "timing_us": {
    "load": 0,
    "build": 0,
    "query": 0,
    "total": 0
  }
}
```

The zero values above describe shape only. Real commands populate them from
measured durations. Each value is an unsigned integer representing the
duration rounded up to the next whole microsecond so a completed sub-microsecond
phase is not reported as zero. `total` covers the three measured phases and
small orchestration overhead, so it may be greater than their sum.

The JSON schema of CLI output must be covered by serialization tests. No
decorative text, progress message, or ANSI escape sequence may appear on
standard output in JSON mode.

### Text

Text output starts with the normalized query and result count, prints one
stable line or block per tank, and ends with separate `load_us`, `build_us`,
`query_us`, and `total_us` fields. Color is disabled when output is not a
terminal and is never required to understand the result.

## Error output

JSON errors use:

```json
{
  "error": {
    "code": "query_value_not_found",
    "message": "No keyword node matched the supplied value.",
    "query_kind": "keyword",
    "value": "unknown tactic",
    "normalized_value": "unknown tactic"
  }
}
```

Error codes are stable snake-case identifiers. Human messages are concise and
actionable. Data and graph errors preserve the typed categories from prior
milestones and never include full source records.

## Timing boundaries

Use a monotonic high-resolution clock.

| Phase | Starts | Ends |
| --- | --- | --- |
| Load | Immediately before opening the data file | After deserialization and semantic validation |
| Build | Immediately before preflight | After graph/index construction and invariant validation |
| Query | Immediately before lookup normalization/lookup | After deduplication and deterministic result sorting |
| Format | Not part of query time | Serialization/printing may be reported separately in diagnostics |

The ordinary query command reports one execution and is useful for
observability, not statistical performance claims.

## Benchmark methodology

The automated benchmark measures the library query function on an already
built graph.

1. Build in Cargo `--release` mode.
2. Load and construct the graph once.
3. Resolve and validate the benchmark target.
4. Run the selected query for the warm-up count; discard those durations.
5. Run the query for the measured count.
6. Prevent compiler elimination by consuming/black-boxing the complete result.
7. Include lookup, traversal, deduplication, matched-strategy collection, and
   sorting in every measured sample.
8. Exclude argument parsing, file I/O, JSON parsing, graph construction,
   formatting, and terminal output.
9. Sort sample durations and compute minimum, median, p95 by nearest-rank, and
   maximum.
10. Report nanosecond sample statistics converted to microseconds with enough
    precision to avoid zero or false accuracy.

The report also includes application version, build profile, query kind/value,
warm-up and measured counts, result count, graph node/edge counts, and available
platform identifiers. Benchmark output is deterministic in structure even
though measurements vary.

A development benchmark harness may use Criterion for regression analysis.
The CLI benchmark command remains the acceptance interface because it reports
the required p95 and can apply an explicit threshold.

## Performance gate

The deterministic acceptance fixture is
`rust/tests/fixtures/benchmark_tanks_data.json`. It must contain the canonical
class `Heavy Tanks`, the canonical keyword `sidescraping`, and enough linked
records to exercise result deduplication and sorting.

- The `Heavy Tanks` class query and `sidescraping` keyword query each run for at
  least 1,000 warm-ups and 10,000 measured iterations.
- Query-only p95 must be less than `1,000 µs`.
- The command is invoked with `--max-p95-us 1000` on a controlled release-mode
  runner.
- Exceeding the threshold exits with status `7` and still prints the measured
  report.

This is the milestone's "microsecond processing" gate: measured traversal is
below one millisecond and is reported in microseconds. It is not a universal
latency guarantee.

Full production data must also be benchmarked and reported, but it has no hard
CI threshold until a named dataset version and stable reference runner exist.
Load/build times are never compared to the query-only target.

## Correctness test matrix

| Area | Required cases |
| --- | --- |
| Argument parsing | Missing data/format/name, unknown command, invalid format, empty name, examples in help |
| Class lookup | Case/space normalization, primary class, subclass tag, unknown class, exact non-transitive behavior |
| Keyword lookup | Case/space normalization, one path, several strategies, several tanks, unknown keyword |
| Deduplication | One tank reached by several strategies appears once with all matched strategies |
| Ordering | Stable tank and matched-strategy order independent of graph insertion order |
| Invariants | Wrong endpoint variant on either traversal hop returns status-category 6 |
| Output | Golden text shape, JSON serialization shape, stdout/stderr separation, no ANSI in JSON |
| Exit status | Every documented error category maps to the specified status |
| Timing | Phase boundaries use fake clock; nonzero durations round up correctly |
| Benchmark | Warm-ups excluded, sample count exact, nearest-rank p95 correct, threshold pass/fail |

An end-to-end integration test loads a fixture JSON file, constructs the graph,
runs both query kinds, and checks complete sorted results. Performance tests run
in release mode on a controlled runner and remain separate from debug-mode
correctness tests.

## Deliverables

- Library query module for exact class and two-hop keyword traversal.
- Non-interactive `query class` and `query keyword` CLI commands.
- Stable text/JSON output and typed exit-status mapping.
- `benchmark class` and `benchmark keyword` commands with median/p95 reporting.
- Automated correctness tests and controlled release-mode performance gate.
- Recorded full-dataset benchmark report with environment and graph size.

## Exit criteria

- [ ] Every required CLI input is expressible as a flag and no command prompts.
- [ ] Layered help contains valid class, keyword, and benchmark examples.
- [ ] Class lookup uses the index and incoming `IsAClassOf` edges without
      transitive subclass expansion.
- [ ] Keyword lookup performs the typed two-hop traversal and deduplicates
      tanks and matched strategies.
- [ ] Results and errors are deterministic and machine-readable in JSON mode.
- [ ] Load, build, and query durations are measured and reported separately.
- [ ] The benchmark excludes startup work, uses warm-ups, and reports median
      and p95 in microseconds.
- [ ] Representative fixture queries meet p95 `<1,000 µs` in the controlled
      release-mode gate.
- [ ] Full-dataset results are reported without claiming a hardware-independent
      threshold.
- [ ] All Milestone 4 correctness and performance acceptance tests pass.
