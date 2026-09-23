//! Load, build, and query orchestration with explicit phase clocks.

use crate::benchmark::{run_benchmark, BenchmarkReport};
use crate::graph::{build_graph, GraphBudgets};
use crate::identity::identity_key;
use crate::ingest::{load_dataset, Limits};
use crate::query::{query_class, query_keyword, QueryKind};
use crate::report::{AppError, QueryMeta, QueryReport, TimingUs};
use crate::timing::{ceil_micros, Clock};
use std::path::Path;

pub fn execute_query<C: Clock>(
    path: &Path,
    kind: QueryKind,
    value: &str,
    limits: &Limits,
    budgets: &GraphBudgets,
    clock: &C,
) -> Result<QueryReport, AppError> {
    let started = clock.now_ns();

    let load_started = clock.now_ns();
    let dataset = load_dataset(path, limits).map_err(AppError::Ingest)?;
    if !dataset.is_production_success() {
        return Err(AppError::Ingest(
            crate::ingest::IngestError::SemanticValidation {
                violations: dataset.warnings.clone(),
                displayed: dataset.warnings.len(),
                total: dataset.warnings.len(),
            },
        ));
    }
    let load_ns = clock.now_ns().saturating_sub(load_started);

    let build_started = clock.now_ns();
    let bundle = build_graph(&dataset, budgets).map_err(AppError::Graph)?;
    let build_ns = clock.now_ns().saturating_sub(build_started);

    let query_started = clock.now_ns();
    let results = match kind {
        QueryKind::Class => query_class(&bundle, value),
        QueryKind::Keyword => query_keyword(&bundle, value),
    }
    .map_err(AppError::Query)?;
    let query_ns = clock.now_ns().saturating_sub(query_started);
    let total_ns = clock.now_ns().saturating_sub(started);

    Ok(QueryReport {
        query: QueryMeta {
            kind,
            value: value.to_owned(),
            normalized_value: identity_key(value),
        },
        result_count: results.len(),
        results,
        timing_us: TimingUs {
            load: ceil_micros(load_ns),
            build: ceil_micros(build_ns),
            query: ceil_micros(query_ns),
            total: ceil_micros(total_ns),
        },
    })
}

#[allow(clippy::too_many_arguments)]
pub fn execute_benchmark<C: Clock>(
    path: &Path,
    kind: QueryKind,
    value: &str,
    warmup: usize,
    iterations: usize,
    max_p95_us: Option<f64>,
    limits: &Limits,
    budgets: &GraphBudgets,
    clock: &C,
) -> Result<BenchmarkReport, AppError> {
    let dataset = load_dataset(path, limits).map_err(AppError::Ingest)?;
    if !dataset.is_production_success() {
        return Err(AppError::Ingest(
            crate::ingest::IngestError::SemanticValidation {
                violations: dataset.warnings.clone(),
                displayed: dataset.warnings.len(),
                total: dataset.warnings.len(),
            },
        ));
    }
    let bundle = build_graph(&dataset, budgets).map_err(AppError::Graph)?;
    let report = run_benchmark(&bundle, kind, value, warmup, iterations, clock, max_p95_us)
        .map_err(AppError::Query)?;
    if report.threshold_met {
        Ok(report)
    } else {
        Err(AppError::Threshold {
            report: Box::new(report),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::timing::FakeClock;
    use std::path::PathBuf;

    fn tiger() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("tiger_i.json")
    }

    #[test]
    fn fake_clock_phases_round_up() {
        let clock = FakeClock::with_auto_step(1);
        let report = execute_query(
            &tiger(),
            QueryKind::Class,
            "Heavy Tanks",
            &Limits::default(),
            &GraphBudgets::default(),
            &clock,
        )
        .expect("query");
        assert!(report.timing_us.load >= 1);
        assert!(report.timing_us.build >= 1);
        assert!(report.timing_us.query >= 1);
        assert!(report.timing_us.total >= 1);
        assert_eq!(report.result_count, 1);
    }
}
