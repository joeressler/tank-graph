"""Validate and atomically publish the Python-to-Rust interchange dataset."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .identity import identity_key, normalize_display, sort_key

PRIMARY_CLASSES = frozenset(
    {"Light Tanks", "Medium Tanks", "Heavy Tanks", "Tank Destroyers", "SPGs"}
)
CANONICAL_SUBCLASSES = frozenset({"Autoloaders"})
CANONICAL_NATIONS = frozenset(
    {
        "USSR",
        "Germany",
        "USA",
        "France",
        "UK",
        "China",
        "Japan",
        "Czechoslovakia",
        "Sweden",
        "Poland",
        "Italy",
    }
)
RECORD_FIELDS = (
    "name",
    "display_name",
    "class",
    "subclasses",
    "nation",
    "tier",
    "strategies",
    "keywords",
)
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class DatasetValidationError(ValueError):
    """Raised when assembled records violate the interchange contract."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = tuple(violations)
        super().__init__("dataset validation failed: " + "; ".join(self.violations))


def _has_disallowed_control(value: str) -> bool:
    return any(unicodedata.category(char) == "Cc" and char not in "\r\n\t" for char in value)


def _validate_text(
    value: Any,
    path: str,
    limit: int,
    violations: list[str],
    *,
    single_line: bool = False,
) -> None:
    if not isinstance(value, str):
        violations.append(f"{path}: expected string")
        return
    if not value or not value.strip():
        violations.append(f"{path}: must not be blank")
    if value != normalize_display(value):
        violations.append(f"{path}: text is not in canonical emitted form")
    if len(value) > limit:
        violations.append(f"{path}: exceeds {limit} Unicode scalars")
    if _has_disallowed_control(value):
        violations.append(f"{path}: contains a disallowed control character")
    if single_line and any(char in value for char in "\r\n\t"):
        violations.append(f"{path}: must not contain a line break or tab")


def _validate_repeated(
    values: Any,
    path: str,
    item_limit: int,
    max_items: int,
    violations: list[str],
    *,
    keywords: bool = False,
) -> None:
    if not isinstance(values, list):
        violations.append(f"{path}: expected array")
        return
    if len(values) > max_items:
        violations.append(f"{path}: exceeds {max_items} items")

    identities: dict[str, int] = {}
    previous: tuple[str, str] | None = None
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        _validate_text(
            value,
            item_path,
            item_limit,
            violations,
            single_line=keywords,
        )
        if not isinstance(value, str):
            continue
        identity = identity_key(value)
        if identity in identities:
            violations.append(
                f"{item_path}: normalized duplicate of {path}[{identities[identity]}]"
            )
        else:
            identities[identity] = index
        value_sort_key = sort_key(value)
        if previous is not None and value_sort_key <= previous:
            violations.append(f"{item_path}: array is not strictly sorted")
        previous = value_sort_key
        if keywords and value != value.casefold():
            violations.append(f"{item_path}: keyword must be lowercase")


def validate_dataset(
    records: Sequence[Mapping[str, Any]],
    schema_path: Path,
    *,
    require_nonempty: bool = False,
) -> list[dict[str, Any]]:
    """Return canonical records or raise with every actionable violation."""

    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise DatasetValidationError(["$: expected a root array"])

    canonical: list[dict[str, Any]] = []
    semantic: list[str] = []
    seen_names: dict[str, int] = {}
    previous_name: tuple[str, str] | None = None

    if require_nonempty and not records:
        semantic.append("$: production dataset must contain at least one tank")

    for index, source_record in enumerate(records):
        path = f"$[{index}]"
        if not isinstance(source_record, Mapping):
            semantic.append(f"{path}: expected object")
            continue
        unknown = set(source_record) - set(RECORD_FIELDS)
        missing = set(RECORD_FIELDS) - set(source_record)
        if unknown:
            semantic.append(f"{path}: unknown fields {sorted(unknown)!r}")
        if missing:
            semantic.append(f"{path}: missing fields {sorted(missing)!r}")
        if unknown or missing:
            continue

        record = {field: source_record[field] for field in RECORD_FIELDS}
        canonical.append(record)
        _validate_text(record["name"], f"{path}.name", 120, semantic)
        _validate_text(record["display_name"], f"{path}.display_name", 120, semantic)
        _validate_text(record["class"], f"{path}.class", 120, semantic)
        _validate_text(record["nation"], f"{path}.nation", 120, semantic)

        name = record["name"]
        if isinstance(name, str):
            if not NAME_PATTERN.fullmatch(name):
                semantic.append(f"{path}.name: invalid stable identifier")
            identity = identity_key(name)
            if identity in seen_names:
                semantic.append(
                    f"{path}.name: normalized duplicate of $[{seen_names[identity]}].name"
                )
            else:
                seen_names[identity] = index
            name_sort_key = sort_key(name)
            if previous_name is not None and name_sort_key <= previous_name:
                semantic.append(f"{path}.name: records are not strictly sorted")
            previous_name = name_sort_key

        if record["class"] not in PRIMARY_CLASSES:
            semantic.append(f"{path}.class: unknown primary class")
        if record["nation"] not in CANONICAL_NATIONS:
            semantic.append(f"{path}.nation: unknown canonical nation")

        tier = record["tier"]
        if isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= 10:
            semantic.append(f"{path}.tier: expected integer from 1 through 10")

        _validate_repeated(record["subclasses"], f"{path}.subclasses", 120, 100, semantic)
        _validate_repeated(record["strategies"], f"{path}.strategies", 1000, 1000, semantic)
        _validate_repeated(
            record["keywords"],
            f"{path}.keywords",
            120,
            1000,
            semantic,
            keywords=True,
        )

        subclasses = record["subclasses"]
        if isinstance(subclasses, list):
            primary_identities = {identity_key(value) for value in PRIMARY_CLASSES}
            for subclass_index, subclass in enumerate(subclasses):
                if isinstance(subclass, str) and identity_key(subclass) in primary_identities:
                    semantic.append(
                        f"{path}.subclasses[{subclass_index}]: primary class is not a subclass"
                    )
                elif subclass not in CANONICAL_SUBCLASSES:
                    semantic.append(
                        f"{path}.subclasses[{subclass_index}]: unknown canonical subclass"
                    )

        if record["strategies"] == [] and record["keywords"] != []:
            semantic.append(f"{path}.keywords: must be empty when strategies is empty")

        for field, value in record.items():
            if isinstance(value, float) and not math.isfinite(value):
                semantic.append(f"{path}.{field}: non-finite number is prohibited")

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetValidationError([f"schema: unable to load {schema_path}: {error}"]) from error

    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(canonical),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    violations = [
        f"schema {'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
        for error in schema_errors
    ]
    violations.extend(semantic)
    if violations:
        raise DatasetValidationError(violations)
    return canonical


def serialize_dataset(records: Sequence[Mapping[str, Any]]) -> bytes:
    """Serialize validated canonical records with stable UTF-8 formatting."""

    text = json.dumps(
        records,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        separators=(",", ": "),
    )
    return f"{text}\n".encode()


def write_dataset_atomic(
    records: Sequence[Mapping[str, Any]],
    destination: Path,
    schema_path: Path,
    *,
    require_nonempty: bool = False,
) -> bytes:
    """Validate and replace destination atomically without risking prior data."""

    canonical = validate_dataset(records, schema_path, require_nonempty=require_nonempty)
    payload = serialize_dataset(canonical)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return payload
