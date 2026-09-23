//! Deterministic directed graph construction from validated tank records.

use crate::identity::identity_key;
use crate::ingest::{is_primary_class, ValidatedDataset};
use crate::model::{EdgeType, NodeType, TankGraph, TankRecord};
use petgraph::graph::NodeIndex;
use petgraph::visit::EdgeRef;
use petgraph::Direction;
use std::collections::{HashMap, HashSet};
use std::fmt;

type LookupMap = HashMap<String, NodeIndex>;

/// Caller-supplied graph resource budgets. Production defaults match the spec.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GraphBudgets {
    pub maximum_nodes: usize,
    pub maximum_edges: usize,
    pub maximum_relationship_attempts: usize,
}

impl Default for GraphBudgets {
    fn default() -> Self {
        Self {
            maximum_nodes: 2_000_000,
            maximum_edges: 5_000_000,
            maximum_relationship_attempts: 5_000_000,
        }
    }
}

/// Counts collected from a successful build. Duration is measured by the caller.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GraphStatistics {
    pub input_records: usize,
    pub tank_nodes: usize,
    pub class_nodes: usize,
    pub strategy_nodes: usize,
    pub keyword_nodes: usize,
    pub is_a_class_of_edges: usize,
    pub is_subclass_of_edges: usize,
    pub recommended_tactic_edges: usize,
    pub associated_with_keyword_edges: usize,
    pub duplicate_edges_suppressed: usize,
}

/// Immutable-by-convention graph plus the indexes required for later queries.
#[derive(Debug)]
pub struct GraphBundle {
    pub graph: TankGraph,
    pub node_cache: HashMap<NodeType, NodeIndex>,
    pub class_lookup: LookupMap,
    pub keyword_lookup: LookupMap,
    pub statistics: GraphStatistics,
}

/// Typed construction failure. Partial graphs are dropped with the builder.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GraphError {
    ArithmeticOverflow,
    NodeBudgetExceeded { attempted: usize, maximum: usize },
    EdgeBudgetExceeded { attempted: usize, maximum: usize },
    RelationshipBudgetExceeded { attempted: usize, maximum: usize },
    InvariantFailure { message: String },
    LookupKeyCollision { key: String },
}

impl fmt::Display for GraphError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ArithmeticOverflow => {
                write!(f, "graph estimate overflowed checked arithmetic")
            }
            Self::NodeBudgetExceeded { attempted, maximum } => {
                write!(f, "node budget exceeded ({attempted} > {maximum})")
            }
            Self::EdgeBudgetExceeded { attempted, maximum } => {
                write!(f, "edge budget exceeded ({attempted} > {maximum})")
            }
            Self::RelationshipBudgetExceeded { attempted, maximum } => write!(
                f,
                "relationship-attempt budget exceeded ({attempted} > {maximum})"
            ),
            Self::InvariantFailure { message } => {
                write!(f, "graph invariant failed: {message}")
            }
            Self::LookupKeyCollision { key } => {
                write!(f, "lookup key collision: {key}")
            }
        }
    }
}

impl std::error::Error for GraphError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct CapacityHints {
    node_attempts: usize,
    relationship_attempts: usize,
}

/// Convert validated records into a directed adjacency-list graph.
pub fn build_graph(
    dataset: &ValidatedDataset,
    budgets: &GraphBudgets,
) -> Result<GraphBundle, GraphError> {
    let capacities = estimate_graph(&dataset.records, budgets)?;
    let mut builder = Builder::new(capacities, budgets);
    for record in &dataset.records {
        builder.ingest_record(record)?;
        builder.check_live_budgets()?;
    }

    let class_lookup;
    let keyword_lookup;
    (class_lookup, keyword_lookup) = derive_query_indexes(&builder.node_cache)?;
    validate_graph_invariants(
        &builder.graph,
        &builder.node_cache,
        &class_lookup,
        &keyword_lookup,
        budgets,
    )?;

    let statistics = tally_statistics(
        &builder.graph,
        dataset.records.len(),
        builder.duplicate_edges_suppressed,
    );
    Ok(GraphBundle {
        graph: builder.graph,
        node_cache: builder.node_cache,
        class_lookup,
        keyword_lookup,
        statistics,
    })
}

