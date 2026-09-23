//! Query-only samples, nearest-rank p95, and threshold comparison.

use crate::graph::GraphBundle;
use crate::query::{query_class, query_keyword, QueryError, QueryKind};
use crate::timing::Clock;
use serde::Serialize;
use std::hint::black_box;

/// Nanosecond sample statistics converted to microseconds.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct TimingStats {
    pub min: f64,
    pub median: f64,
    pub p95: f64,
    pub max: f64,
}

/// Deterministic-structure benchmark report. Measurements vary.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct BenchmarkReport {
    pub kind: QueryKind,
    pub value: String,
    pub normalized_value: String,
    pub application_version: String,
    pub build_profile: String,
    pub warmup: usize,
    pub iterations: usize,
    pub result_count: usize,
    pub graph_nodes: usize,
    pub graph_edges: usize,
    pub os: String,
    pub arch: String,
    pub timing_us: TimingStats,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub threshold_us: Option<f64>,
    pub threshold_met: bool,
}

/// Resolve the query once, warm up, then record exactly `iterations` samples.
pub fn run_benchmark<C: Clock>(
    bundle: &GraphBundle,
    kind: QueryKind,
    value: &str,
    warmup: usize,
    iterations: usize,
    clock: &C,
    max_p95_us: Option<f64>,
) -> Result<BenchmarkReport, QueryError> {
    let probe = run_once(bundle, kind, value)?;
    let result_count = probe.len();
    black_box(probe);

    for _ in 0..warmup {
        black_box(run_once(bundle, kind, value)?);
    }

    let mut samples_ns = Vec::with_capacity(iterations);
    for _ in 0..iterations {
        let start = clock.now_ns();
        black_box(run_once(bundle, kind, value)?);
        samples_ns.push(clock.now_ns().saturating_sub(start));
    }

    let stats = compute_stats(&samples_ns);
    let threshold_met = max_p95_us.map(|limit| stats.p95 < limit).unwrap_or(true);

    Ok(BenchmarkReport {
        kind,
        value: value.to_owned(),
        normalized_value: crate::identity::identity_key(value),
        application_version: env!("CARGO_PKG_VERSION").to_owned(),
        build_profile: if cfg!(debug_assertions) {
            "debug".to_owned()
        } else {
            "release".to_owned()
        },
        warmup,
        iterations,
        result_count,
        graph_nodes: bundle.graph.node_count(),
        graph_edges: bundle.graph.edge_count(),
        os: std::env::consts::OS.to_owned(),
        arch: std::env::consts::ARCH.to_owned(),
        timing_us: stats,
        threshold_us: max_p95_us,
        threshold_met,
    })
}

fn run_once(
    bundle: &GraphBundle,
    kind: QueryKind,
    value: &str,
) -> Result<Vec<crate::query::TankQueryResult>, QueryError> {
    match kind {
        QueryKind::Class => query_class(bundle, value),
        QueryKind::Keyword => query_keyword(bundle, value),
    }
}

pub(crate) fn compute_stats(samples_ns: &[u128]) -> TimingStats {
    let mut sorted = samples_ns.to_vec();
    sorted.sort_unstable();
    let min = *sorted.first().unwrap_or(&0);
    let max = *sorted.last().unwrap_or(&0);
    TimingStats {
        min: ns_to_us(min),
        median: ns_to_us(median_ns(&sorted)),
        p95: ns_to_us(nearest_rank(&sorted, 0.95)),
        max: ns_to_us(max),
    }
}

fn median_ns(sorted: &[u128]) -> u128 {
    let n = sorted.len();
    if n == 0 {
        return 0;
    }
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        let left = sorted[n / 2 - 1];
        let right = sorted[n / 2];
        left / 2 + right / 2 + (left % 2 + right % 2) / 2
    }
}

/// Nearest-rank percentile: rank = ceil(p * n), 1-indexed.
pub(crate) fn nearest_rank(sorted: &[u128], percentile: f64) -> u128 {
    let n = sorted.len();
    if n == 0 {
        return 0;
    }
    let rank = ((percentile * n as f64).ceil() as usize).clamp(1, n);
    sorted[rank - 1]
}

fn ns_to_us(ns: u128) -> f64 {
    (ns as f64 / 1000.0 * 1000.0).round() / 1000.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::graph::{build_graph, GraphBudgets};
    use crate::ingest::ValidatedDataset;
    use crate::model::TankRecord;
    use crate::timing::FakeClock;

    fn one_tank() -> GraphBundle {
        build_graph(
            &ValidatedDataset {
                records: vec![TankRecord {
                    name: "A01".to_owned(),
                    display_name: "Alpha".to_owned(),
                    class_name: "Heavy Tanks".to_owned(),
                    subclasses: Vec::new(),
                    nation: "USA".to_owned(),
                    tier: 5,
                    strategies: vec!["Hold the ridge.".to_owned()],
                    keywords: vec!["sidescraping".to_owned()],
                }],
                warnings: Vec::new(),
            },
            &GraphBudgets::default(),
        )
        .expect("build")
    }

    #[test]
    fn nearest_rank_p95_uses_ceil() {
        let samples: Vec<u128> = (1..=10).collect();
        assert_eq!(nearest_rank(&samples, 0.95), 10);
        let twenty: Vec<u128> = (1..=20).collect();
        assert_eq!(nearest_rank(&twenty, 0.95), 19);
    }

    #[test]
    fn compute_stats_uses_all_samples() {
        let samples = [1000_u128, 2000, 3000, 4000];
        let stats = compute_stats(&samples);
        assert_eq!(stats.min, 1.0);
        assert_eq!(stats.max, 4.0);
        assert_eq!(stats.median, 2.5);
    }

    #[test]
    fn warmup_is_excluded_from_sample_count() {
        let bundle = one_tank();
        let clock = FakeClock::new();
        clock.advance_ns(0);
        let report = run_benchmark(&bundle, QueryKind::Class, "Heavy Tanks", 2, 3, &clock, None)
            .expect("bench");
        assert_eq!(report.warmup, 2);
        assert_eq!(report.iterations, 3);
        assert_eq!(report.result_count, 1);
    }

    #[test]
    fn threshold_failure_still_returns_report() {
        let bundle = one_tank();
        let clock = FakeClock::with_auto_step(5_000);
        let report = run_benchmark(
            &bundle,
            QueryKind::Keyword,
            "sidescraping",
            1,
            1,
            &clock,
            Some(0.001),
        )
        .expect("bench");
        assert!(!report.threshold_met);
        assert_eq!(report.threshold_us, Some(0.001));
    }
}
