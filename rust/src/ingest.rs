//! Bounded file loading and semantic validation of the interchange array.

use crate::identity::{canonical_emitted, case_fold, identity_key, sort_key};
use crate::model::TankRecord;
use serde::de::{Deserializer, SeqAccess, Visitor};
use std::collections::HashMap;
use std::fmt;
use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::{Path, PathBuf};

const PRIMARY_CLASSES: &[&str] = &[
    "Light Tanks",
    "Medium Tanks",
    "Heavy Tanks",
    "Tank Destroyers",
    "SPGs",
];
const MAX_JSON_MESSAGE: usize = 200;

/// Canonical primary-class labels after Milestone 2 validation.
pub(crate) fn is_primary_class(label: &str) -> bool {
    PRIMARY_CLASSES.contains(&label)
}

/// Caller-supplied resource budgets. Production defaults match the milestone spec.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Limits {
    pub maximum_bytes: u64,
    pub maximum_records: usize,
    pub maximum_subclasses: usize,
    pub maximum_strategies: usize,
    pub maximum_keywords: usize,
    pub maximum_label_scalars: usize,
    pub maximum_strategy_scalars: usize,
    pub maximum_displayed_violations: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            maximum_bytes: 256 * 1024 * 1024,
            maximum_records: 10_000,
            maximum_subclasses: 100,
            maximum_strategies: 1_000,
            maximum_keywords: 1_000,
            maximum_label_scalars: 120,
            maximum_strategy_scalars: 1_000,
            maximum_displayed_violations: 32,
        }
    }
}

/// Successfully deserialized and semantically validated records.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedDataset {
    pub records: Vec<TankRecord>,
    pub warnings: Vec<String>,
}

impl ValidatedDataset {
    pub fn is_production_success(&self) -> bool {
        self.warnings.is_empty()
    }
}

/// Typed ingestion failure with safe context only.
#[derive(Debug)]
pub enum IngestError {
    PathNotFound {
        path: PathBuf,
    },
    OpenFailed {
        path: PathBuf,
        source: io::Error,
    },
    NotRegularFile {
        path: PathBuf,
    },
    FileTooLarge {
        path: PathBuf,
        observed_bytes: u64,
        maximum_bytes: u64,
    },
    JsonSyntax {
        line: usize,
        column: usize,
        message: String,
    },
    SemanticValidation {
        violations: Vec<String>,
        displayed: usize,
        total: usize,
    },
}

impl IngestError {
    fn from_io(path: &Path, source: io::Error) -> Self {
        if source.kind() == io::ErrorKind::NotFound {
            Self::PathNotFound {
                path: path.to_path_buf(),
            }
        } else {
            Self::OpenFailed {
                path: path.to_path_buf(),
                source,
            }
        }
    }

    fn json(error: serde_json::Error) -> Self {
        Self::JsonSyntax {
            line: error.line(),
            column: error.column(),
            message: truncate_message(&error.to_string()),
        }
    }
}

impl fmt::Display for IngestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::PathNotFound { path } => {
                write!(f, "path not found: {}", path.display())
            }
            Self::OpenFailed { path, source } => {
                write!(f, "failed to open {}: {source}", path.display())
            }
            Self::NotRegularFile { path } => {
                write!(f, "not a regular file: {}", path.display())
            }
            Self::FileTooLarge {
                path,
                observed_bytes,
                maximum_bytes,
            } => write!(
                f,
                "{} exceeds byte limit ({observed_bytes} > {maximum_bytes})",
                path.display()
            ),
            Self::JsonSyntax {
                line,
                column,
                message,
            } => write!(f, "JSON error at {line}:{column}: {message}"),
            Self::SemanticValidation {
                violations,
                displayed,
                total,
            } => {
                write!(f, "semantic validation failed ({total} violations)")?;
                if *total > *displayed {
                    write!(f, ", showing {displayed} of {total}")?;
                }
                for violation in violations {
                    write!(f, "\n  {violation}")?;
                }
                Ok(())
            }
        }
    }
}

