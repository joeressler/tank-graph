use std::path::{Path, PathBuf};
use std::process::Command;
use tank_graph::graph::{build_graph, GraphBudgets};
use tank_graph::ingest::{load_dataset, Limits};
use tank_graph::query::{query_class, query_keyword};

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("benchmark_tanks_data.json")
}

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_tank-graph")
}

#[test]
fn fixture_class_and_keyword_results_are_complete_and_sorted() {
    let dataset = load_dataset(fixture(), &Limits::default()).expect("load");
    let bundle = build_graph(&dataset, &GraphBudgets::default()).expect("build");

    let heavies = query_class(&bundle, "heavy  tanks").expect("class");
    assert_eq!(
        heavies
            .iter()
            .map(|row| row.name.as_str())
            .collect::<Vec<_>>(),
        vec!["B01_Alpha", "A01_Zebra"]
    );
    assert!(heavies.iter().all(|row| row.matched_strategies.is_empty()));

    let tags = query_class(&bundle, "Autoloaders").expect("subclass");
    assert_eq!(
        tags.iter().map(|row| row.name.as_str()).collect::<Vec<_>>(),
        vec!["C01_Medium", "A01_Zebra"]
    );

    let sides = query_keyword(&bundle, "Sidescraping").expect("keyword");
    assert_eq!(
        sides
            .iter()
            .map(|row| row.name.as_str())
            .collect::<Vec<_>>(),
        vec!["B01_Alpha", "A01_Zebra"]
    );
    assert_eq!(sides[1].matched_strategies.len(), 2);
}

#[test]
fn cli_missing_required_flags_and_unknown_command_are_usage() {
    let missing_data = Command::new(binary())
        .args([
            "--format",
            "text",
            "query",
            "class",
            "--name",
            "Heavy Tanks",
        ])
        .output()
        .expect("missing data");
    assert_eq!(missing_data.status.code(), Some(2));

    let unknown = Command::new(binary())
        .args(["--data", "x.json", "--format", "text", "dance"])
        .output()
        .expect("unknown");
    assert_eq!(unknown.status.code(), Some(2));

    let invalid_format = Command::new(binary())
        .args([
            "--data",
            fixture().to_str().unwrap(),
            "--format",
            "xml",
            "query",
            "class",
            "--name",
            "Heavy Tanks",
        ])
        .output()
        .expect("format");
    assert_eq!(invalid_format.status.code(), Some(2));

    let empty_name = Command::new(binary())
        .args([
            "--data",
            fixture().to_str().unwrap(),
            "--format",
            "text",
            "query",
            "class",
            "--name",
            "   ",
        ])
        .output()
        .expect("empty name");
    assert_eq!(empty_name.status.code(), Some(2));
}

#[test]
fn help_contains_required_examples() {
    let top = Command::new(binary())
        .args(["--help"])
        .output()
        .expect("top help");
    let top_text = String::from_utf8_lossy(&top.stdout);
    assert!(top.status.success());
    assert!(top_text.contains("query"));
    assert!(top_text.contains("benchmark"));

    let class_help = Command::new(binary())
        .args(["query", "class", "--help"])
        .output()
        .expect("class help");
    let class_text = String::from_utf8_lossy(&class_help.stdout);
    assert!(class_text.contains(
        "tank-graph --data data/tanks_data.json --format text query class --name \"Heavy Tanks\""
    ));

    let keyword_help = Command::new(binary())
        .args(["query", "keyword", "--help"])
        .output()
        .expect("keyword help");
    let keyword_text = String::from_utf8_lossy(&keyword_help.stdout);
    assert!(keyword_text.contains(
        "tank-graph --data data/tanks_data.json --format json query keyword --name \"sidescraping\""
    ));
}

#[test]
fn cli_query_json_goes_to_stdout_and_errors_to_stderr() {
    let ok = Command::new(binary())
        .args([
            "--data",
            fixture().to_str().unwrap(),
            "--format",
            "json",
            "query",
            "keyword",
            "--name",
            "sidescraping",
        ])
        .output()
        .expect("json query");
    assert!(ok.status.success());
    let stdout = String::from_utf8_lossy(&ok.stdout);
    assert!(ok.stderr.is_empty());
    assert!(stdout.contains("\"kind\": \"keyword\""));
    assert!(!stdout.contains('\u{001b}'));

    let missing = Command::new(binary())
        .args([
            "--data",
            fixture().to_str().unwrap(),
            "--format",
            "json",
            "query",
            "class",
            "--name",
            "SPGs",
        ])
        .output()
        .expect("missing class");
    assert_eq!(missing.status.code(), Some(4));
    assert!(String::from_utf8_lossy(&missing.stdout).is_empty());
    assert!(String::from_utf8_lossy(&missing.stderr).contains("query_value_not_found"));
}

#[test]
fn cli_ingest_failure_is_status_three() {
    let failed = Command::new(binary())
        .args([
            "--data",
            "missing-file.json",
            "--format",
            "text",
            "query",
            "class",
            "--name",
            "Heavy Tanks",
        ])
        .output()
        .expect("missing file");
    assert_eq!(failed.status.code(), Some(3));
}

#[test]
fn cli_benchmark_threshold_zero_is_status_seven_and_prints_report() {
    let output = Command::new(binary())
        .args([
            "--data",
            fixture().to_str().unwrap(),
            "--format",
            "json",
            "benchmark",
            "class",
            "--name",
            "Heavy Tanks",
            "--warmup",
            "1",
            "--iterations",
            "2",
            "--max-p95-us",
            "0",
        ])
        .output()
        .expect("threshold");
    assert_eq!(output.status.code(), Some(7));
    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(stdout.contains("\"threshold_met\": false") || stdout.contains("p95"));
}
