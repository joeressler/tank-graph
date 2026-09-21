# Milestone 3: Graph Construction

## Purpose

Convert a validated `Vec<TankRecord>` into a deterministic, directed,
in-memory `petgraph` adjacency-list graph. Repeated classes, strategies, and
keywords become shared nodes; repeated typed edges are not inserted.

This milestone consumes only the validated Rust model from Milestone 2. It
does not read JSON directly, perform NLP, or repair invalid records.

## Graph selection

Use a directed `petgraph` graph equivalent to:

```text
Directed adjacency-list graph<NodeType, EdgeType>
```

Direction carries domain meaning and permits efficient incoming and outgoing
traversal:

| Source node | Edge | Target node | Meaning |
| --- | --- | --- | --- |
| Tank | `IsAClassOf` | Primary Class | The tank's principal class |
| Tank | `IsAClassOf` | Subclass | The tank has the narrower category/tag |
| Subclass | `IsSubclassOf` | Primary Class | This dataset observed that subclass under that class |
| Tank | `RecommendedTactic` | Strategy | The strategy is recommended for the tank |
| Strategy | `AssociatedWithKeyword` | Keyword | The strategy context is associated with the concept |

The graph is a directed multigraph internally, but the builder enforces at most
one edge for an identical `(source, edge type, target)` triple.

`Graph`/`DiGraph` is preferred over `StableGraph` because the initial engine
does not remove nodes and benefits from compact indexes. Node removal must not
be added without revisiting index stability.

## Edge semantics and query consequences

Primary classes and subclasses share the `Class(String)` node variant.
`IsAClassOf` is intentionally directed from Tank to Class despite the legacy
edge name.

A subclass may have several observed primary-class parents. For example,
`Autoloaders` may occur under both heavy and medium tanks. Therefore
`IsSubclassOf` forms a directed taxonomy graph, not a strict tree. Exact class
queries follow incoming `IsAClassOf` edges only; they do not automatically
expand through `IsSubclassOf`, which would over-include cross-cutting tags.

The version-one JSON contract stores a tank-level keyword union, not a
per-strategy map. The required graph representation applies every keyword in a
record to every strategy in that same record. This is a deliberate
record-context association:

```text
FOR EACH strategy in one tank record:
    FOR EACH keyword in that same tank record:
        Strategy -> AssociatedWithKeyword -> Keyword
```

If a strategy node is shared by several tanks, its graph keyword set becomes
the union of those records' keywords. Consequently, a keyword query means
"tanks connected through a strategy context associated with this keyword,"
not proof that the exact keyword was mined from the exact shared sentence.
Changing to exact per-sentence provenance requires a future versioned
interchange schema and is outside this milestone.

## Construction result

The builder returns one immutable-by-convention bundle containing:

- The directed graph.
- The canonical `HashMap<NodeType, NodeIndex>` uniqueness cache.
- Read-only lookup indexes derived from the same nodes for class and keyword
  query keys.
- Construction statistics: input records, nodes by variant, edges by type,
  duplicate edges suppressed, and build duration when measured by the caller.

The uniqueness cache is retained with the graph so queries never depend on
reconstructing `NodeType` values by scanning all nodes.

## Capacity and denial-of-service limits

Schema-valid input can still imply a large Cartesian set of
strategy-to-keyword edges. Before allocation, calculate an upper bound with
checked integer arithmetic.

For a record with `C` subclasses, `S` strategies, and `K` keywords:

```text
maximum new relationship attempts = 1 + (2 × C) + S + (S × K)
```

The terms are primary-class membership, two subclass relationships, strategy
recommendations, and record-context keyword associations.

Default graph budgets:

| Resource | Maximum |
| --- | --- |
| Nodes | 2,000,000 |
| Unique edges | 5,000,000 |
| Relationship attempts before deduplication | 5,000,000 |

The builder rejects a dataset whose checked estimate overflows or exceeds a
node, edge, or relationship-attempt budget. It does so before allocating the
graph. This check is intentionally conservative because duplicate relationship
attempts may later collapse to fewer unique edges. Budgets may be lowered by a
caller for tests, but production must not silently raise them based on input.

Use the estimates to reserve reasonable capacity. Do not reserve the full
untrusted estimate if it exceeds a budget.

## Identity and uniqueness

Milestone 2 has already validated canonical strings, so graph identity uses
exact `NodeType` equality:

- A Tank node is unique by its complete validated Tank variant. Dataset-level
  name uniqueness prevents conflicting duplicates.
- A Class node is unique by canonical class/subclass text.
- A Strategy node is unique by exact normalized display sentence.
- A Keyword node is unique by canonical keyword text.
- Variants never compare equal to one another.

