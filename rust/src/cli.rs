//! Non-interactive clap entry point. Query logic stays in the library.

use clap::error::ErrorKind;
use clap::{Args, Parser, Subcommand, ValueEnum};
use std::path::PathBuf;
use std::process::ExitCode;
use tank_graph::engine::{execute_benchmark, execute_query};
use tank_graph::graph::GraphBudgets;
use tank_graph::ingest::Limits;
use tank_graph::query::QueryKind;
use tank_graph::report::{
    format_benchmark_json, format_benchmark_text, format_error_json, format_query_json,
    format_query_text, write_stderr, write_stdout, AppError,
};
use tank_graph::timing::InstantClock;

const CLASS_EXAMPLE: &str =
    "tank-graph --data data/tanks_data.json --format text query class --name \"Heavy Tanks\"";
const KEYWORD_EXAMPLE: &str =
    "tank-graph --data data/tanks_data.json --format json query keyword --name \"sidescraping\"";
#[allow(dead_code)]
const BENCH_CLASS_EXAMPLE: &str = "tank-graph --data data/tanks_data.json --format json benchmark class --name \"Heavy Tanks\" --warmup 1000 --iterations 10000 --max-p95-us 1000";
#[allow(dead_code)]
const BENCH_KEYWORD_EXAMPLE: &str = "tank-graph --data data/tanks_data.json --format json benchmark keyword --name \"sidescraping\" --warmup 1000 --iterations 10000 --max-p95-us 1000";

#[derive(Clone, Copy, Debug, ValueEnum)]
enum OutputFormat {
    Text,
    Json,
}

#[derive(Parser, Debug)]
#[command(
    name = "tank-graph",
    version,
    about = "Query and benchmark a directed tank knowledge graph.",
    disable_help_subcommand = true
)]
struct Cli {
    /// Path to a schema-valid tanks_data.json file.
    #[arg(long, global = true)]
    data: Option<PathBuf>,
    /// Successful output format.
    #[arg(long, global = true, value_enum)]
    format: Option<OutputFormat>,
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Exact class membership or two-hop keyword queries.
    #[command(
        after_help = "Examples:\n  tank-graph --data data/tanks_data.json --format text query class --name \"Heavy Tanks\"\n  tank-graph --data data/tanks_data.json --format json query keyword --name \"sidescraping\""
    )]
    Query {
        #[command(subcommand)]
        target: QueryTarget,
    },
    /// Query-only timing on an already built graph.
    #[command(
        after_help = "Examples:\n  tank-graph --data data/tanks_data.json --format json benchmark class --name \"Heavy Tanks\" --warmup 1000 --iterations 10000 --max-p95-us 1000"
    )]
    Benchmark {
        #[command(subcommand)]
        target: BenchTarget,
    },
}

#[derive(Subcommand, Debug)]
enum QueryTarget {
    /// Incoming IsAClassOf membership only.
    #[command(
        after_help = "Examples:\n  tank-graph --data data/tanks_data.json --format text query class --name \"Heavy Tanks\""
    )]
    Class(NameArgs),
    /// Keyword <- Strategy <- Tank traversal.
    #[command(
        after_help = "Examples:\n  tank-graph --data data/tanks_data.json --format json query keyword --name \"sidescraping\""
    )]
    Keyword(NameArgs),
}

#[derive(Subcommand, Debug)]
enum BenchTarget {
    #[command(
        after_help = "Examples:\n  tank-graph --data data/tanks_data.json --format json benchmark class --name \"Heavy Tanks\" --warmup 1000 --iterations 10000 --max-p95-us 1000"
    )]
    Class(BenchArgs),
    #[command(
        after_help = "Examples:\n  tank-graph --data data/tanks_data.json --format json benchmark keyword --name \"sidescraping\" --warmup 1000 --iterations 10000 --max-p95-us 1000"
    )]
    Keyword(BenchArgs),
}

#[derive(Args, Debug)]
struct NameArgs {
    #[arg(long, value_parser = parse_query_name)]
    name: String,
}

