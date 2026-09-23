use std::path::{Path, PathBuf};
use std::process::Command;

fn fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("benchmark_tanks_data.json")
}

fn run_gate(kind: &str, name: &str) {
    let output = Command::new(env!("CARGO_BIN_EXE_tank-graph"))
        .args([
            "--data",
            fixture().to_str().expect("utf-8"),
            "--format",
            "json",
            "benchmark",
            kind,
            "--name",
            name,
            "--warmup",
            "1000",
            "--iterations",
            "10000",
            "--max-p95-us",
            "1000",
        ])
        .output()
        .expect("benchmark gate");
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let code = output.status.code();
    assert!(
        code == Some(0),
        "p95 gate failed ({code:?})\nstdout:\n{stdout}\nstderr:\n{stderr}"
    );
}

#[test]
#[cfg_attr(debug_assertions, ignore = "release-mode performance gate")]
fn fixture_class_p95_under_1000us() {
    run_gate("class", "Heavy Tanks");
}

#[test]
#[cfg_attr(debug_assertions, ignore = "release-mode performance gate")]
fn fixture_keyword_p95_under_1000us() {
    run_gate("keyword", "sidescraping");
}
