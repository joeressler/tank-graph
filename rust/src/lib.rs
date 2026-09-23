#![forbid(unsafe_code)]

pub mod benchmark;
pub mod engine;
pub mod graph;
pub mod identity;
pub mod ingest;
pub mod model;
pub mod query;
pub mod report;
pub mod timing;

pub use benchmark::{run_benchmark, BenchmarkReport};
pub use graph::{build_graph, GraphBudgets, GraphBundle, GraphError, GraphStatistics};
pub use ingest::{load_dataset, IngestError, Limits, ValidatedDataset};
pub use model::{EdgeType, NodeType, TankGraph, TankRecord};
pub use petgraph::graph::NodeIndex;
pub use query::{query_class, query_keyword, QueryError, QueryKind, TankQueryResult};
pub use report::{AppError, QueryReport};