fn estimate_graph(
    records: &[TankRecord],
    budgets: &GraphBudgets,
) -> Result<CapacityHints, GraphError> {
    let mut node_attempts = 0_usize;
    let mut relationship_attempts = 0_usize;

    for record in records {
        let added_nodes = node_attempts_for_record(
            record.subclasses.len(),
            record.strategies.len(),
            record.keywords.len(),
        )?;
        let added_relationships = relationship_attempts_for_record(
            record.subclasses.len(),
            record.strategies.len(),
            record.keywords.len(),
        )?;
        node_attempts = checked_add(node_attempts, added_nodes)?;
        relationship_attempts = checked_add(relationship_attempts, added_relationships)?;

        if node_attempts > budgets.maximum_nodes {
            return Err(GraphError::NodeBudgetExceeded {
                attempted: node_attempts,
                maximum: budgets.maximum_nodes,
            });
        }
        if relationship_attempts > budgets.maximum_relationship_attempts {
            return Err(GraphError::RelationshipBudgetExceeded {
                attempted: relationship_attempts,
                maximum: budgets.maximum_relationship_attempts,
            });
        }
        if relationship_attempts > budgets.maximum_edges {
            return Err(GraphError::EdgeBudgetExceeded {
                attempted: relationship_attempts,
                maximum: budgets.maximum_edges,
            });
        }
    }

    Ok(CapacityHints {
        node_attempts,
        relationship_attempts,
    })
}

fn node_attempts_for_record(
    subclass_count: usize,
    strategy_count: usize,
    keyword_count: usize,
) -> Result<usize, GraphError> {
    let with_subclasses = checked_add(2, subclass_count)?;
    let with_strategies = checked_add(with_subclasses, strategy_count)?;
    checked_add(with_strategies, keyword_count)
}

fn relationship_attempts_for_record(
    subclass_count: usize,
    strategy_count: usize,
    keyword_count: usize,
) -> Result<usize, GraphError> {
    let subclass_relationships = checked_mul(2, subclass_count)?;
    let keyword_associations = checked_mul(strategy_count, keyword_count)?;
    let with_subclasses = checked_add(1, subclass_relationships)?;
    let with_tactics = checked_add(with_subclasses, strategy_count)?;
    checked_add(with_tactics, keyword_associations)
}

fn checked_add(left: usize, right: usize) -> Result<usize, GraphError> {
    left.checked_add(right)
        .ok_or(GraphError::ArithmeticOverflow)
}

fn checked_mul(left: usize, right: usize) -> Result<usize, GraphError> {
    left.checked_mul(right)
        .ok_or(GraphError::ArithmeticOverflow)
}

struct Builder<'a> {
    graph: TankGraph,
    node_cache: HashMap<NodeType, NodeIndex>,
    edge_cache: HashSet<(NodeIndex, EdgeType, NodeIndex)>,
    duplicate_edges_suppressed: usize,
    budgets: &'a GraphBudgets,
}

impl<'a> Builder<'a> {
    fn new(capacities: CapacityHints, budgets: &'a GraphBudgets) -> Self {
        Self {
            graph: TankGraph::with_capacity(
                capacities.node_attempts,
                capacities.relationship_attempts,
            ),
            node_cache: HashMap::with_capacity(capacities.node_attempts),
            edge_cache: HashSet::with_capacity(capacities.relationship_attempts),
            duplicate_edges_suppressed: 0,
            budgets,
        }
    }

