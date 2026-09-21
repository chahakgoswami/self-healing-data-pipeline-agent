"""Day 1 smoke tests: ETL pipeline + schema registry."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.pipeline.mock_source import get_batch
from src.pipeline.etl import extract, transform, load, get_sink, run_pipeline
from src.registry.schema_registry import SchemaRegistry


# ---------------------------------------------------------------------------
# Mock source tests
# ---------------------------------------------------------------------------

class TestMockSource:
    def test_get_orders_batch(self):
        batch = get_batch("orders")
        assert len(batch) == 3
        assert all("order_id" in row for row in batch)

    def test_get_users_batch(self):
        batch = get_batch("users")
        assert len(batch) == 3
        assert all("user_id" in row for row in batch)

    def test_unknown_table_raises(self):
        with pytest.raises(ValueError, match="Unknown mock table"):
            get_batch("nonexistent")

    def test_returns_copies(self):
        b1 = get_batch("orders")
        b2 = get_batch("orders")
        b1[0]["order_id"] = "MUTATED"
        assert b2[0]["order_id"] != "MUTATED"


# ---------------------------------------------------------------------------
# ETL stage tests
# ---------------------------------------------------------------------------

class TestETLStages:
    def test_extract_orders(self):
        records = extract("orders")
        assert isinstance(records, list)
        assert len(records) > 0

    def test_transform_orders_types(self):
        raw = extract("orders")
        transformed = transform("orders", raw)
        for row in transformed:
            assert isinstance(row["customer_id"], int)
            assert isinstance(row["amount"], float)
            assert row["currency"] == row["currency"].upper()
            assert row["status"] == row["status"].lower()

    def test_transform_users_types(self):
        raw = extract("users")
        transformed = transform("users", raw)
        for row in transformed:
            assert isinstance(row["user_id"], int)
            assert isinstance(row["username"], str)
            assert isinstance(row["active"], bool)

    def test_load_and_retrieve(self):
        records = [{"x": 1}]
        load("test_table", records)
        assert get_sink("test_table") == records

    def test_run_pipeline_orders(self):
        result = run_pipeline("orders")
        assert len(result) == 3

    def test_run_pipeline_users(self):
        result = run_pipeline("users")
        assert len(result) == 3

    def test_transform_unknown_table_raises(self):
        with pytest.raises(ValueError, match="No transformer"):
            transform("bogus", [])


# ---------------------------------------------------------------------------
# Schema registry tests
# ---------------------------------------------------------------------------

class TestSchemaRegistry:
    def _tmp_registry(self) -> SchemaRegistry:
        """Return a registry backed by a temp file."""
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        Path(tmp.name).write_text("{}", encoding="utf-8")
        return SchemaRegistry(path=tmp.name)

    def test_loads_existing_registry(self):
        registry = SchemaRegistry()  # uses data/schema_registry.json
        assert "orders" in registry.tables()
        assert "users" in registry.tables()

    def test_get_schema_returns_none_for_unknown(self):
        registry = self._tmp_registry()
        assert registry.get_schema("nonexistent") is None

    def test_register_and_retrieve(self):
        registry = self._tmp_registry()
        columns = {
            "id":   {"type": "int",  "nullable": False, "constraints": {}},
            "name": {"type": "str",  "nullable": True,  "constraints": {}},
        }
        registry.register_table("sample", columns)
        assert "sample" in registry.tables()
        assert registry.get_columns("sample")["id"]["type"] == "int"

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "reg.json"
            registry = SchemaRegistry(path=path)
            registry.register_table("t", {"col": {"type": "str", "nullable": False, "constraints": {}}})
            registry.save()

            reloaded = SchemaRegistry(path=path)
            assert "t" in reloaded.tables()

    def test_infer_and_register(self):
        registry = self._tmp_registry()
        records = [
            {"a": 1,    "b": "hello", "c": None},
            {"a": 2,    "b": "world", "c": 3.14},
        ]
        registry.infer_and_register("inferred", records)
        cols = registry.get_columns("inferred")
        assert cols["a"]["type"] == "int"
        assert cols["b"]["type"] == "str"
        assert cols["c"]["nullable"] is True

    def test_infer_empty_records_raises(self):
        registry = self._tmp_registry()
        with pytest.raises(ValueError, match="empty"):
            registry.infer_and_register("x", [])

    def test_get_columns_unknown_table(self):
        registry = self._tmp_registry()
        assert registry.get_columns("missing") == {}
