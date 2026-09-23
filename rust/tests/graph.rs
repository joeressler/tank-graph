use std::path::{Path, PathBuf};
use tank_graph::identity::identity_key;
use tank_graph::{
    build_graph, load_dataset, EdgeType, GraphBudgets, GraphError, Limits, NodeType, TankRecord,
    ValidatedDataset,
};

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

fn tiger_fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("tiger_i.json")
}

fn incoming_class_tanks(bundle: &tank_graph::GraphBundle, class: &str) -> Vec<String> {
    let class_index = *bundle
        .class_lookup
        .get(&identity_key(class))
        .expect("class lookup");
    let mut names = Vec::new();
    for edge in bundle.graph.edge_indices() {
        let (source, target) = bundle.graph.edge_endpoints(edge).expect("edge endpoints");
        if target != class_index || bundle.graph[edge] != EdgeType::IsAClassOf {
            continue;
        }
        if let NodeType::Tank { name, .. } = &bundle.graph[source] {
            names.push(name.clone());
        }
    }
    names.sort();
    names
}

fn edge_triples(bundle: &tank_graph::GraphBundle) -> Vec<(NodeType, EdgeType, NodeType)> {
    let mut triples = Vec::new();
    for edge in bundle.graph.edge_indices() {
        let (source, target) = bundle.graph.edge_endpoints(edge).expect("edge endpoints");
        triples.push((
            bundle.graph[source].clone(),
            bundle.graph[edge],
            bundle.graph[target].clone(),
        ));
    }
    triples.sort_by_key(|(source, edge_type, target)| {
        (
            format!("{source:?}"),
            format!("{edge_type:?}"),
            format!("{target:?}"),
        )
    });
    triples
}

#[test]
fn single_record_fan_out_has_six_nodes_and_seven_edges() {
    let data = dataset(vec![record(
        "A01",
        "Heavy Tanks",
        &[],
        &["hold the ridge", "push the flank"],
        &["hull", "ridge"],
    )]);
    let bundle = build_graph(&data, &GraphBudgets::default()).expect("fan-out");
    assert_eq!(bundle.graph.node_count(), 6);
    assert_eq!(bundle.graph.edge_count(), 7);
    assert_eq!(bundle.statistics.tank_nodes, 1);
    assert_eq!(bundle.statistics.class_nodes, 1);
    assert_eq!(bundle.statistics.strategy_nodes, 2);
    assert_eq!(bundle.statistics.keyword_nodes, 2);
    assert_eq!(bundle.statistics.is_a_class_of_edges, 1);
    assert_eq!(bundle.statistics.recommended_tactic_edges, 2);
    assert_eq!(bundle.statistics.associated_with_keyword_edges, 4);
    assert_eq!(bundle.statistics.duplicate_edges_suppressed, 0);
}

#[test]
fn subclass_fixture_has_five_nodes_and_five_edges() {
    let data = dataset(vec![record(
        "A01",
        "Heavy Tanks",
        &["Autoloaders"],
        &["hold the ridge"],
        &["hull"],
    )]);
    let bundle = build_graph(&data, &GraphBudgets::default()).expect("subclass");
    assert_eq!(bundle.graph.node_count(), 5);
    assert_eq!(bundle.graph.edge_count(), 5);
    assert_eq!(bundle.statistics.class_nodes, 2);
    assert_eq!(bundle.statistics.is_a_class_of_edges, 2);
    assert_eq!(bundle.statistics.is_subclass_of_edges, 1);
    assert_eq!(bundle.statistics.recommended_tactic_edges, 1);
    assert_eq!(bundle.statistics.associated_with_keyword_edges, 1);
}

#[test]
fn shared_entities_deduplicate_and_suppress_repeated_keyword_edge() {
    let data = dataset(vec![
        record(
            "A01",
            "Heavy Tanks",
            &[],
            &["private alpha", "shared hold"],
            &["hull"],
        ),
        record(
            "B01",
            "Heavy Tanks",
            &[],
            &["private bravo", "shared hold"],
            &["hull"],
        ),
    ]);
    let bundle = build_graph(&data, &GraphBudgets::default()).expect("shared");
    assert_eq!(bundle.graph.node_count(), 7);
    assert_eq!(bundle.graph.edge_count(), 9);
    assert_eq!(bundle.statistics.tank_nodes, 2);
    assert_eq!(bundle.statistics.class_nodes, 1);
    assert_eq!(bundle.statistics.strategy_nodes, 3);
    assert_eq!(bundle.statistics.keyword_nodes, 1);
    assert_eq!(bundle.statistics.is_a_class_of_edges, 2);
    assert_eq!(bundle.statistics.recommended_tactic_edges, 4);
    assert_eq!(bundle.statistics.associated_with_keyword_edges, 3);
    assert_eq!(bundle.statistics.duplicate_edges_suppressed, 1);
}