    fn ingest_record(&mut self, record: &TankRecord) -> Result<(), GraphError> {
        let tank_index = get_or_insert_node(
            &mut self.graph,
            &mut self.node_cache,
            NodeType::Tank {
                name: record.name.clone(),
                display_name: record.display_name.clone(),
                nation: record.nation.clone(),
                tier: record.tier,
            },
        );

        let primary_index = get_or_insert_node(
            &mut self.graph,
            &mut self.node_cache,
            NodeType::Class(record.class_name.clone()),
        );
        add_unique_edge(
            &mut self.graph,
            &mut self.edge_cache,
            tank_index,
            EdgeType::IsAClassOf,
            primary_index,
            &mut self.duplicate_edges_suppressed,
        );

        for subclass in &record.subclasses {
            let subclass_index = get_or_insert_node(
                &mut self.graph,
                &mut self.node_cache,
                NodeType::Class(subclass.clone()),
            );
            add_unique_edge(
                &mut self.graph,
                &mut self.edge_cache,
                tank_index,
                EdgeType::IsAClassOf,
                subclass_index,
                &mut self.duplicate_edges_suppressed,
            );
            add_unique_edge(
                &mut self.graph,
                &mut self.edge_cache,
                subclass_index,
                EdgeType::IsSubclassOf,
                primary_index,
                &mut self.duplicate_edges_suppressed,
            );
        }

        let mut keyword_indexes = Vec::with_capacity(record.keywords.len());
        for keyword in &record.keywords {
            keyword_indexes.push(get_or_insert_node(
                &mut self.graph,
                &mut self.node_cache,
                NodeType::Keyword(keyword.clone()),
            ));
        }

        for strategy in &record.strategies {
            let strategy_index = get_or_insert_node(
                &mut self.graph,
                &mut self.node_cache,
                NodeType::Strategy(strategy.clone()),
            );
            add_unique_edge(
                &mut self.graph,
                &mut self.edge_cache,
                tank_index,
                EdgeType::RecommendedTactic,
                strategy_index,
                &mut self.duplicate_edges_suppressed,
            );
            for keyword_index in &keyword_indexes {
                add_unique_edge(
                    &mut self.graph,
                    &mut self.edge_cache,
                    strategy_index,
                    EdgeType::AssociatedWithKeyword,
                    *keyword_index,
                    &mut self.duplicate_edges_suppressed,
                );
            }
        }

        Ok(())
    }

    fn check_live_budgets(&self) -> Result<(), GraphError> {
        let node_count = self.graph.node_count();
        if node_count > self.budgets.maximum_nodes {
            return Err(GraphError::NodeBudgetExceeded {
                attempted: node_count,
                maximum: self.budgets.maximum_nodes,
            });
        }
        let edge_count = self.graph.edge_count();
        if edge_count > self.budgets.maximum_edges {
            return Err(GraphError::EdgeBudgetExceeded {
                attempted: edge_count,
                maximum: self.budgets.maximum_edges,
            });
        }
        Ok(())
    }
}

fn get_or_insert_node(
    graph: &mut TankGraph,
    node_cache: &mut HashMap<NodeType, NodeIndex>,
    node_value: NodeType,
) -> NodeIndex {
    if let Some(&index) = node_cache.get(&node_value) {
        return index;
    }
    let index = graph.add_node(node_value.clone());
    node_cache.insert(node_value, index);
    index
}

fn add_unique_edge(
    graph: &mut TankGraph,
    edge_cache: &mut HashSet<(NodeIndex, EdgeType, NodeIndex)>,
    source: NodeIndex,
    edge_type: EdgeType,
    target: NodeIndex,
    duplicate_edges_suppressed: &mut usize,
) {
    if !edge_cache.insert((source, edge_type, target)) {
        *duplicate_edges_suppressed += 1;
        return;
    }
    graph.add_edge(source, target, edge_type);
}

fn derive_query_indexes(
    node_cache: &HashMap<NodeType, NodeIndex>,
) -> Result<(LookupMap, LookupMap), GraphError> {
    let mut class_lookup = HashMap::new();
    let mut keyword_lookup = HashMap::new();
    for (node, &index) in node_cache {
        match node {
            NodeType::Class(label) => {
                insert_lookup(&mut class_lookup, identity_key(label), index)?;
            }
            NodeType::Keyword(label) => {
                insert_lookup(&mut keyword_lookup, identity_key(label), index)?;
            }
            NodeType::Tank { .. } | NodeType::Strategy(_) => {}
        }
    }
    Ok((class_lookup, keyword_lookup))
}

fn insert_lookup(lookup: &mut LookupMap, key: String, index: NodeIndex) -> Result<(), GraphError> {
    if let Some(&existing) = lookup.get(&key) {
        if existing != index {
            return Err(GraphError::LookupKeyCollision { key });
        }
    } else {
        lookup.insert(key, index);
    }
    Ok(())
}

