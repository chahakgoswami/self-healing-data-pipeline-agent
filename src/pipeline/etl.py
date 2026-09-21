"""Simulated ETL pipeline: Extract → Transform → Load (to in-memory sink)."""
from __future__ import annotations

from typing import Any

from src.pipeline.mock_source import get_batch


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def extract(table: str) -> list[dict[str, Any]]:
    """Extract raw records from the mock source for *table*."""
    records = get_batch(table)
    print(f"[extract] {len(records)} record(s) read from '{table}'")
    return records


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

def _transform_orders(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in records:
        out.append({
            "order_id":    row["order_id"],
            "customer_id": int(row["customer_id"]),
            "amount":      float(row["amount"]),
            "currency":    str(row["currency"]).upper(),
            "status":      str(row["status"]).lower(),
        })
    return out


def _transform_users(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in records:
        out.append({
            "user_id":  int(row["user_id"]),
            "username": str(row["username"]).strip().lower(),
            "email":    str(row["email"]).strip().lower(),
            "age":      int(row["age"]),
            "active":   bool(row["active"]),
        })
    return out


_TRANSFORMERS = {
    "orders": _transform_orders,
    "users":  _transform_users,
}


def transform(table: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply table-specific transformations."""
    fn = _TRANSFORMERS.get(table)
    if fn is None:
        raise ValueError(f"No transformer registered for table: {table!r}")
    result = fn(records)
    print(f"[transform] {len(result)} record(s) transformed for '{table}'")
    return result


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

# In-memory sink — replaces a real database write.
_SINK: dict[str, list[dict[str, Any]]] = {}


def load(table: str, records: list[dict[str, Any]]) -> None:
    """Load records into the in-memory sink."""
    _SINK[table] = records
    print(f"[load] {len(records)} record(s) written to in-memory sink for '{table}'")


def get_sink(table: str) -> list[dict[str, Any]]:
    """Retrieve loaded records from the in-memory sink (for inspection / testing)."""
    return _SINK.get(table, [])


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def run_pipeline(table: str) -> list[dict[str, Any]]:
    """Run the full ETL pipeline for *table* and return the loaded records."""
    raw = extract(table)
    transformed = transform(table, raw)
    load(table, transformed)
    return get_sink(table)
