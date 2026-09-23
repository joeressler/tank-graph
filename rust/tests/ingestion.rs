use std::fs::{self, File};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use tank_graph::{load_dataset, IngestError, Limits, TankRecord};

static TEMP_SEQ: AtomicU64 = AtomicU64::new(0);

fn tiger_fixture() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("fixtures")
        .join("tiger_i.json")
}

fn temp_json(contents: impl AsRef<[u8]>) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "tank-graph-ingest-{}-{}.json",
        std::process::id(),
        TEMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = File::create(&path).expect("create temp fixture");
    file.write_all(contents.as_ref())
        .expect("write temp fixture");
    path
}

fn load_bytes(contents: impl AsRef<[u8]>) -> Result<tank_graph::ValidatedDataset, IngestError> {
    let path = temp_json(contents);
    let result = load_dataset(&path, &Limits::default());
    let _ = fs::remove_file(&path);
    result
}

fn load_bytes_with(
    contents: impl AsRef<[u8]>,
    limits: &Limits,
) -> Result<tank_graph::ValidatedDataset, IngestError> {
    let path = temp_json(contents);
    let result = load_dataset(&path, limits);
    let _ = fs::remove_file(&path);
    result
}

fn expect_json(contents: impl AsRef<[u8]>) -> IngestError {
    load_bytes(contents).expect_err("expected JSON or semantic failure")
}

fn expect_semantic(contents: impl AsRef<[u8]>) -> Vec<String> {
    match expect_json(contents) {
        IngestError::SemanticValidation { violations, .. } => violations,
        other => panic!("expected semantic validation, got {other}"),
    }
}

fn wrap_record(body: &str) -> String {
    format!("[{body}]")
}

fn tiger_record() -> &'static str {
    r#"{
        "name": "G04_PzVI_Tiger_I",
        "display_name": "Tiger I",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": [
          "Excels at long-range sniping due to high accuracy.",
          "Hull armor is flat and easily penetrated; rely on sidescraping configurations."
        ],
        "keywords": ["accuracy", "hull armor", "sidescraping", "sniping"]
    }"#
}

fn record_named(name: &str, display_name: &str) -> String {
    format!(
        r#"{{
            "name": "{name}",
            "display_name": "{display_name}",
            "class": "Heavy Tanks",
            "subclasses": [],
            "nation": "Germany",
            "tier": 7,
            "strategies": [],
            "keywords": []
        }}"#
    )
}

#[test]
fn tiger_i_example_loads_with_exact_field_values() {
    let dataset = load_dataset(tiger_fixture(), &Limits::default()).expect("valid fixture");
    assert!(dataset.warnings.is_empty());
    assert_eq!(dataset.records.len(), 1);
    let TankRecord {
        name,
        display_name,
        class_name,
        subclasses,
        nation,
        tier,
        strategies,
        keywords,
    } = &dataset.records[0];
    assert_eq!(name, "G04_PzVI_Tiger_I");
    assert_eq!(display_name, "Tiger I");
    assert_eq!(class_name, "Heavy Tanks");
    assert!(subclasses.is_empty());
    assert_eq!(nation, "Germany");
    assert_eq!(*tier, 7);
    assert_eq!(
        strategies,
        &[
            "Excels at long-range sniping due to high accuracy.",
            "Hull armor is flat and easily penetrated; rely on sidescraping configurations."
        ]
    );
    assert_eq!(
        keywords,
        &["accuracy", "hull armor", "sidescraping", "sniping"]
    );
}

#[test]
fn empty_root_is_warning_not_production_success() {
    let dataset = load_bytes("[]").expect("empty array is syntactically valid");
    assert!(dataset.records.is_empty());
    assert_eq!(dataset.warnings.len(), 1);
    assert!(!dataset.is_production_success());
}

#[test]
fn object_root_is_json_error() {
    assert!(matches!(expect_json("{}"), IngestError::JsonSyntax { .. }));
}

#[test]
fn scalar_root_is_json_error() {
    assert!(matches!(expect_json("1"), IngestError::JsonSyntax { .. }));
}