fn validate_graph_invariants(
    graph: &TankGraph,
    node_cache: &HashMap<NodeType, NodeIndex>,
    class_lookup: &LookupMap,
    keyword_lookup: &LookupMap,
    budgets: &GraphBudgets,
) -> Result<(), GraphError> {
    if node_cache.len() != graph.node_count() {
        return invariant("node cache size does not match graph node count");
    }

    for (node, &index) in node_cache {
        match graph.node_weight(index) {
            Some(stored) if stored == node => {}
            Some(_) => return invariant("cached index points to a different node"),
            None => return invariant("cached index is out of bounds"),
        }
    }

    for index in graph.node_indices() {
        let node = &graph[index];
        match node_cache.get(node) {
            Some(&cached) if cached == index => {}
            Some(_) => return invariant("graph node is cached under a different index"),
            None => return invariant("graph node is missing from the cache"),
        }
    }

    let mut seen_edges = HashSet::new();
    for edge in graph.edge_references() {
        let source = edge.source();
        let target = edge.target();
        let edge_type = *edge.weight();
        if !seen_edges.insert((source, edge_type, target)) {
            return invariant("duplicate typed edge triple");
        }
        validate_edge_endpoints(graph, source, edge_type, target)?;
    }

    for index in graph.node_indices() {
        match &graph[index] {
            NodeType::Tank { .. } => validate_tank_class_edges(graph, index)?,
            NodeType::Strategy(_) => {
                if !has_incoming(graph, index, EdgeType::RecommendedTactic) {
                    return invariant("strategy has no incoming RecommendedTactic edge");
                }
            }
            NodeType::Keyword(_) => {
                if !has_incoming(graph, index, EdgeType::AssociatedWithKeyword) {
                    return invariant("keyword has no incoming AssociatedWithKeyword edge");
                }
            }
            NodeType::Class(_) => {}
        }
    }

    validate_lookup_map(graph, class_lookup, |node| {
        matches!(node, NodeType::Class(_))
    })?;
    validate_lookup_map(graph, keyword_lookup, |node| {
        matches!(node, NodeType::Keyword(_))
    })?;

    if graph.node_count() > budgets.maximum_nodes {
        return Err(GraphError::NodeBudgetExceeded {
            attempted: graph.node_count(),
            maximum: budgets.maximum_nodes,
        });
    }
    if graph.edge_count() > budgets.maximum_edges {
        return Err(GraphError::EdgeBudgetExceeded {
            attempted: graph.edge_count(),
            maximum: budgets.maximum_edges,
        });
    }

    Ok(())
}

fn validate_edge_endpoints(
    graph: &TankGraph,
    source: NodeIndex,
    edge_type: EdgeType,
    target: NodeIndex,
) -> Result<(), GraphError> {
    let source_node = graph
        .node_weight(source)
        .ok_or_else(|| GraphError::InvariantFailure {
            message: "edge source is out of bounds".to_owned(),
        })?;
    let target_node = graph
        .node_weight(target)
        .ok_or_else(|| GraphError::InvariantFailure {
            message: "edge target is out of bounds".to_owned(),
        })?;

    match (edge_type, source_node, target_node) {
        (EdgeType::IsAClassOf, NodeType::Tank { .. }, NodeType::Class(_)) => Ok(()),
        (EdgeType::IsSubclassOf, NodeType::Class(label), NodeType::Class(_)) => {
            if is_primary_class(label) {
                invariant("IsSubclassOf starts at a primary class")
            } else {
                Ok(())
            }
        }
        (EdgeType::RecommendedTactic, NodeType::Tank { .. }, NodeType::Strategy(_)) => Ok(()),
        (EdgeType::AssociatedWithKeyword, NodeType::Strategy(_), NodeType::Keyword(_)) => Ok(()),
        _ => invariant("edge endpoints do not match the required variants"),
    }
}

fn validate_tank_class_edges(graph: &TankGraph, tank: NodeIndex) -> Result<(), GraphError> {
    let mut primary_count = 0_usize;
    for edge in graph.edges_directed(tank, Direction::Outgoing) {
        if *edge.weight() != EdgeType::IsAClassOf {
            continue;
        }
        match graph.node_weight(edge.target()) {
            Some(NodeType::Class(label)) if is_primary_class(label) => {
                primary_count =
                    checked_add(primary_count, 1).map_err(|_| GraphError::InvariantFailure {
                        message: "tank primary-class edge count overflowed".to_owned(),
                    })?;
            }
            Some(NodeType::Class(_)) => {}
            _ => return invariant("IsAClassOf target is not a class"),
        }
    }
    if primary_count != 1 {
        return invariant("tank must have exactly one primary-class IsAClassOf edge");
    }
    Ok(())
}

