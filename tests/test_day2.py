"""Day 2 tests: schema drift detector."""
from __future__ import annotations

import tempfile
import json
from pathlib import Path
from typing import Any

import pytest

from src.registry.schema_registry import SchemaRegistry
from src.drift.detector import (
    SchemaDriftDetector,
    DriftReport,
    DriftItem,
    DriftType,
)
from src.drift.mock_drift_batches import (
    get_drift_scenario,
    list_scenarios,
    ORDERS_MISSING_COLUMN,
    ORDERS_TYPE_MISMATCH,
    ORDERS_NEW_COLUMN,
    ORDERS_NULL_VIOLATION,
    ORDERS_MULTI_DRIFT,
    USERS_NULL_VIOLATION,
    USERS_NEW_COLUMN,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registry() -> SchemaRegistry:
    """Return the real registry backed by data/schema_registry.json."""
    return SchemaRegistry()  # uses default path


def _tmp_registry(columns: dict[str, dict[str, Any]]) -> SchemaRegistry:
    """Create an in-memory-like registry with a single table 'tbl'."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    Path(tmp.name).write_text(json.dumps({"tbl": {"columns": columns}}), encoding="utf-8")
    return SchemaRegistry(path=tmp.name)


# ---------------------------------------------------------------------------
# DriftItem / DriftReport dataclass tests
# ---------------------------------------------------------------------------

class TestDataclasses:
    def test_drift_item_fields(self):
        item = DriftItem(
            drift_type=DriftType.MISSING_COLUMN,
            column="foo",
            expected="foo",
            message="gone",
        )
        assert item.drift_type == DriftType.MISSING_COLUMN
        assert item.column == "foo"
        assert item.row_index is None

    def test_drift_report_has_drift_false(self):
        report = DriftReport(table="t", n_rows=3)
        assert not report.has_drift

    def test_drift_report_has_drift_true(self):
        report = DriftReport(table="t", n_rows=3)
        report.items.append(
            DriftItem(drift_type=DriftType.NEW_COLUMN, column="x")
        )
        assert report.has_drift

    def test_drift_report_by_type(self):
        report = DriftReport(table="t", n_rows=2)
        report.items += [
            DriftItem(drift_type=DriftType.NEW_COLUMN, column="a"),
            DriftItem(drift_type=DriftType.MISSING_COLUMN, column="b"),
        ]
        new_cols = report.by_type(DriftType.NEW_COLUMN)
        assert len(new_cols) == 1
        assert new_cols[0].column == "a"

    def test_drift_report_summary_no_drift(self):
        report = DriftReport(table="t", n_rows=5)
        assert "NO DRIFT" in report.summary()

    def test_drift_report_summary_with_drift(self):
        report = DriftReport(table="t", n_rows=2)
        report.items.append(DriftItem(drift_type=DriftType.NEW_COLUMN, column="z"))
        summary = report.summary()
        assert "drift_items=1" in summary

    def test_drift_type_enum_values(self):
        assert DriftType.MISSING_COLUMN.value == "missing_column"
        assert DriftType.TYPE_MISMATCH.value  == "type_mismatch"
        assert DriftType.NEW_COLUMN.value     == "new_column"
        assert DriftType.NULL_VIOLATION.value == "null_violation"


# ---------------------------------------------------------------------------
# SchemaDriftDetector – no-drift baseline
# ---------------------------------------------------------------------------

class TestNoDrift:
    def test_clean_orders_batch_no_drift(self):
        from src.pipeline.mock_source import get_batch
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        batch = get_batch("orders")
        report = detector.detect("orders", batch)
        assert isinstance(report, DriftReport)
        assert not report.has_drift

    def test_clean_users_batch_no_drift(self):
        from src.pipeline.mock_source import get_batch
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        batch = get_batch("users")
        report = detector.detect("users", batch)
        assert not report.has_drift

    def test_unregistered_table_returns_empty_report(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("nonexistent_table", [{"a": 1}])
        assert not report.has_drift
        assert report.n_rows == 1


# ---------------------------------------------------------------------------
# MISSING_COLUMN
# ---------------------------------------------------------------------------

class TestMissingColumn:
    def test_detects_missing_amount(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_MISSING_COLUMN)
        assert report.has_drift
        missing = report.by_type(DriftType.MISSING_COLUMN)
        assert len(missing) == 1
        assert missing[0].column == "amount"

    def test_empty_batch_flags_all_columns_missing(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", [])
        assert report.has_drift
        missing_cols = {i.column for i in report.by_type(DriftType.MISSING_COLUMN)}
        expected_cols = set(registry.get_columns("orders").keys())
        assert missing_cols == expected_cols

    def test_missing_column_no_type_mismatch(self):
        """A missing column must not also appear as a type mismatch."""
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_MISSING_COLUMN)
        mismatches = report.by_type(DriftType.TYPE_MISMATCH)
        mismatch_cols = {i.column for i in mismatches}
        assert "amount" not in mismatch_cols

    def test_single_row_missing_column(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        # Only one column present
        batch = [{"order_id": "X"}]
        report = detector.detect("orders", batch)
        missing = {i.column for i in report.by_type(DriftType.MISSING_COLUMN)}
        assert {"customer_id", "amount", "currency", "status"} == missing


# ---------------------------------------------------------------------------
# TYPE_MISMATCH
# ---------------------------------------------------------------------------

class TestTypeMismatch:
    def test_detects_customer_id_string(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_TYPE_MISMATCH)
        mismatches = report.by_type(DriftType.TYPE_MISMATCH)
        assert len(mismatches) == 1
        assert mismatches[0].column == "customer_id"
        assert mismatches[0].expected == "int"
        assert mismatches[0].actual   == "str"

    def test_int_for_float_column_is_ok(self):
        """int literal in a float column must NOT be flagged as a mismatch."""
        cols = {"price": {"type": "float", "nullable": False, "constraints": {}}}
        registry = _tmp_registry(cols)
        detector = SchemaDriftDetector(registry)
        batch = [{"price": 5}]   # int literal, expected float
        report = detector.detect("tbl", batch)
        assert not report.has_drift

    def test_type_mismatch_records_row_index(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_TYPE_MISMATCH)
        item = report.by_type(DriftType.TYPE_MISMATCH)[0]
        # First row (index 0) has the bad value
        assert item.row_index == 0

    def test_type_mismatch_reported_once_per_column(self):
        """Even if multiple rows have a type mismatch, we emit one item per column."""
        cols = {"val": {"type": "int", "nullable": False, "constraints": {}}}
        registry = _tmp_registry(cols)
        detector = SchemaDriftDetector(registry)
        batch = [{"val": "bad"}, {"val": "also bad"}]
        report = detector.detect("tbl", batch)
        mismatches = report.by_type(DriftType.TYPE_MISMATCH)
        assert len(mismatches) == 1


# ---------------------------------------------------------------------------
# NEW_COLUMN
# ---------------------------------------------------------------------------

class TestNewColumn:
    def test_detects_discount_pct(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_NEW_COLUMN)
        new_cols = report.by_type(DriftType.NEW_COLUMN)
        assert len(new_cols) == 1
        assert new_cols[0].column == "discount_pct"

    def test_detects_phone_in_users(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("users", USERS_NEW_COLUMN)
        new_cols = report.by_type(DriftType.NEW_COLUMN)
        assert any(i.column == "phone" for i in new_cols)

    def test_new_column_not_also_missing(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_NEW_COLUMN)
        missing = {i.column for i in report.by_type(DriftType.MISSING_COLUMN)}
        new_col = {i.column for i in report.by_type(DriftType.NEW_COLUMN)}
        assert new_col.isdisjoint(missing)

    def test_multiple_new_columns(self):
        cols = {"a": {"type": "int", "nullable": False, "constraints": {}}}
        registry = _tmp_registry(cols)
        detector = SchemaDriftDetector(registry)
        batch = [{"a": 1, "b": 2, "c": 3}]
        report = detector.detect("tbl", batch)
        new_names = {i.column for i in report.by_type(DriftType.NEW_COLUMN)}
        assert new_names == {"b", "c"}


# ---------------------------------------------------------------------------
# NULL_VIOLATION
# ---------------------------------------------------------------------------

class TestNullViolation:
    def test_detects_null_amount(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_NULL_VIOLATION)
        nulls = report.by_type(DriftType.NULL_VIOLATION)
        assert len(nulls) == 1
        assert nulls[0].column == "amount"

    def test_detects_null_email_users(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("users", USERS_NULL_VIOLATION)
        nulls = report.by_type(DriftType.NULL_VIOLATION)
        assert any(i.column == "email" for i in nulls)

    def test_nullable_column_allows_none(self):
        cols = {
            "a": {"type": "str", "nullable": True,  "constraints": {}},
            "b": {"type": "str", "nullable": False, "constraints": {}},
        }
        registry = _tmp_registry(cols)
        detector = SchemaDriftDetector(registry)
        batch = [{"a": None, "b": "ok"}]
        report = detector.detect("tbl", batch)
        assert not report.has_drift

    def test_null_violation_not_also_type_mismatch(self):
        """A None value must not simultaneously trigger TYPE_MISMATCH."""
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_NULL_VIOLATION)
        mismatch_cols = {i.column for i in report.by_type(DriftType.TYPE_MISMATCH)}
        assert "amount" not in mismatch_cols

    def test_null_violation_reported_once_per_column(self):
        cols = {"x": {"type": "int", "nullable": False, "constraints": {}}}
        registry = _tmp_registry(cols)
        detector = SchemaDriftDetector(registry)
        batch = [{"x": None}, {"x": None}, {"x": None}]
        report = detector.detect("tbl", batch)
        nulls = report.by_type(DriftType.NULL_VIOLATION)
        assert len(nulls) == 1


# ---------------------------------------------------------------------------
# Multi-drift
# ---------------------------------------------------------------------------

class TestMultiDrift:
    def test_orders_multi_drift_all_types_present(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_MULTI_DRIFT)
        assert report.has_drift
        found_types = {i.drift_type for i in report.items}
        assert DriftType.MISSING_COLUMN in found_types
        assert DriftType.TYPE_MISMATCH  in found_types
        assert DriftType.NEW_COLUMN     in found_types

    def test_report_n_rows_correct(self):
        registry = _registry()
        detector = SchemaDriftDetector(registry)
        report = detector.detect("orders", ORDERS_MULTI_DRIFT)
        assert report.n_rows == len(ORDERS_MULTI_DRIFT)


# ---------------------------------------------------------------------------
# Mock drift batches module
# ---------------------------------------------------------------------------

class TestMockDriftBatches:
    def test_list_scenarios_returns_names(self):
        names = list_scenarios()
        assert len(names) > 0
        assert "orders_missing_column" in names

    def test_get_drift_scenario_returns_tuple(self):
        table, batch = get_drift_scenario("orders_missing_column")
        assert table == "orders"
        assert isinstance(batch, list)
        assert len(batch) > 0

    def test_get_unknown_scenario_raises(self):
        with pytest.raises(ValueError, match="Unknown drift scenario"):
            get_drift_scenario("does_not_exist")

    def test_all_scenarios_valid(self):
        """Every listed scenario can be fetched without error."""
        for name in list_scenarios():
            table, batch = get_drift_scenario(name)
            assert isinstance(table, str)
            assert isinstance(batch, list)
