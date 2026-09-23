//! Stable text and JSON reports for queries, errors, and benchmarks.

use crate::benchmark::BenchmarkReport;
use crate::graph::GraphError;
use crate::ingest::IngestError;
use crate::query::{QueryError, QueryKind, TankQueryResult};
use serde::Serialize;
use std::fmt::Write as _;
use std::io::{self, Write};
use std::path::PathBuf;

/// Whole-microsecond phase timings for an ordinary query command.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TimingUs {
    pub load: u64,
    pub build: u64,
    pub query: u64,
    pub total: u64,
}

/// Successful query payload shared by text and JSON formatters.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct QueryReport {
    pub query: QueryMeta,
    pub result_count: usize,
    pub results: Vec<TankQueryResult>,
    pub timing_us: TimingUs,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct QueryMeta {
    pub kind: QueryKind,
    pub value: String,
    pub normalized_value: String,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ErrorReport {
    pub error: ErrorBody,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ErrorBody {
    pub code: &'static str,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub query_kind: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub normalized_value: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
}

/// Failures mapped to CLI exit statuses 2–7.
#[derive(Debug)]
pub enum AppError {
    Usage(String),
    Ingest(IngestError),
    Graph(GraphError),
    Query(QueryError),
    Threshold { report: Box<BenchmarkReport> },
}

impl AppError {
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Usage(_) => 2,
            Self::Ingest(_) => 3,
            Self::Query(QueryError::ValueNotFound { .. }) => 4,
            Self::Graph(GraphError::ArithmeticOverflow)
            | Self::Graph(GraphError::NodeBudgetExceeded { .. })
            | Self::Graph(GraphError::EdgeBudgetExceeded { .. })
            | Self::Graph(GraphError::RelationshipBudgetExceeded { .. }) => 5,
            Self::Query(QueryError::InvariantFailure { .. })
            | Self::Graph(GraphError::InvariantFailure { .. })
            | Self::Graph(GraphError::LookupKeyCollision { .. }) => 6,
            Self::Threshold { .. } => 7,
        }
    }

    pub fn to_error_report(&self) -> ErrorReport {
        match self {
            Self::Usage(message) => ErrorReport {
                error: ErrorBody {
                    code: "usage",
                    message: message.clone(),
                    query_kind: None,
                    value: None,
                    normalized_value: None,
                    path: None,
                },
            },
            Self::Ingest(error) => ingest_error_report(error),
            Self::Graph(error) => graph_error_report(error),
            Self::Query(error) => query_error_report(error),
            Self::Threshold { report } => ErrorReport {
                error: ErrorBody {
                    code: "threshold_not_met",
                    message: format!(
                        "p95 {:.3} us exceeds threshold {:.3} us",
                        report.timing_us.p95,
                        report.threshold_us.unwrap_or(0.0)
                    ),
                    query_kind: Some(report.kind.as_str()),
                    value: Some(report.value.clone()),
                    normalized_value: Some(report.normalized_value.clone()),
                    path: None,
                },
            },
        }
    }
}

impl std::fmt::Display for AppError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Usage(message) => write!(f, "{message}"),
            Self::Ingest(error) => write!(f, "{error}"),
            Self::Graph(error) => write!(f, "{error}"),
            Self::Query(error) => write!(f, "{error}"),
            Self::Threshold { report } => write!(
                f,
                "p95 {:.3} us exceeds threshold {:.3} us",
                report.timing_us.p95,
                report.threshold_us.unwrap_or(0.0)
            ),
        }
    }
}

impl std::error::Error for AppError {}

fn ingest_error_report(error: &IngestError) -> ErrorReport {
    let (code, path) = match error {
        IngestError::PathNotFound { path } => ("path_not_found", Some(path)),
        IngestError::OpenFailed { path, .. } => ("open_failed", Some(path)),
        IngestError::NotRegularFile { path } => ("not_regular_file", Some(path)),
        IngestError::FileTooLarge { path, .. } => ("file_too_large", Some(path)),
        IngestError::JsonSyntax { .. } => ("json_syntax", None),
        IngestError::SemanticValidation { .. } => ("semantic_validation", None),
    };
    ErrorReport {
        error: ErrorBody {
            code,
            message: error.to_string(),
            query_kind: None,
            value: None,
            normalized_value: None,
            path: path.map(|p: &PathBuf| p.display().to_string()),
        },
    }
}