fn has_incoming(graph: &TankGraph, target: NodeIndex, edge_type: EdgeType) -> bool {
    graph
        .edges_directed(target, Direction::Incoming)
        .any(|edge| *edge.weight() == edge_type)
}

fn validate_lookup_map(
    graph: &TankGraph,
    lookup: &LookupMap,
    expected: fn(&NodeType) -> bool,
) -> Result<(), GraphError> {
    let mut seen_indexes = HashSet::new();
    for (key, &index) in lookup {
        let node = graph
            .node_weight(index)
            .ok_or_else(|| GraphError::InvariantFailure {
                message: "lookup index is out of bounds".to_owned(),
            })?;
        if !expected(node) {
            return invariant("lookup maps to the wrong node variant");
        }
        let stored_key = match node {
            NodeType::Class(label) | NodeType::Keyword(label) => identity_key(label),
            _ => return invariant("lookup maps to the wrong node variant"),
        };
        if stored_key != *key {
            return invariant("lookup key does not match the stored node");
        }
        if !seen_indexes.insert(index) {
            return invariant("lookup maps two keys to the same node");
        }
    }
    Ok(())
}

fn tally_statistics(
    graph: &TankGraph,
    input_records: usize,
    duplicate_edges_suppressed: usize,
) -> GraphStatistics {
    let mut statistics = GraphStatistics {
        input_records,
        duplicate_edges_suppressed,
        ..GraphStatistics::default()
    };
    for node in graph.node_weights() {
        match node {
            NodeType::Tank { .. } => statistics.tank_nodes += 1,
            NodeType::Class(_) => statistics.class_nodes += 1,
            NodeType::Strategy(_) => statistics.strategy_nodes += 1,
            NodeType::Keyword(_) => statistics.keyword_nodes += 1,
        }
    }
    for edge in graph.edge_weights() {
        match edge {
            EdgeType::IsAClassOf => statistics.is_a_class_of_edges += 1,
            EdgeType::IsSubclassOf => statistics.is_subclass_of_edges += 1,
            EdgeType::RecommendedTactic => statistics.recommended_tactic_edges += 1,
            EdgeType::AssociatedWithKeyword => statistics.associated_with_keyword_edges += 1,
        }
    }
    statistics
}