impl std::error::Error for IngestError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::OpenFailed { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// Load one caller-supplied JSON array and validate it independently of the producer.
pub fn load_dataset(
    path: impl AsRef<Path>,
    limits: &Limits,
) -> Result<ValidatedDataset, IngestError> {
    let path = path.as_ref();
    if !path.exists() {
        return Err(IngestError::PathNotFound {
            path: path.to_path_buf(),
        });
    }

    let metadata = fs_metadata(path)?;
    if !metadata.is_file() {
        return Err(IngestError::NotRegularFile {
            path: path.to_path_buf(),
        });
    }
    if metadata.len() > limits.maximum_bytes {
        return Err(IngestError::FileTooLarge {
            path: path.to_path_buf(),
            observed_bytes: metadata.len(),
            maximum_bytes: limits.maximum_bytes,
        });
    }

    let file = File::open(path).map_err(|source| IngestError::from_io(path, source))?;
    load_from_file(path, file, limits)
}

fn fs_metadata(path: &Path) -> Result<std::fs::Metadata, IngestError> {
    std::fs::metadata(path).map_err(|source| IngestError::from_io(path, source))
}

fn load_from_file(
    path: &Path,
    file: File,
    limits: &Limits,
) -> Result<ValidatedDataset, IngestError> {
    let mut bounded = BoundedReader::new(file, limits.maximum_bytes);
    let parse_result = deserialize_records(&mut bounded);
    if bounded.consumed > limits.maximum_bytes {
        return Err(IngestError::FileTooLarge {
            path: path.to_path_buf(),
            observed_bytes: bounded.consumed,
            maximum_bytes: limits.maximum_bytes,
        });
    }
    let records = parse_result?;
    validate_dataset(records, limits)
}

fn deserialize_records<R: Read>(reader: R) -> Result<Vec<TankRecord>, IngestError> {
    let buffered = BufReader::new(reader);
    let mut deserializer = serde_json::Deserializer::from_reader(buffered);
    let records = deserializer
        .deserialize_seq(RecordSeqVisitor)
        .map_err(IngestError::json)?;
    deserializer.end().map_err(IngestError::json)?;
    Ok(records)
}

struct RecordSeqVisitor;

impl<'de> Visitor<'de> for RecordSeqVisitor {
    type Value = Vec<TankRecord>;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a root JSON array of tank records")
    }

    fn visit_seq<A>(self, mut seq: A) -> Result<Self::Value, A::Error>
    where
        A: SeqAccess<'de>,
    {
        // Grow from empty so untrusted JSON size hints cannot reserve memory.
        let mut records = Vec::new();
        while let Some(record) = seq.next_element()? {
            records.push(record);
        }
        Ok(records)
    }
}

#[cfg(test)]
pub(crate) fn load_from_reader<R: Read>(
    path: &Path,
    reader: R,
    limits: &Limits,
) -> Result<ValidatedDataset, IngestError> {
    let mut bounded = BoundedReader::new(reader, limits.maximum_bytes);
    let parse_result = deserialize_records(&mut bounded);
    if bounded.consumed > limits.maximum_bytes {
        return Err(IngestError::FileTooLarge {
            path: path.to_path_buf(),
            observed_bytes: bounded.consumed,
            maximum_bytes: limits.maximum_bytes,
        });
    }
    let records = parse_result?;
    validate_dataset(records, limits)
}

/// Reads at most `maximum_bytes + 1` so a growing file cannot bypass the limit.
pub(crate) struct BoundedReader<R> {
    inner: R,
    remaining: u64,
    pub consumed: u64,
}

impl<R: Read> BoundedReader<R> {
    pub(crate) fn new(inner: R, maximum_bytes: u64) -> Self {
        Self {
            inner,
            remaining: maximum_bytes.saturating_add(1),
            consumed: 0,
        }
    }
}

impl<R: Read> Read for BoundedReader<R> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.remaining == 0 || buf.is_empty() {
            return Ok(0);
        }
        let allowed = usize::try_from(self.remaining)
            .unwrap_or(buf.len())
            .min(buf.len());
        let read = self.inner.read(&mut buf[..allowed])?;
        let read_u64 = u64::try_from(read).expect("read length fits u64");
        self.remaining -= read_u64;
        self.consumed += read_u64;
        Ok(read)
    }
}