The single `HashMap<NodeType, NodeIndex>` is authoritative. Auxiliary class and
keyword lookup maps store normalized query key to an existing index and must
never create independent nodes.

### Node insertion pseudocode

```text
FUNCTION get_or_insert_node(graph, node_cache, node_value):
    IF node_value EXISTS IN node_cache:
        RETURN node_cache[node_value]

    index = graph.add_node(clone(node_value))
    node_cache.insert(node_value, index)
    RETURN index
```

Every node insertion, including Tank insertion, goes through this function.
After the build, cache size must equal graph node count and every cached index
must point to an equal node value.

## Edge uniqueness

Maintain a construction-only set keyed by source index, edge variant, and
target index.

```text
FUNCTION add_unique_edge(graph, edge_cache, source, edge_type, target):
    key = (source, edge_type, target)
    IF key EXISTS IN edge_cache:
        increment duplicate_edge_suppression_count
        RETURN

    graph.add_edge(source, target, edge_type)
    edge_cache.insert(key)
```

Checking only whether any edge exists between two nodes is insufficient because
different edge variants have different meanings. Searching adjacency lists for
each insertion is also prohibited because it makes dense builds unnecessarily
quadratic.

## Build pipeline

Construction happens in local state. A failure returns an error and drops the
partial graph; callers never observe a half-built bundle.

### Preflight pseudocode

```text
FUNCTION estimate_graph(records, budgets):
    node_attempts = 0
    relationship_attempts = 0

    FOR record IN records:
        C = record.subclasses.count
        S = record.strategies.count
        K = record.keywords.count

        node_attempts = checked_add(
            node_attempts,
            1 tank + 1 primary_class + C subclasses + S strategies + K keywords
        )
        relationship_attempts = checked_add(
            relationship_attempts,
            1 + checked_multiply(2, C) + S + checked_multiply(S, K)
        )

        IF node_attempts > budgets.maximum_nodes:
            RETURN NodeBudgetExceeded
        IF relationship_attempts > budgets.maximum_relationship_attempts:
            RETURN RelationshipBudgetExceeded
        IF relationship_attempts > budgets.maximum_edges:
            RETURN EdgeBudgetExceeded

    RETURN safe_capacity_hints(node_attempts, relationship_attempts)
```

Node attempts are an upper bound because shared nodes deduplicate. Edge attempts
are an upper bound on unique edges, are checked against both relevant budgets
before allocation, and are checked again during construction as a defensive
invariant.

### Record construction pseudocode

```text
FUNCTION build_graph(validated_dataset, budgets):
    capacities = estimate_graph(validated_dataset.records, budgets)
    graph = directed_graph_with_capacity(capacities)
    node_cache = EMPTY HashMap<NodeType, NodeIndex>
    edge_cache = EMPTY HashSet<(NodeIndex, EdgeType, NodeIndex)>
    statistics = zeroed_statistics

    FOR record IN validated_dataset.records IN validated_order:
        tank_node = Tank(
            record.name,
            record.display_name,
            record.nation,
            record.tier
        )
        tank_index = get_or_insert_node(graph, node_cache, tank_node)

        primary_node = Class(record.class)
        primary_index = get_or_insert_node(graph, node_cache, primary_node)
        add_unique_edge(
            graph,
            edge_cache,
            tank_index,
            IsAClassOf,
            primary_index
        )

        FOR subclass IN record.subclasses IN validated_order:
            subclass_index = get_or_insert_node(
                graph,
                node_cache,
                Class(subclass)
            )
            add_unique_edge(
                graph,
                edge_cache,
                tank_index,
                IsAClassOf,
                subclass_index
            )
            add_unique_edge(
                graph,
                edge_cache,
                subclass_index,
                IsSubclassOf,
                primary_index
            )

        keyword_indexes = EMPTY_LIST
        FOR keyword IN record.keywords IN validated_order:
            keyword_index = get_or_insert_node(
                graph,
                node_cache,
                Keyword(keyword)
            )
            keyword_indexes.push(keyword_index)

        FOR strategy IN record.strategies IN validated_order:
            strategy_index = get_or_insert_node(
                graph,
                node_cache,
                Strategy(strategy)
            )
            add_unique_edge(
                graph,
                edge_cache,
                tank_index,
                RecommendedTactic,
                strategy_index
            )

            FOR keyword_index IN keyword_indexes:
                add_unique_edge(
                    graph,
                    edge_cache,
                    strategy_index,
                    AssociatedWithKeyword,
                    keyword_index
                )

        IF graph.node_count > budgets.maximum_nodes:
            RETURN NodeBudgetExceeded
        IF graph.edge_count > budgets.maximum_edges:
            RETURN EdgeBudgetExceeded

    class_lookup, keyword_lookup = derive_query_indexes(node_cache)
    validate_graph_invariants(graph, node_cache, edge_cache, lookup_indexes)

    RETURN GraphBundle(
        graph,
        node_cache,
        class_lookup,
        keyword_lookup,
        statistics
    )
```