fn invariant(message: &str) -> Result<(), GraphError> {
    Err(GraphError::InvariantFailure {
        message: message.to_owned(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::TankRecord;

    fn record(
        name: &str,
        class_name: &str,
        subclasses: &[&str],
        strategies: &[&str],
        keywords: &[&str],
    ) -> TankRecord {
        TankRecord {
            name: name.to_owned(),
            display_name: name.to_owned(),
            class_name: class_name.to_owned(),
            subclasses: subclasses.iter().map(|value| (*value).to_owned()).collect(),
            nation: "USA".to_owned(),
            tier: 5,
            strategies: strategies.iter().map(|value| (*value).to_owned()).collect(),
            keywords: keywords.iter().map(|value| (*value).to_owned()).collect(),
        }
    }

    fn dataset(records: Vec<TankRecord>) -> ValidatedDataset {
        ValidatedDataset {
            records,
            warnings: Vec::new(),
        }
    }

    fn tank_node(name: &str) -> NodeType {
        NodeType::Tank {
            name: name.to_owned(),
            display_name: name.to_owned(),
            nation: "USA".to_owned(),
            tier: 5,
        }
    }

    fn cache_from_graph(graph: &TankGraph) -> HashMap<NodeType, NodeIndex> {
        graph
            .node_indices()
            .map(|index| (graph[index].clone(), index))
            .collect()
    }

    fn empty_lookups() -> (LookupMap, LookupMap) {
        (HashMap::new(), HashMap::new())
    }

    fn expect_invariant(result: Result<(), GraphError>) -> String {
        match result {
            Err(GraphError::InvariantFailure { message }) => message,
            other => panic!("expected invariant failure, got {other:?}"),
        }
    }

    #[test]
    fn relationship_attempts_overflow_without_allocating_records() {
        let error =
            relationship_attempts_for_record(0, usize::MAX, 2).expect_err("S × K must overflow");
        assert_eq!(error, GraphError::ArithmeticOverflow);
    }

    #[test]
    fn node_attempts_overflow_without_allocating_records() {
        let error = node_attempts_for_record(usize::MAX, 0, 0).expect_err("2 + C must overflow");
        assert_eq!(error, GraphError::ArithmeticOverflow);
    }

    #[test]
    fn estimate_accumulator_overflows_near_usize_max() {
        let added = node_attempts_for_record(0, 0, 0).expect("tiny record");
        let error = checked_add(usize::MAX - 1, added).expect_err("accumulator overflow");
        assert_eq!(error, GraphError::ArithmeticOverflow);
    }

    #[test]
    fn exact_budget_boundary_succeeds() {
        let data = dataset(vec![record("A01", "Heavy Tanks", &[], &[], &[])]);
        let budgets = GraphBudgets {
            maximum_nodes: 2,
            maximum_edges: 1,
            maximum_relationship_attempts: 1,
        };
        let bundle = build_graph(&data, &budgets).expect("exact boundary");
        assert_eq!(bundle.graph.node_count(), 2);
        assert_eq!(bundle.graph.edge_count(), 1);
    }

    #[test]
    fn node_budget_rejects_before_allocation() {
        let data = dataset(vec![record("A01", "Heavy Tanks", &[], &[], &[])]);
        let budgets = GraphBudgets {
            maximum_nodes: 1,
            maximum_edges: 5,
            maximum_relationship_attempts: 5,
        };
        let error = build_graph(&data, &budgets).expect_err("node budget");
        assert!(matches!(
            error,
            GraphError::NodeBudgetExceeded {
                attempted: 2,
                maximum: 1
            }
        ));
    }

    #[test]
    fn relationship_budget_rejects_cartesian_fan_out() {
        let data = dataset(vec![record(
            "A01",
            "Heavy Tanks",
            &[],
            &["hold", "push"],
            &["hull", "ridge"],
        )]);
        let budgets = GraphBudgets {
            maximum_nodes: 100,
            maximum_edges: 100,
            maximum_relationship_attempts: 6,
        };
        let error = build_graph(&data, &budgets).expect_err("relationship budget");
        assert!(matches!(
            error,
            GraphError::RelationshipBudgetExceeded {
                attempted: 7,
                maximum: 6
            }
        ));
    }

    #[test]
    fn edge_budget_uses_relationship_upper_bound() {
        let data = dataset(vec![record(
            "A01",
            "Heavy Tanks",
            &[],
            &["hold", "push"],
            &["hull", "ridge"],
        )]);
        let budgets = GraphBudgets {
            maximum_nodes: 100,
            maximum_edges: 6,
            maximum_relationship_attempts: 100,
        };
        let error = build_graph(&data, &budgets).expect_err("edge budget");
        assert!(matches!(
            error,
            GraphError::EdgeBudgetExceeded {
                attempted: 7,
                maximum: 6
            }
        ));
    }

    #[test]
    fn typed_edges_are_distinct_by_variant() {
        let mut graph = TankGraph::new();
        let source = graph.add_node(NodeType::Class("Autoloaders".to_owned()));
        let target = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        let mut edge_cache = HashSet::new();
        let mut suppressed = 0;
        add_unique_edge(
            &mut graph,
            &mut edge_cache,
            source,
            EdgeType::IsAClassOf,
            target,
            &mut suppressed,
        );
        add_unique_edge(
            &mut graph,
            &mut edge_cache,
            source,
            EdgeType::IsSubclassOf,
            target,
            &mut suppressed,
        );
        assert_eq!(graph.edge_count(), 2);
        add_unique_edge(
            &mut graph,
            &mut edge_cache,
            source,
            EdgeType::IsAClassOf,
            target,
            &mut suppressed,
        );
        assert_eq!(graph.edge_count(), 2);
        assert_eq!(suppressed, 1);
    }

    #[test]
    fn cache_size_mismatch_is_detected() {
        let mut graph = TankGraph::new();
        graph.add_node(tank_node("A01"));
        graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        let error = validate_graph_invariants(
            &graph,
            &HashMap::new(),
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("cache size"));
    }

    #[test]
    fn cached_index_mismatch_is_detected() {
        let mut graph = TankGraph::new();
        graph.add_node(tank_node("A01"));
        graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        let mut cache = cache_from_graph(&graph);
        cache.insert(tank_node("A01"), NodeIndex::new(1));
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("different node"));
    }

    #[test]
    fn reversed_class_edge_is_detected() {
        let mut graph = TankGraph::new();
        let tank = graph.add_node(tank_node("A01"));
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        graph.add_edge(class, tank, EdgeType::IsAClassOf);
        let cache = cache_from_graph(&graph);
        let (class_lookup, keyword_lookup) = empty_lookups();
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &class_lookup,
            &keyword_lookup,
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("endpoints"));
    }

    #[test]
    fn subclass_edge_from_primary_class_is_detected() {
        let mut graph = TankGraph::new();
        let heavy = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        let medium = graph.add_node(NodeType::Class("Medium Tanks".to_owned()));
        graph.add_edge(heavy, medium, EdgeType::IsSubclassOf);
        let cache = cache_from_graph(&graph);
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("primary class"));
    }

    #[test]
    fn missing_incoming_strategy_edge_is_detected() {
        let mut graph = TankGraph::new();
        let tank = graph.add_node(tank_node("A01"));
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        graph.add_node(NodeType::Strategy("hold the ridge".to_owned()));
        graph.add_edge(tank, class, EdgeType::IsAClassOf);
        let cache = cache_from_graph(&graph);
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("RecommendedTactic"));
    }

    #[test]
    fn missing_incoming_keyword_edge_is_detected() {
        let mut graph = TankGraph::new();
        let tank = graph.add_node(tank_node("A01"));
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        let strategy = graph.add_node(NodeType::Strategy("hold the ridge".to_owned()));
        graph.add_node(NodeType::Keyword("hull".to_owned()));
        graph.add_edge(tank, class, EdgeType::IsAClassOf);
        graph.add_edge(tank, strategy, EdgeType::RecommendedTactic);
        let cache = cache_from_graph(&graph);
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("AssociatedWithKeyword"));
    }

    #[test]
    fn tank_without_primary_class_edge_is_detected() {
        let mut graph = TankGraph::new();
        let tank = graph.add_node(tank_node("A01"));
        let subclass = graph.add_node(NodeType::Class("Autoloaders".to_owned()));
        graph.add_edge(tank, subclass, EdgeType::IsAClassOf);
        let cache = cache_from_graph(&graph);
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("primary-class"));
    }

    #[test]
    fn duplicate_typed_edge_is_detected() {
        let mut graph = TankGraph::new();
        let tank = graph.add_node(tank_node("A01"));
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        graph.add_edge(tank, class, EdgeType::IsAClassOf);
        graph.add_edge(tank, class, EdgeType::IsAClassOf);
        let cache = cache_from_graph(&graph);
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("duplicate"));
    }

    #[test]
    fn lookup_wrong_variant_is_detected() {
        let mut graph = TankGraph::new();
        let tank = graph.add_node(tank_node("A01"));
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        graph.add_edge(tank, class, EdgeType::IsAClassOf);
        let cache = cache_from_graph(&graph);
        let mut class_lookup = HashMap::new();
        class_lookup.insert(identity_key("Heavy Tanks"), tank);
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &class_lookup,
            &HashMap::new(),
            &GraphBudgets::default(),
        );
        assert!(expect_invariant(error).contains("wrong node variant"));
    }

    #[test]
    fn lookup_key_collision_is_error() {
        let mut cache = HashMap::new();
        cache.insert(NodeType::Class("Heavy Tanks".to_owned()), NodeIndex::new(0));
        cache.insert(NodeType::Class("heavy tanks".to_owned()), NodeIndex::new(1));
        let error = derive_query_indexes(&cache).expect_err("collision");
        assert!(matches!(error, GraphError::LookupKeyCollision { .. }));
    }

    #[test]
    fn actual_counts_over_budget_fail_invariants() {
        let mut graph = TankGraph::new();
        let tank = graph.add_node(tank_node("A01"));
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        graph.add_edge(tank, class, EdgeType::IsAClassOf);
        let cache = cache_from_graph(&graph);
        let error = validate_graph_invariants(
            &graph,
            &cache,
            &HashMap::new(),
            &HashMap::new(),
            &GraphBudgets {
                maximum_nodes: 1,
                maximum_edges: 10,
                maximum_relationship_attempts: 10,
            },
        );
        assert!(matches!(
            error,
            Err(GraphError::NodeBudgetExceeded {
                attempted: 2,
                maximum: 1
            })
        ));
    }
}
