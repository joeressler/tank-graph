from __future__ import annotations

import json
from pathlib import Path

import pytest

from tank_graph_extractor.output import (
    DatasetValidationError,
    serialize_dataset,
    validate_dataset,
    write_dataset_atomic,
)

SCHEMA = Path(__file__).parents[2] / "docs" / "specs" / "tanks-data.schema.json"


def record(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "G04_PzVI_Tiger_I",
        "display_name": "Tiger I",
        "class": "Heavy Tanks",
        "subclasses": [],
        "nation": "Germany",
        "tier": 7,
        "strategies": [
            "Excels at long-range sniping due to high accuracy.",
            "Use sidescraping to protect the hull armor.",
        ],
        "keywords": ["accuracy", "hull armor", "sidescraping", "sniping"],
    }
    value.update(changes)
    return value


def test_schema_and_semantic_validation_accepts_canonical_record() -> None:
    assert validate_dataset([record()], SCHEMA) == [record()]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"class": "Battleship"}, "unknown primary class"),
        ({"tier": 0}, "expected integer from 1 through 10"),
        ({"strategies": [], "keywords": ["sniping"]}, "must be empty"),
        ({"keywords": ["Sniping"]}, "keyword must be lowercase"),
        ({"subclasses": ["Heavy Tanks"]}, "primary class is not a subclass"),
        ({"subclasses": ["Wheelchairs"]}, "unknown canonical subclass"),
        ({"nation": "Atlantis"}, "unknown canonical nation"),
        ({"strategies": ["same", "Same"]}, "normalized duplicate"),
    ],
)
def test_semantic_validation_rejects_invalid_records(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(DatasetValidationError, match=message):
        validate_dataset([record(**changes)], SCHEMA)


def test_records_must_be_sorted_and_normalized_unique() -> None:
    first = record(name="z")
    second = record(name="Z")
    with pytest.raises(DatasetValidationError) as error:
        validate_dataset([first, second], SCHEMA)
    assert "normalized duplicate" in str(error.value)
    assert "strictly sorted" in str(error.value)


def test_production_dataset_must_not_be_empty() -> None:
    with pytest.raises(DatasetValidationError, match="at least one tank"):
        validate_dataset([], SCHEMA, require_nonempty=True)


def test_atomic_write_is_stable_and_preserves_prior_file_on_failure(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "tanks_data.json"
    first = write_dataset_atomic([record()], destination, SCHEMA)
    second = write_dataset_atomic([record()], destination, SCHEMA)
    assert first == second == destination.read_bytes()
    assert destination.read_bytes().endswith(b"\n")
    assert json.loads(destination.read_text(encoding="utf-8")) == [record()]

    before = destination.read_bytes()
    with pytest.raises(DatasetValidationError):
        write_dataset_atomic([record(tier=11)], destination, SCHEMA)
    assert destination.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_serialization_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError):
        serialize_dataset([{"value": float("nan")}])
