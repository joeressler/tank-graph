# Milestone 2: Rust Data Model and Ingestion

## Purpose

Create a Cargo binary package that safely converts the Milestone 1 JSON array
into validated Rust records. This milestone stops at validated in-memory data;
it does not construct the graph or implement user queries.

## Package boundary

The package lives under `rust/` and is named `tank-graph`. It is a binary
package with a library boundary so ingestion and later graph behavior can be
tested without spawning a process.

Expected responsibilities:

```text
rust/
├── Cargo.toml
├── src/
│   ├── main.rs       command entry point
│   ├── lib.rs        reusable package surface
│   ├── model.rs      interchange and graph type definitions
│   └── ingest.rs     bounded file loading and semantic validation
└── tests/
    └── ingestion.rs  public-boundary integration tests
```

This is a target layout, not a direction to create placeholders before their
behavior is implemented and tested.

## Dependency contract

Runtime dependencies must include:

- `petgraph` for the graph types used beginning in Milestone 3.
- `serde` with derive support for the interchange records.
- `serde_json` for streaming JSON deserialization.
- A Unicode normalization library capable of NFKC normalization.

Milestone 4 may add a maintained argument parser. Benchmark-only dependencies
must be development dependencies. Dependency versions must use compatible,
maintained releases available when implementation begins; the lockfile records
the exact resolved build.

The implementation must not disable `serde_json`'s recursion protection or use
unsafe code to accelerate ingestion.

## Interchange record model

`TankRecord` is a deserialization-only structure with these exact fields:

| JSON field | Rust value | Rules |
| --- | --- | --- |
| `name` | UTF-8 string | Stable unique internal identity |
| `display_name` | UTF-8 string | Human-readable label |
| `class` | UTF-8 string mapped to a non-keyword member name | Canonical primary class |
| `subclasses` | list of UTF-8 strings | Sorted unique narrower classes |
| `nation` | UTF-8 string | Canonical source label |
| `tier` | unsigned 8-bit integer | 1 through 10 |
| `strategies` | list of UTF-8 strings | Sorted unique clean statements |
| `keywords` | list of UTF-8 strings | Sorted unique normalized concepts |

Deserialization denies unknown object fields. Every required field must be
present; no field receives an implicit default. JSON `null`, a numeric string
for `tier`, non-integer numbers, duplicate object keys, and non-array roots are
errors.

The reserved JSON field `class` is mapped explicitly to an unambiguous Rust
member such as `class_name` while preserving the wire name.

## Graph type contract

Milestone 2 defines, but does not yet instantiate, these logical enum variants:

### `NodeType`

- `Tank` with `name`, `display_name`, `nation`, and `tier`.
- `Class` with one canonical class or subclass string.
- `Strategy` with one strategy string.
- `Keyword` with one normalized keyword string.

### `EdgeType`

- `IsSubclassOf`
- `IsAClassOf`
- `RecommendedTactic`
- `AssociatedWithKeyword`

`NodeType` must support equality and hashing because Milestone 3 uses it as a
`HashMap` key. Equality is variant-sensitive: a `Class("sniping")` is not equal
to a `Keyword("sniping")`. Tank equality includes all Tank fields after
ingestion has already guaranteed unique `name` values, so conflicting duplicate
tank records are rejected rather than merged.

`EdgeType` must support equality so graph construction can prevent duplicate
typed edges.

## Input limits

The loader treats the file path and bytes as untrusted input.

| Limit | Required behavior |
| --- | --- |
| File type | Must resolve to a readable regular file |
| File size | Default hard limit of 256 MiB |
| Root records | At most 10,000 |
| Subclasses per tank | At most 100 |
| Strategies per tank | At most 1,000 |
| Keywords per tank | At most 1,000 |
| Label length | At most 120 Unicode scalar values |
| Strategy length | At most 1,000 Unicode scalar values |
| JSON recursion | Keep the parser's safe default limit |

