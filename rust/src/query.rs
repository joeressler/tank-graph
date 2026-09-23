//! Exact class membership and two-hop keyword traversal.

use crate::graph::GraphBundle;
use crate::identity::{identity_key, sort_key};
use crate::model::{EdgeType, NodeType};
use petgraph::graph::NodeIndex;
use petgraph::visit::EdgeRef;
use petgraph::Direction;
use serde::Serialize;
use std::collections::HashMap;
use std::fmt;

const AVAILABLE_VALUE_CAP: usize = 16;

/// Supported query families.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum QueryKind {
    Class,
    Keyword,
}

impl QueryKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Class => "class",
            Self::Keyword => "keyword",
        }
    }
}

/// One matching tank. `matched_strategies` is empty for class queries.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TankQueryResult {
    pub name: String,
    pub display_name: String,
    pub nation: String,
    pub tier: u8,
    pub matched_strategies: Vec<String>,
}

/// Query failure with safe context only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum QueryError {
    ValueNotFound {
        kind: QueryKind,
        value: String,
        normalized_value: String,
        available: Vec<String>,
    },
    InvariantFailure {
        message: String,
    },
}

impl fmt::Display for QueryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ValueNotFound {
                kind,
                value,
                normalized_value,
                available,
            } => {
                write!(
                    f,
                    "no {} node matched `{value}` (normalized `{normalized_value}`)",
                    kind.as_str()
                )?;
                if !available.is_empty() {
                    write!(f, "; examples: {}", available.join(", "))?;
                }
                Ok(())
            }
            Self::InvariantFailure { message } => {
                write!(f, "graph invariant failed during query: {message}")
            }
        }
    }
}

impl std::error::Error for QueryError {}

/// Incoming `IsAClassOf` membership only; does not walk `IsSubclassOf`.
pub fn query_class(
    bundle: &GraphBundle,
    supplied: &str,
) -> Result<Vec<TankQueryResult>, QueryError> {
    let key = identity_key(supplied);
    let class_index = *bundle
        .class_lookup
        .get(&key)
        .ok_or_else(|| missing_value(bundle, QueryKind::Class, supplied, &key))?;
    match bundle.graph.node_weight(class_index) {
        Some(NodeType::Class(_)) => {}
        _ => {
            return Err(QueryError::InvariantFailure {
                message: "class lookup did not point at a Class node".to_owned(),
            });
        }
    }

    let mut matched: HashMap<NodeIndex, TankQueryResult> = HashMap::new();
    for edge in bundle
        .graph
        .edges_directed(class_index, Direction::Incoming)
    {
        if *edge.weight() != EdgeType::IsAClassOf {
            continue;
        }
        let source = edge.source();
        match bundle.graph.node_weight(source) {
            Some(NodeType::Tank { .. }) => {
                if let std::collections::hash_map::Entry::Vacant(entry) = matched.entry(source) {
                    entry.insert(tank_result(&bundle.graph[source], Vec::new())?);
                }
            }
            _ => {
                return Err(QueryError::InvariantFailure {
                    message: "IsAClassOf source is not a Tank".to_owned(),
                });
            }
        }
    }
    Ok(sorted_results(matched))
}

/// Two incoming hops: Keyword <- Strategy <- Tank.
pub fn query_keyword(
    bundle: &GraphBundle,
    supplied: &str,
) -> Result<Vec<TankQueryResult>, QueryError> {
    let key = identity_key(supplied);
    let keyword_index = *bundle
        .keyword_lookup
        .get(&key)
        .ok_or_else(|| missing_value(bundle, QueryKind::Keyword, supplied, &key))?;
    match bundle.graph.node_weight(keyword_index) {
        Some(NodeType::Keyword(_)) => {}
        _ => {
            return Err(QueryError::InvariantFailure {
                message: "keyword lookup did not point at a Keyword node".to_owned(),
            });
        }
    }

    let mut matched: HashMap<NodeIndex, TankQueryResult> = HashMap::new();
    for keyword_edge in bundle
        .graph
        .edges_directed(keyword_index, Direction::Incoming)
    {
        if *keyword_edge.weight() != EdgeType::AssociatedWithKeyword {
            continue;
        }
        let strategy_index = keyword_edge.source();
        let strategy_text = match bundle.graph.node_weight(strategy_index) {
            Some(NodeType::Strategy(text)) => text.clone(),
            _ => {
                return Err(QueryError::InvariantFailure {
                    message: "AssociatedWithKeyword source is not a Strategy".to_owned(),
                });
            }
        };

        for tactic_edge in bundle
            .graph
            .edges_directed(strategy_index, Direction::Incoming)
        {
            if *tactic_edge.weight() != EdgeType::RecommendedTactic {
                continue;
            }
            let tank_index = tactic_edge.source();
            match bundle.graph.node_weight(tank_index) {
                Some(NodeType::Tank { .. }) => {}
                _ => {
                    return Err(QueryError::InvariantFailure {
                        message: "RecommendedTactic source is not a Tank".to_owned(),
                    });
                }
            }
            match matched.entry(tank_index) {
                std::collections::hash_map::Entry::Occupied(mut entry) => {
                    if !entry
                        .get()
                        .matched_strategies
                        .iter()
                        .any(|s| s == &strategy_text)
                    {
                        entry
                            .get_mut()
                            .matched_strategies
                            .push(strategy_text.clone());
                    }
                }
                std::collections::hash_map::Entry::Vacant(entry) => {
                    entry.insert(tank_result(
                        &bundle.graph[tank_index],
                        vec![strategy_text.clone()],
                    )?);
                }
            }
        }
    }

    for result in matched.values_mut() {
        result
            .matched_strategies
            .sort_by_key(|value| sort_key(value));
    }
    Ok(sorted_results(matched))
}