#[test]
fn two_documents_are_json_error() {
    let payload = format!(
        "{}\n{}",
        wrap_record(tiger_record()),
        wrap_record(tiger_record())
    );
    assert!(matches!(
        expect_json(payload),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn trailing_garbage_is_json_error() {
    assert!(matches!(
        expect_json(format!("{} trailing", wrap_record(tiger_record()))),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn utf8_bom_is_rejected() {
    let mut payload = vec![0xef, 0xbb, 0xbf];
    payload.extend_from_slice(wrap_record(tiger_record()).as_bytes());
    assert!(matches!(
        expect_json(payload),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn invalid_utf8_is_rejected() {
    let mut payload = wrap_record(tiger_record()).into_bytes();
    payload.insert(1, 0xff);
    assert!(matches!(
        expect_json(payload),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn missing_field_is_json_error() {
    let body = r#"{
        "name": "A01",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": []
    }"#;
    assert!(matches!(
        expect_json(wrap_record(body)),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn unknown_field_is_json_error() {
    let body = r#"{
        "name": "A01",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": [],
        "keywords": [],
        "extra": true
    }"#;
    assert!(matches!(
        expect_json(wrap_record(body)),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn null_field_is_json_error() {
    let body = r#"{
        "name": null,
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": [],
        "keywords": []
    }"#;
    assert!(matches!(
        expect_json(wrap_record(body)),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn wrong_type_is_json_error() {
    let body = r#"{
        "name": ["A01"],
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": [],
        "keywords": []
    }"#;
    assert!(matches!(
        expect_json(wrap_record(body)),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn duplicate_key_is_json_error() {
    let body = r#"{
        "name": "A01",
        "name": "A02",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": [],
        "keywords": []
    }"#;
    assert!(matches!(
        expect_json(wrap_record(body)),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn tier_zero_is_semantic_error() {
    let violations = expect_semantic(wrap_record(&record_with_tier(0)));
    assert!(violations.iter().any(|item| item.contains("tier")));
}

#[test]
fn tier_eleven_is_semantic_error() {
    let violations = expect_semantic(wrap_record(&record_with_tier(11)));
    assert!(violations.iter().any(|item| item.contains("tier")));
}

#[test]
fn negative_tier_is_json_error() {
    assert!(matches!(
        expect_json(wrap_record(&record_with_tier_raw("-1"))),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn decimal_tier_is_json_error() {
    assert!(matches!(
        expect_json(wrap_record(&record_with_tier_raw("7.5"))),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn numeric_string_tier_is_json_error() {
    assert!(matches!(
        expect_json(wrap_record(&record_with_tier_raw("\"7\""))),
        IngestError::JsonSyntax { .. }
    ));
}

#[test]
fn overflowing_tier_is_json_error() {
    assert!(matches!(
        expect_json(wrap_record(&record_with_tier_raw("256"))),
        IngestError::JsonSyntax { .. }
    ));
}

fn record_with_tier(tier: u8) -> String {
    record_with_tier_raw(&tier.to_string())
}

fn record_with_tier_raw(tier: &str) -> String {
    format!(
        r#"{{
            "name": "A01_Tank",
            "display_name": "A",
            "class": "Heavy Tanks",
            "subclasses": [],
            "nation": "Germany",
            "tier": {tier},
            "strategies": [],
            "keywords": []
        }}"#
    )
}

#[test]
fn empty_text_is_rejected() {
    let violations = expect_semantic(wrap_record(&record_named("", "Alpha")));
    assert!(violations
        .iter()
        .any(|item| item.contains("must not be blank")));
}

#[test]
fn whitespace_only_text_is_rejected() {
    let violations = expect_semantic(wrap_record(&record_named("   ", "Alpha")));
    assert!(violations
        .iter()
        .any(|item| item.contains("must not be blank")));
}

#[test]
fn leading_or_trailing_space_is_rejected() {
    let violations = expect_semantic(wrap_record(&record_named("A01_Tank", " Alpha")));
    assert!(violations
        .iter()
        .any(|item| item.contains("trimmed") || item.contains("canonical")));
}

#[test]
fn control_character_is_rejected() {
    let body = record_named("A01_Tank", "Alpha\\u0007");
    let violations = expect_semantic(wrap_record(&body));
    assert!(violations
        .iter()
        .any(|item| item.contains("control character")));
}

#[test]
fn non_nfkc_text_is_rejected() {
    let body = record_named("A01_Tank", "cafe\u{0301}");
    let violations = expect_semantic(wrap_record(&body));
    assert!(violations
        .iter()
        .any(|item| item.contains("canonical emitted form")));
}

#[test]
fn unsorted_records_are_rejected() {
    let payload = format!(
        "[{}, {}]",
        record_named("B01_Tank", "Bravo"),
        record_named("A01_Tank", "Alpha")
    );
    let violations = expect_semantic(payload);
    assert!(violations
        .iter()
        .any(|item| item.contains("not strictly sorted")));
}

#[test]
fn unsorted_keywords_are_rejected() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": ["Hold the ridge."],
        "keywords": ["sniping", "accuracy"]
    }"#;
    let violations = expect_semantic(wrap_record(body));
    assert!(violations
        .iter()
        .any(|item| item.contains("not strictly sorted")));
}

#[test]
fn unsorted_strategies_are_rejected() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": ["Zulu first.", "Alpha later."],
        "keywords": ["alpha later", "zulu first"]
    }"#;
    let violations = expect_semantic(wrap_record(body));
    assert!(violations
        .iter()
        .any(|item| item.contains("not strictly sorted")));
}

#[test]
fn unsorted_subclasses_are_rejected() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": ["Scouts", "Autoloaders"],
        "nation": "Germany",
        "tier": 7,
        "strategies": [],
        "keywords": []
    }"#;
    let violations = expect_semantic(wrap_record(body));
    assert!(violations
        .iter()
        .any(|item| item.contains("not strictly sorted")));
}

#[test]
fn normalized_duplicate_tank_names_are_rejected() {
    let payload = format!(
        "[{}, {}]",
        record_named("Tiger", "Tiger"),
        record_named("tiger", "Tiger")
    );
    let violations = expect_semantic(payload);
    assert!(violations
        .iter()
        .any(|item| item.contains("normalized duplicate")));
}

#[test]
fn exact_repeated_record_is_rejected() {
    let payload = format!(
        "[{}, {}]",
        record_named("A01_Tank", "A"),
        record_named("A01_Tank", "A")
    );
    let violations = expect_semantic(payload);
    assert!(violations
        .iter()
        .any(|item| item.contains("duplicate") || item.contains("sorted")));
}

#[test]
fn normalized_list_duplicates_are_rejected() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": ["Hold the ridge."],
        "keywords": ["\u03c3", "\u03c2"]
    }"#;
    let violations = expect_semantic(wrap_record(body));
    assert!(violations
        .iter()
        .any(|item| item.contains("normalized duplicate")));
}

#[test]
fn oversized_file_is_rejected() {
    let limits = Limits {
        maximum_bytes: 32,
        ..Limits::default()
    };
    let payload = wrap_record(tiger_record());
    assert!(payload.len() as u64 > limits.maximum_bytes);
    assert!(matches!(
        load_bytes_with(payload, &limits),
        Err(IngestError::FileTooLarge { .. })
    ));
}

#[test]
fn too_many_records_are_rejected() {
    let limits = Limits {
        maximum_records: 1,
        ..Limits::default()
    };
    let payload = format!(
        "[{}, {}]",
        record_named("A01_Tank", "A"),
        record_named("B01_Tank", "B")
    );
    match load_bytes_with(payload, &limits) {
        Err(IngestError::SemanticValidation { violations, .. }) => {
            assert!(violations
                .iter()
                .any(|item| item.contains("exceeds 1 records")));
        }
        other => panic!("expected record-limit violation, got {other:?}"),
    }
}

#[test]
fn too_many_keywords_are_rejected() {
    let limits = Limits {
        maximum_keywords: 1,
        ..Limits::default()
    };
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": ["Hold the ridge."],
        "keywords": ["accuracy", "sniping"]
    }"#;
    match load_bytes_with(wrap_record(body), &limits) {
        Err(IngestError::SemanticValidation { violations, .. }) => {
            assert!(violations
                .iter()
                .any(|item| item.contains("exceeds 1 items")));
        }
        other => panic!("expected keyword-limit violation, got {other:?}"),
    }
}

#[test]
fn overlong_label_is_rejected() {
    let name = "A".repeat(121);
    let violations = expect_semantic(wrap_record(&record_named(&name, "Alpha")));
    assert!(violations
        .iter()
        .any(|item| item.contains("Unicode scalars")));
}

#[test]
fn overlong_strategy_is_rejected() {
    let strategy = "A".repeat(1001);
    let body = format!(
        r#"{{
            "name": "A01_Tank",
            "display_name": "A",
            "class": "Heavy Tanks",
            "subclasses": [],
            "nation": "Germany",
            "tier": 7,
            "strategies": ["{strategy}"],
            "keywords": ["aaaa"]
        }}"#
    );
    let violations = expect_semantic(wrap_record(&body));
    assert!(violations
        .iter()
        .any(|item| item.contains("Unicode scalars")));
}

#[test]
fn violation_list_is_capped_with_total_count() {
    let limits = Limits {
        maximum_displayed_violations: 2,
        ..Limits::default()
    };
    let body = r#"{
        "name": "",
        "display_name": "",
        "class": "Unknown",
        "subclasses": [],
        "nation": "",
        "tier": 0,
        "strategies": [],
        "keywords": []
    }"#;
    match load_bytes_with(wrap_record(body), &limits) {
        Err(IngestError::SemanticValidation {
            violations,
            displayed,
            total,
        }) => {
            assert_eq!(displayed, 2);
            assert_eq!(violations.len(), 2);
            assert!(total > displayed);
        }
        other => panic!("expected capped semantic report, got {other:?}"),
    }
}

#[test]
fn patch_note_arrows_are_not_markup() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "USA",
        "tier": 7,
        "strategies": ["Reload from 9.59 -> 8.44 and matchmaking VI->V."],
        "keywords": ["matchmaking", "reload"]
    }"#;
    let dataset = load_bytes(wrap_record(body)).expect("arrow notation is valid strategy text");
    assert_eq!(dataset.records[0].strategies.len(), 1);
}

#[test]
fn leftover_markup_in_strategies_is_rejected() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "USA",
        "tier": 7,
        "strategies": ["See [[Tiger I]] and <ref>patch notes</ref>."],
        "keywords": ["patch notes", "tiger"]
    }"#;
    let violations = expect_semantic(wrap_record(body));
    assert!(violations
        .iter()
        .any(|item| item.contains("contains markup")));
}

#[test]
fn primary_class_subclass_is_rejected() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": ["heavy tanks"],
        "nation": "Germany",
        "tier": 7,
        "strategies": [],
        "keywords": []
    }"#;
    let violations = expect_semantic(wrap_record(body));
    assert!(violations
        .iter()
        .any(|item| item.contains("primary class is not a subclass")));
}

#[test]
fn keywords_without_strategies_are_rejected() {
    let body = r#"{
        "name": "A01_Tank",
        "display_name": "A",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": [],
        "keywords": ["sniping"]
    }"#;
    let violations = expect_semantic(wrap_record(body));
    assert!(violations
        .iter()
        .any(|item| item.contains("must be empty when strategies is empty")));
}

#[test]
fn repeated_display_names_are_allowed() {
    let payload = format!(
        "[{}, {}]",
        record_named("A01_Tank", "Shared"),
        record_named("B01_Tank", "Shared")
    );
    let dataset = load_bytes(payload).expect("shared display names are valid");
    assert_eq!(dataset.records.len(), 2);
}

#[test]
fn missing_path_is_not_found() {
    let missing = PathBuf::from("definitely-missing-tank-graph-dataset.json");
    assert!(matches!(
        load_dataset(&missing, &Limits::default()),
        Err(IngestError::PathNotFound { .. })
    ));
}

#[test]
fn directory_path_is_not_regular_file() {
    let dir = std::env::temp_dir().join(format!(
        "tank-graph-dir-{}-{}",
        std::process::id(),
        TEMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    fs::create_dir_all(&dir).expect("temp dir");
    let result = load_dataset(&dir, &Limits::default());
    let _ = fs::remove_dir_all(&dir);
    assert!(matches!(result, Err(IngestError::NotRegularFile { .. })));
}

#[test]
fn unreadable_file_is_open_failure() {
    let path = temp_json(wrap_record(tiger_record()));
    let result = load_unreadable(&path);
    let _ = fs::remove_file(&path);
    assert!(
        matches!(result, Err(IngestError::OpenFailed { .. })),
        "expected open failure, got {result:?}"
    );
}

#[cfg(windows)]
fn load_unreadable(path: &Path) -> Result<tank_graph::ValidatedDataset, IngestError> {
    use std::os::windows::fs::OpenOptionsExt;
    let _hold = std::fs::OpenOptions::new()
        .read(true)
        .share_mode(0)
        .open(path)
        .expect("exclusive lock");
    load_dataset(path, &Limits::default())
}

#[cfg(unix)]
fn load_unreadable(path: &Path) -> Result<tank_graph::ValidatedDataset, IngestError> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = fs::metadata(path).expect("metadata").permissions();
    permissions.set_mode(0o000);
    fs::set_permissions(path, permissions).expect("chmod");
    load_dataset(path, &Limits::default())
}

#[test]
fn binary_reports_success_usage_and_ingest_failures() {
    let binary = env!("CARGO_BIN_EXE_tank-graph");
    let data = tiger_fixture();
    let data = data.to_str().expect("utf-8 path");
    let ok = Command::new(binary)
        .args([
            "--data",
            data,
            "--format",
            "text",
            "query",
            "class",
            "--name",
            "Heavy Tanks",
        ])
        .output()
        .expect("run success");
    assert!(ok.status.success(), "{ok:?}");
    assert!(String::from_utf8_lossy(&ok.stdout).contains("Tiger I"));

    let usage = Command::new(binary)
        .args(["--unknown"])
        .output()
        .expect("run usage");
    assert_eq!(usage.status.code(), Some(2));

    let empty = temp_json("[]");
    let failed = Command::new(binary)
        .args([
            "--data",
            empty.to_str().expect("utf-8 path"),
            "--format",
            "text",
            "query",
            "class",
            "--name",
            "Heavy Tanks",
        ])
        .output()
        .expect("run empty");
    let _ = fs::remove_file(&empty);
    assert_eq!(failed.status.code(), Some(3));
}