fn validate_dataset(
    records: Vec<TankRecord>,
    limits: &Limits,
) -> Result<ValidatedDataset, IngestError> {
    let mut violations = Vec::new();
    if records.len() > limits.maximum_records {
        violations.push(Violation {
            path: JsonPath::root(),
            message: format!("exceeds {} records", limits.maximum_records),
        });
    }

    let mut seen_names = HashMap::new();
    let mut previous_name_key: Option<(String, String)> = None;
    let primary_identities: Vec<String> = PRIMARY_CLASSES
        .iter()
        .map(|label| identity_key(label))
        .collect();

    for (record_number, record) in records.iter().enumerate() {
        let path = JsonPath::record(record_number);
        validate_text(
            &record.name,
            path.field("name"),
            limits.maximum_label_scalars,
            TextKind::Name,
            &mut violations,
        );
        validate_text(
            &record.display_name,
            path.field("display_name"),
            limits.maximum_label_scalars,
            TextKind::Human,
            &mut violations,
        );
        validate_text(
            &record.class_name,
            path.field("class"),
            limits.maximum_label_scalars,
            TextKind::Human,
            &mut violations,
        );
        validate_text(
            &record.nation,
            path.field("nation"),
            limits.maximum_label_scalars,
            TextKind::Human,
            &mut violations,
        );

        if !(1..=10).contains(&record.tier) {
            violations.push(Violation {
                path: path.field("tier"),
                message: "expected integer from 1 through 10".to_owned(),
            });
        }
        if !is_primary_class(&record.class_name) {
            violations.push(Violation {
                path: path.field("class"),
                message: "unknown primary class".to_owned(),
            });
        }

        validate_repeated(
            &record.subclasses,
            path.field("subclasses"),
            limits.maximum_label_scalars,
            limits.maximum_subclasses,
            TextKind::Human,
            &mut violations,
        );
        validate_repeated(
            &record.strategies,
            path.field("strategies"),
            limits.maximum_strategy_scalars,
            limits.maximum_strategies,
            TextKind::Strategy,
            &mut violations,
        );
        validate_repeated(
            &record.keywords,
            path.field("keywords"),
            limits.maximum_label_scalars,
            limits.maximum_keywords,
            TextKind::Keyword,
            &mut violations,
        );

        for (index, subclass) in record.subclasses.iter().enumerate() {
            if primary_identities
                .iter()
                .any(|identity| identity == &identity_key(subclass))
            {
                violations.push(Violation {
                    path: path.field("subclasses").index(index),
                    message: "primary class is not a subclass".to_owned(),
                });
            }
        }

        if record.strategies.is_empty() && !record.keywords.is_empty() {
            violations.push(Violation {
                path: path.field("keywords"),
                message: "must be empty when strategies is empty".to_owned(),
            });
        }

        let tank_identity = identity_key(&record.name);
        let tank_sort = sort_key(&record.name);
        if let Some(previous) = seen_names.get(&tank_identity) {
            violations.push(Violation {
                path: path.field("name"),
                message: format!("normalized duplicate of {previous}"),
            });
        } else {
            seen_names.insert(tank_identity, format!("{}.name", path.as_str()));
        }
        if let Some(previous) = &previous_name_key {
            if tank_sort <= *previous {
                violations.push(Violation {
                    path: path.field("name"),
                    message: "records are not strictly sorted".to_owned(),
                });
            }
        }
        previous_name_key = Some(tank_sort);
    }

    if !violations.is_empty() {
        return Err(semantic_error(
            violations,
            limits.maximum_displayed_violations,
        ));
    }

    let mut warnings = Vec::new();
    if records.is_empty() {
        warnings.push("empty root array is not a successful production extraction".to_owned());
    }

    Ok(ValidatedDataset { records, warnings })
}

#[derive(Clone, Copy)]
enum TextKind {
    Name,
    Human,
    Strategy,
    Keyword,
}

fn validate_repeated(
    values: &[String],
    path: JsonPath,
    item_limit: usize,
    max_items: usize,
    kind: TextKind,
    violations: &mut Vec<Violation>,
) {
    if values.len() > max_items {
        violations.push(Violation {
            path,
            message: format!("exceeds {max_items} items"),
        });
    }

    let mut identities = HashMap::new();
    let mut previous: Option<(String, String)> = None;
    for (index, value) in values.iter().enumerate() {
        let item_path = path.index(index);
        validate_text(value, item_path, item_limit, kind, violations);
        let identity = identity_key(value);
        if let Some(first) = identities.get(&identity) {
            violations.push(Violation {
                path: item_path,
                message: format!("normalized duplicate of {}[{first}]", path.as_str()),
            });
        } else {
            identities.insert(identity, index);
        }
        let value_sort = sort_key(value);
        if let Some(previous_key) = &previous {
            if value_sort <= *previous_key {
                violations.push(Violation {
                    path: item_path,
                    message: "array is not strictly sorted".to_owned(),
                });
            }
        }
        previous = Some(value_sort);
    }
}

fn validate_text(
    value: &str,
    path: JsonPath,
    limit: usize,
    kind: TextKind,
    violations: &mut Vec<Violation>,
) {
    if value.is_empty() || value.trim().is_empty() {
        violations.push(Violation {
            path,
            message: "must not be blank".to_owned(),
        });
    }
    if value != value.trim() {
        violations.push(Violation {
            path,
            message: "must already equal its trimmed form".to_owned(),
        });
    }
    if value != canonical_emitted(value) {
        violations.push(Violation {
            path,
            message: "text is not in canonical emitted form".to_owned(),
        });
    }
    if value.chars().count() > limit {
        violations.push(Violation {
            path,
            message: format!("exceeds {limit} Unicode scalars"),
        });
    }
    if has_disallowed_control(value) {
        violations.push(Violation {
            path,
            message: "contains a disallowed control character".to_owned(),
        });
    }

    match kind {
        TextKind::Name => {
            if !is_stable_identifier(value) {
                violations.push(Violation {
                    path,
                    message: "invalid stable identifier".to_owned(),
                });
            }
        }
        TextKind::Human | TextKind::Strategy => {
            if contains_markup(value) {
                violations.push(Violation {
                    path,
                    message: "contains markup".to_owned(),
                });
            }
        }
        TextKind::Keyword => {
            if value
                .chars()
                .any(|character| matches!(character, '\r' | '\n' | '\t'))
            {
                violations.push(Violation {
                    path,
                    message: "must not contain a line break or tab".to_owned(),
                });
            }
            if value != case_fold(value) {
                violations.push(Violation {
                    path,
                    message: "keyword must be lowercase".to_owned(),
                });
            }
            if contains_markup(value) {
                violations.push(Violation {
                    path,
                    message: "contains markup".to_owned(),
                });
            }
        }
    }
}