fn graph_error_report(error: &GraphError) -> ErrorReport {
    let code = match error {
        GraphError::ArithmeticOverflow => "arithmetic_overflow",
        GraphError::NodeBudgetExceeded { .. } => "node_budget_exceeded",
        GraphError::EdgeBudgetExceeded { .. } => "edge_budget_exceeded",
        GraphError::RelationshipBudgetExceeded { .. } => "relationship_budget_exceeded",
        GraphError::InvariantFailure { .. } => "graph_invariant_failure",
        GraphError::LookupKeyCollision { .. } => "lookup_key_collision",
    };
    ErrorReport {
        error: ErrorBody {
            code,
            message: error.to_string(),
            query_kind: None,
            value: None,
            normalized_value: None,
            path: None,
        },
    }
}

fn query_error_report(error: &QueryError) -> ErrorReport {
    match error {
        QueryError::ValueNotFound {
            kind,
            value,
            normalized_value,
            ..
        } => ErrorReport {
            error: ErrorBody {
                code: "query_value_not_found",
                message: format!("No {} node matched the supplied value.", kind.as_str()),
                query_kind: Some(kind.as_str()),
                value: Some(value.clone()),
                normalized_value: Some(normalized_value.clone()),
                path: None,
            },
        },
        QueryError::InvariantFailure { message } => ErrorReport {
            error: ErrorBody {
                code: "graph_invariant_failure",
                message: message.clone(),
                query_kind: None,
                value: None,
                normalized_value: None,
                path: None,
            },
        },
    }
}

pub fn format_query_json(report: &QueryReport) -> Result<String, serde_json::Error> {
    serde_json::to_string_pretty(report)
}

pub fn format_error_json(report: &ErrorReport) -> Result<String, serde_json::Error> {
    serde_json::to_string_pretty(report)
}

pub fn format_benchmark_json(report: &BenchmarkReport) -> Result<String, serde_json::Error> {
    serde_json::to_string_pretty(report)
}

pub fn format_query_text(report: &QueryReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "query_kind: {}", report.query.kind.as_str());
    let _ = writeln!(out, "value: {}", report.query.value);
    let _ = writeln!(out, "normalized_value: {}", report.query.normalized_value);
    let _ = writeln!(out, "result_count: {}", report.result_count);
    for result in &report.results {
        let _ = writeln!(out);
        let _ = writeln!(out, "name: {}", result.name);
        let _ = writeln!(out, "display_name: {}", result.display_name);
        let _ = writeln!(out, "nation: {}", result.nation);
        let _ = writeln!(out, "tier: {}", result.tier);
        let _ = writeln!(out, "matched_strategies:");
        for strategy in &result.matched_strategies {
            let _ = writeln!(out, "  - {strategy}");
        }
    }
    let _ = writeln!(out);
    let _ = writeln!(out, "load_us: {}", report.timing_us.load);
    let _ = writeln!(out, "build_us: {}", report.timing_us.build);
    let _ = writeln!(out, "query_us: {}", report.timing_us.query);
    let _ = writeln!(out, "total_us: {}", report.timing_us.total);
    out
}

pub fn format_benchmark_text(report: &BenchmarkReport) -> String {
    let mut out = String::new();
    let _ = writeln!(out, "kind: {}", report.kind.as_str());
    let _ = writeln!(out, "value: {}", report.value);
    let _ = writeln!(out, "normalized_value: {}", report.normalized_value);
    let _ = writeln!(out, "application_version: {}", report.application_version);
    let _ = writeln!(out, "build_profile: {}", report.build_profile);
    let _ = writeln!(out, "warmup: {}", report.warmup);
    let _ = writeln!(out, "iterations: {}", report.iterations);
    let _ = writeln!(out, "result_count: {}", report.result_count);
    let _ = writeln!(out, "graph_nodes: {}", report.graph_nodes);
    let _ = writeln!(out, "graph_edges: {}", report.graph_edges);
    let _ = writeln!(out, "os: {}", report.os);
    let _ = writeln!(out, "arch: {}", report.arch);
    let _ = writeln!(out, "min_us: {:.3}", report.timing_us.min);
    let _ = writeln!(out, "median_us: {:.3}", report.timing_us.median);
    let _ = writeln!(out, "p95_us: {:.3}", report.timing_us.p95);
    let _ = writeln!(out, "max_us: {:.3}", report.timing_us.max);
    if let Some(threshold) = report.threshold_us {
        let _ = writeln!(out, "threshold_us: {threshold:.3}");
    }
    let _ = writeln!(out, "threshold_met: {}", report.threshold_met);
    out
}