#[derive(Args, Debug)]
struct BenchArgs {
    #[arg(long, value_parser = parse_query_name)]
    name: String,
    #[arg(long, default_value_t = 1000, value_parser = clap::value_parser!(u64).range(1..=1_000_000))]
    warmup: u64,
    #[arg(long, default_value_t = 10_000, value_parser = clap::value_parser!(u64).range(1..=10_000_000))]
    iterations: u64,
    #[arg(long)]
    max_p95_us: Option<u64>,
}

fn parse_query_name(value: &str) -> Result<String, String> {
    if value.trim().is_empty() {
        Err("name must not be empty or whitespace-only".to_owned())
    } else {
        Ok(value.to_owned())
    }
}

pub fn run() -> ExitCode {
    let json = format_requested_json(std::env::args());
    match Cli::try_parse() {
        Ok(cli) => match dispatch(cli) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => emit_error(error, json),
        },
        Err(err) => clap_failure(err, json),
    }
}

fn dispatch(cli: Cli) -> Result<(), AppError> {
    let data = cli
        .data
        .ok_or_else(|| AppError::Usage(format!("--data is required. Example: {CLASS_EXAMPLE}")))?;
    let format = cli.format.ok_or_else(|| {
        AppError::Usage(format!(
            "--format is required. Example: {CLASS_EXAMPLE} or {KEYWORD_EXAMPLE}"
        ))
    })?;
    let clock = InstantClock::new();
    match cli.command {
        Commands::Query { target } => {
            let (kind, name) = match target {
                QueryTarget::Class(args) => (QueryKind::Class, args.name),
                QueryTarget::Keyword(args) => (QueryKind::Keyword, args.name),
            };
            let report = execute_query(
                &data,
                kind,
                &name,
                &Limits::default(),
                &GraphBudgets::default(),
                &clock,
            )?;
            let body = match format {
                OutputFormat::Json => format_query_json(&report).expect("json report"),
                OutputFormat::Text => format_query_text(&report),
            };
            write_stdout(&body).ok();
            Ok(())
        }
        Commands::Benchmark { target } => {
            let (kind, args) = match target {
                BenchTarget::Class(args) => (QueryKind::Class, args),
                BenchTarget::Keyword(args) => (QueryKind::Keyword, args),
            };
            match execute_benchmark(
                &data,
                kind,
                &args.name,
                usize::try_from(args.warmup).unwrap_or(1),
                usize::try_from(args.iterations).unwrap_or(1),
                args.max_p95_us.map(|v| v as f64),
                &Limits::default(),
                &GraphBudgets::default(),
                &clock,
            ) {
                Ok(report) => {
                    write_stdout(&benchmark_body(format, &report)).ok();
                    Ok(())
                }
                Err(AppError::Threshold { report }) => {
                    write_stdout(&benchmark_body(format, &report)).ok();
                    Err(AppError::Threshold { report })
                }
                Err(other) => Err(other),
            }
        }
    }
}

fn benchmark_body(format: OutputFormat, report: &tank_graph::benchmark::BenchmarkReport) -> String {
    match format {
        OutputFormat::Json => format_benchmark_json(report).expect("json report"),
        OutputFormat::Text => format_benchmark_text(report),
    }
}

fn emit_error(error: AppError, json: bool) -> ExitCode {
    let code = error.exit_code();
    if json {
        if let Ok(body) = format_error_json(&error.to_error_report()) {
            let _ = write_stderr(&body);
        }
    } else {
        let _ = write_stderr(&error.to_string());
    }
    ExitCode::from(code)
}

fn clap_failure(err: clap::Error, json: bool) -> ExitCode {
    match err.kind() {
        ErrorKind::DisplayHelp | ErrorKind::DisplayVersion => {
            let _ = err.print();
            ExitCode::SUCCESS
        }
        _ => {
            if json {
                let error = AppError::Usage(err.to_string());
                emit_error(error, true)
            } else {
                let _ = err.print();
                ExitCode::from(2)
            }
        }
    }
}

fn format_requested_json<I>(args: I) -> bool
where
    I: IntoIterator<Item = String>,
{
    let args: Vec<String> = args.into_iter().collect();
    let mut index = 0;
    while index < args.len() {
        if args[index] == "--format" {
            return args.get(index + 1).is_some_and(|value| value == "json");
        }
        if let Some(value) = args[index].strip_prefix("--format=") {
            return value == "json";
        }
        index += 1;
    }
    false
}