The loader must enforce the byte limit through the reader as well as checking
metadata so a file that changes during reading cannot bypass the limit.
Allocation sizes must come from validated bounds, not untrusted capacity hints.

The caller supplies the path explicitly. The loader does not search parent
directories, follow a path embedded in the JSON, or open source URLs.

## Buffered loading pseudocode

```text
FUNCTION load_dataset(input_path, limits):
    IF input_path IS missing:
        RETURN PathNotFound(input_path)

    file = open_for_read_only(input_path)
        OR RETURN OpenFailed(input_path, operating_system_error)

    IF file IS NOT a regular file:
        RETURN NotRegularFile(input_path)

    IF metadata_size(file) > limits.maximum_bytes:
        RETURN FileTooLarge(input_path, metadata_size, maximum_bytes)

    bounded_reader = byte_limit_reader(file, limits.maximum_bytes + 1)
    buffered_reader = buffered(bounded_reader)

    records = deserialize_one_complete_json_array(buffered_reader)
        OR RETURN JsonSyntax(error_line, error_column, safe_message)

    IF bounded_reader consumed more than limits.maximum_bytes:
        RETURN FileTooLarge(input_path, observed_bytes, maximum_bytes)

    validate_dataset(records, limits)
        OR RETURN SemanticValidation(list_of_contextual_violations)

    RETURN ValidatedDataset(records)
```

Deserialization must consume the complete stream. Non-whitespace trailing
content is an error. A UTF-8 byte-order mark, invalid UTF-8, or multiple JSON
documents is rejected rather than normalized silently.

`serde_json::from_reader` over a `BufReader` is the intended baseline. The
resulting `Vec<TankRecord>` is allowed because the graph is explicitly
in-memory and the bounded file/record limits control allocation.

## Semantic validation

JSON Schema validates the producer. Rust independently enforces the same
contract so a hand-edited or third-party file cannot bypass it.

### Canonical text rules

For every string:

1. It must be non-empty after trimming.
2. It must already equal its trimmed form.
3. It must already be NFKC-normalized.
4. It must not contain disallowed control characters.
5. Its Unicode scalar length must not exceed the field limit.

Names must match the schema's stable-identifier grammar. Keywords must be
lowercase canonical phrases with internal whitespace collapsed and no line
breaks or tabs. Strategies may contain ordinary sentence punctuation but no
markup.

Normalized identity and deterministic ordering use the exact shared algorithm
from the project overview: NFKC, trimmed/collapsed Unicode whitespace,
canonical ASCII hyphens, full case folding, and an original-value tie-breaker
for sorting. No other punctuation is removed. The key is used only to detect
violations; the loader does not rewrite user input.

### Record rules

- `tier` is between 1 and 10 inclusive.
- The primary class is one of `Light Tanks`, `Medium Tanks`, `Heavy Tanks`,
  `Tank Destroyers`, or `SPGs`.
- `subclasses` does not contain any canonical primary-class label under
  normalized identity.
- Each repeated-value list is strictly ordered by the shared canonical sort key
  and has no normalized duplicates.
- An empty strategy list requires an empty keyword list.
- Per-strategy keyword attribution is a producer-side invariant that the flat
  version-one wire format cannot prove. Rust must not claim to validate more
  than the enforceable empty-strategy relationship above.

### Dataset rules

- Every `name` is unique under normalized identity.
- Input records are strictly ordered by the shared canonical sort key for
  `name`.
- An identical repeated tank is still an error; the loader never chooses one.
- Display names need not be globally unique because variants may share a
  player-facing name.
- An empty root array is syntactically valid but produces a warning-level
  diagnostic for development and is not accepted as a successful production
  extraction artifact.

### Validation pseudocode