The validated input order plus deterministic list order makes first insertion
and resulting `NodeIndex` values reproducible for identical input and
dependency versions. External output must still sort by domain values rather
than expose node indexes as stable IDs.

## Graph invariants

After construction:

1. Every graph node has exactly one equal entry in the node cache.
2. Every cached index is in bounds and points to its cache key.
3. Every `IsAClassOf` edge starts at Tank and ends at Class.
4. Every `IsSubclassOf` edge starts at Class and ends at Class; its source is
   not a canonical primary-class label.
5. Every `RecommendedTactic` edge starts at Tank and ends at Strategy.
6. Every `AssociatedWithKeyword` edge starts at Strategy and ends at Keyword.
7. No duplicate typed edge triple exists.
8. Every Strategy has at least one incoming `RecommendedTactic` edge.
9. Every Keyword has at least one incoming `AssociatedWithKeyword` edge.
10. Every Tank has exactly one outgoing `IsAClassOf` edge to its primary class
    and zero or more to subclasses.
11. Lookup maps point only to nodes of their promised variant.
12. Actual node and edge counts do not exceed configured budgets.

The distinction between a primary Class and a subclass Class is derived from
the validated set of canonical primary labels and edge context; it is not a
separate enum variant.

## Failure contract

Construction returns typed errors for:

- Checked-arithmetic overflow during preflight.
- Node, unique-edge, or relationship-attempt budget exceeded.
- An impossible cache mismatch or wrong endpoint variant found by invariant
  validation.
- A lookup-key collision that maps one normalized key to unequal canonical
  nodes.

Invariant errors indicate an implementation defect, but they still return an
error at the library boundary rather than panicking in normal operation. The
caller reports counts and context without dumping the graph.

## Test fixtures and expected counts

### Single-record fan-out fixture

One tank, one primary class, no subclasses, two unique strategies, and two
unique keywords must produce:

- 6 nodes: 1 Tank + 1 Class + 2 Strategy + 2 Keyword.
- 7 edges: 1 class + 2 tactic + 4 strategy-keyword.

This fixture proves the documented record-context Cartesian association.

### Subclass fixture

One tank, one primary class, one subclass, one strategy, and one keyword must
produce:

- 5 nodes: 1 Tank + 2 Class + 1 Strategy + 1 Keyword.
- 5 edges: primary membership + subclass membership + subclass parent +
  tactic + keyword association.

### Shared-entity fixture

Two tanks in the same primary class, each with one private strategy and one
identical shared strategy, and both with the same keyword must produce:

- 7 nodes: 2 Tank + 1 Class + 3 Strategy + 1 Keyword.
- 9 edges: 2 class + 4 tactic + 3 unique strategy-keyword.

This fixture proves that Class, Strategy, and Keyword nodes deduplicate and that
the repeated shared-strategy-to-keyword edge is suppressed.

### Additional required tests

| Area | Required cases |
| --- | --- |
| Preflight | Checked overflow, node budget, edge-attempt budget, exact-boundary success |
| Nodes | Shared class, subclass, strategy, and keyword; variant-sensitive identity |
| Edges | Duplicate suppression and same endpoints with different legal variants where applicable |
| Taxonomy | One subclass with several observed primary parents; no automatic tree assumption |
| Empty tactics | Tank and class nodes build with no strategy or keyword nodes |
| Determinism | Identical input produces equal node values, edge triples, and counts |
| Invariants | Purpose-built invalid internal graphs are detected in module tests |
| Safety | Any returned error drops partial state and never exposes a bundle |

## Deliverables

- Graph construction module using a directed `petgraph` adjacency list.
- Graph bundle containing the graph and canonical node/query indexes.
- Preflight capacity calculation and enforced graph budgets.
- Invariant validator and construction statistics.
- Unit and integration tests with exact node/edge assertions.

## Exit criteria

- [ ] Every insertion uses the canonical `HashMap<NodeType, NodeIndex>`.
- [ ] Shared Class, Strategy, and Keyword entities produce one node each.
- [ ] Typed duplicate edges are suppressed in constant expected time.
- [ ] Edge directions and endpoint variants match the table in this spec.
- [ ] Primary-class queries can remain exact despite cross-cutting subclasses.
- [ ] Strategy-keyword fan-out matches the version-one flattened data contract.
- [ ] Oversized or overflowing graph estimates fail before unsafe allocation.
- [ ] The three count fixtures produce exactly the specified node and edge
      totals.
- [ ] Graph invariant checks and all Milestone 3 tests pass.