pub fn write_stdout(text: &str) -> io::Result<()> {
    let mut out = io::stdout().lock();
    out.write_all(text.as_bytes())?;
    if !text.ends_with('\n') {
        out.write_all(b"\n")?;
    }
    Ok(())
}

pub fn write_stderr(text: &str) -> io::Result<()> {
    let mut out = io::stderr().lock();
    out.write_all(text.as_bytes())?;
    if !text.ends_with('\n') {
        out.write_all(b"\n")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::timing::ceil_micros;
    use std::path::PathBuf;

    fn sample_report() -> QueryReport {
        QueryReport {
            query: QueryMeta {
                kind: QueryKind::Keyword,
                value: "sidescraping".to_owned(),
                normalized_value: "sidescraping".to_owned(),
            },
            result_count: 1,
            results: vec![TankQueryResult {
                name: "G04_PzVI_Tiger_I".to_owned(),
                display_name: "Tiger I".to_owned(),
                nation: "Germany".to_owned(),
                tier: 7,
                matched_strategies: vec![
                    "Hull armor is flat and easily penetrated; rely on sidescraping configurations."
                        .to_owned(),
                ],
            }],
            timing_us: TimingUs {
                load: ceil_micros(1),
                build: ceil_micros(1),
                query: ceil_micros(1),
                total: ceil_micros(5),
            },
        }
    }

    #[test]
    fn json_success_shape() {
        let json = format_query_json(&sample_report()).expect("json");
        assert!(json.contains("\"kind\": \"keyword\""));
        assert!(json.contains("\"result_count\": 1"));
        assert!(json.contains("\"timing_us\""));
        assert!(!json.contains('\u{001b}'));
    }

    #[test]
    fn text_success_shape() {
        let text = format_query_text(&sample_report());
        assert!(text.starts_with("query_kind: keyword"));
        assert!(text.contains("display_name: Tiger I"));
        assert!(text.contains("load_us: 1"));
        assert!(text.contains("total_us: 1") || text.contains("total_us: 5"));
    }

    #[test]
    fn json_error_shape() {
        let report = AppError::Query(QueryError::ValueNotFound {
            kind: QueryKind::Keyword,
            value: "unknown tactic".to_owned(),
            normalized_value: "unknown tactic".to_owned(),
            available: Vec::new(),
        })
        .to_error_report();
        let json = format_error_json(&report).expect("json");
        assert!(json.contains("query_value_not_found"));
        assert!(json.contains("unknown tactic"));
        assert!(!json.contains('\u{001b}'));
    }

    #[test]
    fn fake_clock_ceil_is_nonzero_for_one_nanosecond() {
        assert_eq!(ceil_micros(1), 1);
    }

    #[test]
    fn exit_codes_match_status_table() {
        assert_eq!(AppError::Usage("x".into()).exit_code(), 2);
        assert_eq!(
            AppError::Ingest(crate::ingest::IngestError::PathNotFound {
                path: PathBuf::from("missing.json"),
            })
            .exit_code(),
            3
        );
        assert_eq!(
            AppError::Query(QueryError::ValueNotFound {
                kind: QueryKind::Class,
                value: "x".into(),
                normalized_value: "x".into(),
                available: Vec::new(),
            })
            .exit_code(),
            4
        );
        assert_eq!(
            AppError::Graph(crate::graph::GraphError::NodeBudgetExceeded {
                attempted: 3,
                maximum: 1,
            })
            .exit_code(),
            5
        );
        assert_eq!(
            AppError::Graph(crate::graph::GraphError::InvariantFailure {
                message: "x".into(),
            })
            .exit_code(),
            6
        );
        assert_eq!(
            AppError::Query(QueryError::InvariantFailure {
                message: "x".into(),
            })
            .exit_code(),
            6
        );
    }
}