```text
FUNCTION validate_dataset(records, limits):
    violations = EMPTY_LIST
    seen_tank_names = EMPTY_MAP
    previous_tank_sort_key = NONE

    IF records.count > limits.maximum_records:
        ADD dataset_limit_violation TO violations

    FOR record_number, record IN records:
        path = "$[" + record_number + "]"
        validate_all_scalar_fields(record, path, violations)
        validate_tier(record.tier, path + ".tier", violations)
        validate_primary_class(record.class, path + ".class", violations)
        validate_bounded_sorted_unique_list(
            record.subclasses,
            path + ".subclasses",
            limits
        )
        validate_bounded_sorted_unique_list(
            record.strategies,
            path + ".strategies",
            limits
        )
        validate_bounded_sorted_unique_list(
            record.keywords,
            path + ".keywords",
            limits
        )

        IF record.strategies IS empty AND record.keywords IS NOT empty:
            ADD orphan_keyword_violation(path) TO violations

        tank_identity = normalized_identity(record.name)
        tank_sort_key = canonical_sort_key(record.name)
        IF tank_identity IN seen_tank_names:
            ADD duplicate_tank(path, seen_tank_names[tank_identity]) TO violations
        ELSE:
            seen_tank_names[tank_identity] = path

        IF previous_tank_sort_key EXISTS
           AND tank_sort_key <= previous_tank_sort_key:
            ADD non_deterministic_order(path) TO violations
        previous_tank_sort_key = tank_sort_key

    IF violations IS NOT empty:
        RETURN all violations in deterministic path order
    RETURN success
```

Validation should collect independent semantic problems in one bounded error
report rather than forcing a user to repair one field per run. The report must
cap displayed violations while retaining the total count to avoid
attacker-controlled output volume.

## Error contract

The ingestion API returns a typed error category with safe context:

| Category | Context |
| --- | --- |
| Path not found | Supplied path |
| Open/read failure | Path and operating-system error |
| Not a regular file | Path |
| File too large | Limit and observed size |
| JSON syntax/type error | Line, column, and parser message |
| Semantic validation | JSON-style field paths and bounded violation list |

Library paths must not terminate the process, call `unwrap`, or call `expect`
for user-controlled conditions. The binary entry point owns human formatting
and exit status. Error messages must not echo the full dataset or unrelated
file contents.

## Test matrix

| Area | Required cases |
| --- | --- |
| Happy path | Tiger I example loads with exact field values |
| Root syntax | Empty root, object root, scalar root, two documents, trailing garbage |
| Fields | Missing, unknown, null, wrong type, duplicate key |
| Numbers | Tier zero, tier eleven, negative, decimal, numeric string, integer overflow |
| Text | Empty, whitespace-only, leading/trailing space, control character, invalid UTF-8, non-NFKC |
| Ordering | Unsorted records and each unsorted repeated-value list |
| Identity | Exact and normalized duplicate tank names and list members |
| Bounds | Oversized file, too many records/items, overlong labels/strategies |
| Relationships | Any primary-class label used as a subclass; keywords without strategies |
| I/O | Missing path, directory path, unreadable file, file changing past byte limit |
| Resilience | Every invalid fixture returns an error and never panics |

A fixture generated from the JSON Schema example must deserialize successfully.
Malformed fixtures are deliberately minimal so each test proves one contract.
Property/fuzz testing is recommended for the deserializer boundary, but it does
not replace the listed deterministic tests.

## Deliverables

- `rust/Cargo.toml` for the `tank-graph` binary package.
- Reusable model and ingestion library modules.
- A minimal binary entry point capable of loading a supplied dataset and
  reporting success or a typed failure.
- Unit and integration tests covering the matrix above.

## Exit criteria

- [ ] Cargo builds with `petgraph`, `serde`, and `serde_json` declared.
- [ ] The Rust record shape matches every required schema field and denies
      unknown fields.
- [ ] A buffered, byte-bounded reader loads the valid fixture.
- [ ] Invalid syntax, types, ordering, bounds, and semantic relationships
      return contextual typed errors.
- [ ] Duplicate tank names are rejected under normalized identity.
- [ ] No user-controlled fixture causes a panic or unbounded diagnostic.
- [ ] The implementation performs no network access and does not silently
      modify invalid input.
- [ ] All Milestone 2 tests pass before graph construction begins.