#[test]
fn subclass_with_two_parents_does_not_expand_primary_membership() {
    let data = dataset(vec![
        record("A01", "Heavy Tanks", &["Autoloaders"], &[], &[]),
        record("B01", "Medium Tanks", &["Autoloaders"], &[], &[]),
    ]);
    let bundle = build_graph(&data, &GraphBudgets::default()).expect("taxonomy");
    assert_eq!(incoming_class_tanks(&bundle, "Heavy Tanks"), vec!["A01"]);
    assert_eq!(incoming_class_tanks(&bundle, "Medium Tanks"), vec!["B01"]);
    assert_eq!(
        incoming_class_tanks(&bundle, "Autoloaders"),
        vec!["A01", "B01"]
    );
    assert_eq!(bundle.statistics.is_subclass_of_edges, 2);
}

#[test]
fn empty_tactics_build_tank_and_class_only() {
    let data = dataset(vec![record("A01", "Heavy Tanks", &[], &[], &[])]);
    let bundle = build_graph(&data, &GraphBudgets::default()).expect("empty tactics");
    assert_eq!(bundle.graph.node_count(), 2);
    assert_eq!(bundle.graph.edge_count(), 1);
    assert_eq!(bundle.statistics.strategy_nodes, 0);
    assert_eq!(bundle.statistics.keyword_nodes, 0);
    assert!(bundle.keyword_lookup.is_empty());
}

#[test]
fn identical_input_is_deterministic() {
    let data = dataset(vec![record(
        "A01",
        "Heavy Tanks",
        &["Autoloaders"],
        &["hold the ridge", "push the flank"],
        &["hull", "ridge"],
    )]);
    let first = build_graph(&data, &GraphBudgets::default()).expect("first");
    let second = build_graph(&data, &GraphBudgets::default()).expect("second");
    assert_eq!(first.graph.node_count(), second.graph.node_count());
    assert_eq!(first.graph.edge_count(), second.graph.edge_count());
    assert_eq!(first.statistics, second.statistics);
    let first_nodes: Vec<_> = first.graph.node_weights().cloned().collect();
    let second_nodes: Vec<_> = second.graph.node_weights().cloned().collect();
    assert_eq!(first_nodes, second_nodes);
    assert_eq!(edge_triples(&first), edge_triples(&second));
}

#[test]
fn too_small_budget_returns_error_and_no_bundle() {
    let data = dataset(vec![record("A01", "Heavy Tanks", &[], &[], &[])]);
    let result = build_graph(
        &data,
        &GraphBudgets {
            maximum_nodes: 1,
            maximum_edges: 1,
            maximum_relationship_attempts: 1,
        },
    );
    assert!(matches!(result, Err(GraphError::NodeBudgetExceeded { .. })));
}

#[test]
fn tiger_fixture_builds_eight_nodes_and_eleven_edges() {
    let loaded = load_dataset(tiger_fixture(), &Limits::default()).expect("load tiger");
    let bundle = build_graph(&loaded, &GraphBudgets::default()).expect("build tiger");
    assert_eq!(bundle.graph.node_count(), 8);
    assert_eq!(bundle.graph.edge_count(), 11);
    assert_eq!(bundle.statistics.tank_nodes, 1);
    assert_eq!(bundle.statistics.class_nodes, 1);
    assert_eq!(bundle.statistics.strategy_nodes, 2);
    assert_eq!(bundle.statistics.keyword_nodes, 4);
    assert_eq!(bundle.statistics.is_a_class_of_edges, 1);
    assert_eq!(bundle.statistics.recommended_tactic_edges, 2);
    assert_eq!(bundle.statistics.associated_with_keyword_edges, 8);
    assert_eq!(
        bundle.node_cache.len(),
        bundle.graph.node_count(),
        "every insertion uses the canonical node cache"
    );
}