fn tank_result(
    node: &NodeType,
    matched_strategies: Vec<String>,
) -> Result<TankQueryResult, QueryError> {
    match node {
        NodeType::Tank {
            name,
            display_name,
            nation,
            tier,
        } => Ok(TankQueryResult {
            name: name.clone(),
            display_name: display_name.clone(),
            nation: nation.clone(),
            tier: *tier,
            matched_strategies,
        }),
        _ => Err(QueryError::InvariantFailure {
            message: "expected a Tank node".to_owned(),
        }),
    }
}

fn sorted_results(matched: HashMap<NodeIndex, TankQueryResult>) -> Vec<TankQueryResult> {
    let mut results: Vec<TankQueryResult> = matched.into_values().collect();
    results.sort_by(|left, right| {
        sort_key(&left.display_name)
            .cmp(&sort_key(&right.display_name))
            .then_with(|| sort_key(&left.name).cmp(&sort_key(&right.name)))
            .then_with(|| left.nation.cmp(&right.nation))
            .then_with(|| left.tier.cmp(&right.tier))
    });
    results
}

fn missing_value(
    bundle: &GraphBundle,
    kind: QueryKind,
    value: &str,
    normalized_value: &str,
) -> QueryError {
    QueryError::ValueNotFound {
        kind,
        value: value.to_owned(),
        normalized_value: normalized_value.to_owned(),
        available: available_labels(bundle, kind),
    }
}

