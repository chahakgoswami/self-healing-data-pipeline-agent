"""Mock data source that simulates reading from an upstream system."""
from __future__ import annotations

from typing import Any

# Each entry is a "batch" that would arrive at the pipeline.
# Batches are intentionally kept clean for Day 1; drift scenarios come later.

ORDERS_BATCH: list[dict[str, Any]] = [
    {"order_id": "ORD-001", "customer_id": 101, "amount": 49.99, "currency": "USD", "status": "shipped"},
    {"order_id": "ORD-002", "customer_id": 102, "amount": 129.00, "currency": "EUR", "status": "pending"},
    {"order_id": "ORD-003", "customer_id": 103, "amount": 9.99,  "currency": "USD", "status": "delivered"},
]

USERS_BATCH: list[dict[str, Any]] = [
    {"user_id": 1, "username": "alice",   "email": "alice@example.com",   "age": 30, "active": True},
    {"user_id": 2, "username": "bob",     "email": "bob@example.com",     "age": 25, "active": False},
    {"user_id": 3, "username": "charlie", "email": "charlie@example.com", "age": 35, "active": True},
]


def get_batch(table: str) -> list[dict[str, Any]]:
    """Return a mock batch for *table*."""
    batches = {
        "orders": ORDERS_BATCH,
        "users": USERS_BATCH,
    }
    if table not in batches:
        raise ValueError(f"Unknown mock table: {table!r}. Available: {list(batches)}")
    return [row.copy() for row in batches[table]]