fn is_stable_identifier(value: &str) -> bool {
    let mut characters = value.chars();
    match characters.next() {
        Some(first) if first.is_ascii_alphanumeric() => {}
        _ => return false,
    }
    characters
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '_' | '.' | '-'))
}

fn has_disallowed_control(value: &str) -> bool {
    value
        .chars()
        .any(|character| character.is_control() && !matches!(character, '\r' | '\n' | '\t'))
}

fn contains_markup(value: &str) -> bool {
    if value.contains('`')
        || value.contains("[[")
        || value.contains("]]")
        || value.contains("{{")
        || value.contains("}}")
    {
        return true;
    }
    // Patch-note arrows such as "9.59 -> 8.44" are ordinary punctuation.
    // Only tag-like sequences count as leftover HTML or wiki markup.
    let bytes = value.as_bytes();
    bytes.windows(2).any(|pair| {
        pair[0] == b'<' && matches!(pair[1], b'A'..=b'Z' | b'a'..=b'z' | b'/' | b'!' | b'?')
    })
}

fn semantic_error(mut violations: Vec<Violation>, maximum_displayed: usize) -> IngestError {
    violations.sort_by_key(|left| left.path);
    let total = violations.len();
    let displayed = total.min(maximum_displayed);
    IngestError::SemanticValidation {
        violations: violations
            .into_iter()
            .take(displayed)
            .map(|violation| format!("{}: {}", violation.path.as_str(), violation.message))
            .collect(),
        displayed,
        total,
    }
}

fn truncate_message(message: &str) -> String {
    let mut truncated = String::new();
    for character in message.chars() {
        if truncated.chars().count() >= MAX_JSON_MESSAGE {
            truncated.push('…');
            break;
        }
        truncated.push(character);
    }
    truncated
}

struct Violation {
    path: JsonPath,
    message: String,
}

#[derive(Clone, Copy, PartialEq, Eq)]
struct JsonPath {
    record: Option<usize>,
    field: Option<&'static str>,
    index: Option<usize>,
}

impl JsonPath {
    fn root() -> Self {
        Self {
            record: None,
            field: None,
            index: None,
        }
    }

    fn record(record: usize) -> Self {
        Self {
            record: Some(record),
            field: None,
            index: None,
        }
    }

    fn field(self, field: &'static str) -> Self {
        Self {
            field: Some(field),
            ..self
        }
    }

    fn index(self, index: usize) -> Self {
        Self {
            index: Some(index),
            ..self
        }
    }

    fn as_str(&self) -> String {
        let mut path = String::from("$");
        if let Some(record) = self.record {
            path.push_str(&format!("[{record}]"));
        }
        if let Some(field) = self.field {
            path.push('.');
            path.push_str(field);
        }
        if let Some(index) = self.index {
            path.push_str(&format!("[{index}]"));
        }
        path
    }
}

impl PartialOrd for JsonPath {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for JsonPath {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.record
            .cmp(&other.record)
            .then_with(|| self.field.cmp(&other.field))
            .then_with(|| self.index.cmp(&other.index))
    }
}

#[cfg(test)]
mod tests {
    use super::{load_from_reader, BoundedReader, Limits};
    use std::io::{Cursor, Read};
    use std::path::Path;

    #[test]
    fn bounded_reader_stops_after_maximum_plus_one() {
        let mut reader = BoundedReader::new(Cursor::new(vec![b'x'; 100]), 50);
        let mut buf = vec![0_u8; 100];
        let read = reader.read(&mut buf).expect("read");
        assert_eq!(read, 51);
        assert_eq!(reader.consumed, 51);
        assert_eq!(reader.read(&mut buf).expect("eof"), 0);
    }

    #[test]
    fn reader_exceeding_byte_limit_is_file_too_large() {
        let limits = Limits {
            maximum_bytes: 8,
            ..Limits::default()
        };
        let payload = br#"[{"name":"A"}]"#;
        let error = load_from_reader(Path::new("growing.json"), Cursor::new(payload), &limits)
            .expect_err("oversize reader");
        assert!(matches!(error, super::IngestError::FileTooLarge { .. }));
    }
}
