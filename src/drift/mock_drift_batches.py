"""Canned drift scenarios used by tests and the future orchestrator."""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# orders – drift scenarios
# ---------------------------------------------------------------------------

ORDERS_MISSING_COLUMN: list[dict[str, Any]] = [
    # 'amount' column is missing from every row
    {"order_id": "ORD-010", "customer_id": 201, "currency": "USD", "status": "shipped"},
    {"order_id": "ORD-011", "customer_id": 202, "currency": "EUR", "status": "pending"},
]

ORDERS_TYPE_MISMATCH: list[dict[str, Any]] = [
    # 'customer_id' is a string instead of int
    {"order_id": "ORD-020", "customer_id": "two-oh-one", "amount": 9.99, "currency": "USD", "status": "shipped"},
    {"order_id": "ORD-021", "customer_id": 202,          "amount": 5.00, "currency": "USD", "status": "pending"},
]

ORDERS_NEW_COLUMN: list[dict[str, Any]] = [
    # Extra 'discount_pct' column not in registry
    {"order_id": "ORD-030", "customer_id": 301, "amount": 19.99, "currency": "USD", "status": "shipped",   "discount_pct": 0.1},
    {"order_id": "ORD-031", "customer_id": 302, "amount": 29.99, "currency": "EUR", "status": "delivered", "discount_pct": 0.0},
]

ORDERS_NULL_VIOLATION: list[dict[str, Any]] = [
    # 'amount' is non-nullable in the registry, but we send None
    {"order_id": "ORD-040", "customer_id": 401, "amount": None,  "currency": "USD", "status": "shipped"},
    {"order_id": "ORD-041", "customer_id": 402, "amount": 14.99, "currency": "USD", "status": "pending"},
]

ORDERS_MULTI_DRIFT: list[dict[str, Any]] = [
    # Missing 'status', type mismatch on 'customer_id', new column 'region'
    {"order_id": "ORD-050", "customer_id": "bad", "amount": 5.00, "currency": "USD", "region": "EMEA"},
    {"order_id": "ORD-051", "customer_id": 502,   "amount": 6.00, "currency": "USD", "region": "APAC"},
]

# ---------------------------------------------------------------------------
# users – drift scenarios
# ---------------------------------------------------------------------------

USERS_NULL_VIOLATION: list[dict[str, Any]] = [
    # 'email' is non-nullable; one row sends None
    {"user_id": 10, "username": "dave",  "email": None,                "age": 28, "active": True},
    {"user_id": 11, "username": "eve",   "email": "eve@example.com",   "age": 22, "active": False},
]

USERS_NEW_COLUMN: list[dict[str, Any]] = [
    {"user_id": 20, "username": "frank", "email": "frank@example.com", "age": 40, "active": True,  "phone": "555-0100"},
    {"user_id": 21, "username": "grace", "email": "grace@example.com", "age": 33, "active": False, "phone": "555-0101"},
]

# ---------------------------------------------------------------------------
# Lookup helper
# ---------------------------------------------------------------------------

_SCENARIOS: dict[str, tuple[str, list[dict[str, Any]]]] = {
    "orders_missing_column":  ("orders", ORDERS_MISSING_COLUMN),
    "orders_type_mismatch":   ("orders", ORDERS_TYPE_MISMATCH),
    "orders_new_column":      ("orders", ORDERS_NEW_COLUMN),
    "orders_null_violation":  ("orders", ORDERS_NULL_VIOLATION),
    "orders_multi_drift":     ("orders", ORDERS_MULTI_DRIFT),
    "users_null_violation":   ("users",  USERS_NULL_VIOLATION),
    "users_new_column":       ("users",  USERS_NEW_COLUMN),
}


def get_drift_scenario(name: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (table_name, batch) for the named drift scenario."""
    if name not in _SCENARIOS:
        raise ValueError(
            f"Unknown drift scenario: {name!r}. Available: {list(_SCENARIOS)}"
        )
    return _SCENARIOS[name]


def list_scenarios() -> list[str]:
    """Return all available scenario names."""
    return list(_SCENARIOS.keys())