fn available_labels(bundle: &GraphBundle, kind: QueryKind) -> Vec<String> {
    let lookup = match kind {
        QueryKind::Class => &bundle.class_lookup,
        QueryKind::Keyword => &bundle.keyword_lookup,
    };
    let mut labels: Vec<String> = lookup
        .values()
        .filter_map(|&index| match bundle.graph.node_weight(index) {
            Some(NodeType::Class(label)) if kind == QueryKind::Class => Some(label.clone()),
            Some(NodeType::Keyword(label)) if kind == QueryKind::Keyword => Some(label.clone()),
            _ => None,
        })
        .collect();
    labels.sort_by_key(|label| sort_key(label));
    labels.truncate(AVAILABLE_VALUE_CAP);
    labels
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::{build_graph, GraphBudgets};
    use crate::ingest::ValidatedDataset;
    use crate::model::{EdgeType, NodeType, TankGraph, TankRecord};
    use std::collections::HashMap;

    #[allow(clippy::too_many_arguments)]
    fn record(
        name: &str,
        display_name: &str,
        class_name: &str,
        subclasses: &[&str],
        nation: &str,
        tier: u8,
        strategies: &[&str],
        keywords: &[&str],
    ) -> TankRecord {
        TankRecord {
            name: name.to_owned(),
            display_name: display_name.to_owned(),
            class_name: class_name.to_owned(),
            subclasses: subclasses.iter().map(|v| (*v).to_owned()).collect(),
            nation: nation.to_owned(),
            tier,
            strategies: strategies.iter().map(|v| (*v).to_owned()).collect(),
            keywords: keywords.iter().map(|v| (*v).to_owned()).collect(),
        }
    }

    fn bundle(records: Vec<TankRecord>) -> GraphBundle {
        build_graph(
            &ValidatedDataset {
                records,
                warnings: Vec::new(),
            },
            &GraphBudgets::default(),
        )
        .expect("build")
    }

    #[test]
    fn class_query_normalizes_case_and_space() {
        let graph = bundle(vec![record(
            "A01",
            "Alpha",
            "Heavy Tanks",
            &[],
            "USA",
            5,
            &[],
            &[],
        )]);
        let results = query_class(&graph, "  HEAVY   tanks ").expect("class");
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].name, "A01");
        assert!(results[0].matched_strategies.is_empty());
    }

    #[test]
    fn subclass_query_is_exact_and_not_transitive() {
        let graph = bundle(vec![
            record(
                "A01",
                "Heavy",
                "Heavy Tanks",
                &["Autoloaders"],
                "USA",
                8,
                &[],
                &[],
            ),
            record(
                "B01",
                "Medium",
                "Medium Tanks",
                &["Autoloaders"],
                "USA",
                6,
                &[],
                &[],
            ),
        ]);
        let heavy = query_class(&graph, "Heavy Tanks").expect("heavy");
        assert_eq!(
            heavy.iter().map(|r| r.name.as_str()).collect::<Vec<_>>(),
            vec!["A01"]
        );
        let tags = query_class(&graph, "Autoloaders").expect("tag");
        assert_eq!(
            tags.iter().map(|r| r.name.as_str()).collect::<Vec<_>>(),
            vec!["A01", "B01"]
        );
    }

    #[test]
    fn unknown_class_is_not_found() {
        let graph = bundle(vec![record(
            "A01",
            "Alpha",
            "Heavy Tanks",
            &[],
            "USA",
            5,
            &[],
            &[],
        )]);
        match query_class(&graph, "SPGs") {
            Err(QueryError::ValueNotFound { kind, .. }) => {
                assert_eq!(kind, QueryKind::Class);
            }
            other => panic!("expected not found, got {other:?}"),
        }
    }

    #[test]
    fn keyword_query_dedupes_tanks_and_collects_strategies() {
        let graph = bundle(vec![record(
            "A01",
            "Alpha",
            "Heavy Tanks",
            &[],
            "USA",
            5,
            &[
                "Hold the ridge and sidescrape the hull.",
                "Trade sidescraping against hull-down peekers.",
            ],
            &["hull", "sidescraping"],
        )]);
        let results = query_keyword(&graph, "SIDESCRAPING").expect("keyword");
        assert_eq!(results.len(), 1);
        assert_eq!(
            results[0].matched_strategies,
            vec![
                "Hold the ridge and sidescrape the hull.".to_owned(),
                "Trade sidescraping against hull-down peekers.".to_owned(),
            ]
        );
    }

    #[test]
    fn results_sort_by_display_name_then_name() {
        let graph = bundle(vec![
            record(
                "A01_Zebra",
                "Zebra",
                "Heavy Tanks",
                &[],
                "USA",
                8,
                &["Hold."],
                &["sidescraping"],
            ),
            record(
                "B01_Alpha",
                "Alpha",
                "Heavy Tanks",
                &[],
                "Germany",
                7,
                &["Peek."],
                &["sidescraping"],
            ),
        ]);
        let class_hits = query_class(&graph, "Heavy Tanks").expect("class");
        assert_eq!(class_hits[0].name, "B01_Alpha");
        assert_eq!(class_hits[1].name, "A01_Zebra");
        let keyword_hits = query_keyword(&graph, "sidescraping").expect("keyword");
        assert_eq!(keyword_hits[0].name, "B01_Alpha");
        assert_eq!(keyword_hits[1].name, "A01_Zebra");
    }

    #[test]
    fn unknown_keyword_is_not_found() {
        let graph = bundle(vec![record(
            "A01",
            "Alpha",
            "Heavy Tanks",
            &[],
            "USA",
            5,
            &["Hold."],
            &["hull"],
        )]);
        assert!(matches!(
            query_keyword(&graph, "mobility"),
            Err(QueryError::ValueNotFound {
                kind: QueryKind::Keyword,
                ..
            })
        ));
    }

    #[test]
    fn class_hop_wrong_endpoint_is_invariant_failure() {
        let mut graph = TankGraph::new();
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        let strategy = graph.add_node(NodeType::Strategy("not a tank".to_owned()));
        graph.add_edge(strategy, class, EdgeType::IsAClassOf);
        let mut class_lookup = HashMap::new();
        class_lookup.insert(identity_key("Heavy Tanks"), class);
        let bundle = GraphBundle {
            graph,
            node_cache: HashMap::new(),
            class_lookup,
            keyword_lookup: HashMap::new(),
            statistics: crate::graph::GraphStatistics::default(),
        };
        assert!(matches!(
            query_class(&bundle, "Heavy Tanks"),
            Err(QueryError::InvariantFailure { .. })
        ));
    }

    #[test]
    fn keyword_hop_wrong_endpoint_is_invariant_failure() {
        let mut graph = TankGraph::new();
        let keyword = graph.add_node(NodeType::Keyword("hull".to_owned()));
        let class = graph.add_node(NodeType::Class("Heavy Tanks".to_owned()));
        graph.add_edge(class, keyword, EdgeType::AssociatedWithKeyword);
        let mut keyword_lookup = HashMap::new();
        keyword_lookup.insert(identity_key("hull"), keyword);
        let bundle = GraphBundle {
            graph,
            node_cache: HashMap::new(),
            class_lookup: HashMap::new(),
            keyword_lookup,
            statistics: crate::graph::GraphStatistics::default(),
        };
        assert!(matches!(
            query_keyword(&bundle, "hull"),
            Err(QueryError::InvariantFailure { .. })
        ));
    }
}
