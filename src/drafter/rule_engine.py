"""Rule-based fix engine.

For well-known drift types + column combinations the engine can produce a
fix code string entirely without LLM assistance.  When no rule matches it
returns ``None`` so the caller can fall back to the LLM stub.
"""
from __future__ import annotations

from typing import Any

from src.drift.detector import DriftItem, DriftType


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------
# Each rule is a tuple:
#   (drift_type, column_name_or_None, expected_type_or_None, code_template)
# None in the column/expected positions means "match any".

_RULES: list[tuple[DriftType, str | None, str | None, str]] = [
    # ---- TYPE_MISMATCH rules -------------------------------------------
    (
        DriftType.TYPE_MISMATCH,
        None,  # any column
        "int",
        "record['{col}'] = int(record['{col}']) if record.get('{col}') is not None else None",
    ),
    (
        DriftType.TYPE_MISMATCH,
        None,
        "float",
        "record['{col}'] = float(record['{col}']) if record.get('{col}') is not None else None",
    ),
    (
        DriftType.TYPE_MISMATCH,
        None,
        "str",
        "record['{col}'] = str(record['{col}']) if record.get('{col}') is not None else None",
    ),
    (
        DriftType.TYPE_MISMATCH,
        None,
        "bool",
        "record['{col}'] = bool(record['{col}']) if record.get('{col}') is not None else None",
    ),
    # ---- NULL_VIOLATION rules ------------------------------------------
    (
        DriftType.NULL_VIOLATION,
        None,
        "int",
        "record['{col}'] = record['{col}'] if record.get('{col}') is not None else -1",
    ),
    (
        DriftType.NULL_VIOLATION,
        None,
        "float",
        "record['{col}'] = record['{col}'] if record.get('{col}') is not None else 0.0",
    ),
    (
        DriftType.NULL_VIOLATION,
        None,
        "str",
        "record['{col}'] = record['{col}'] if record.get('{col}') is not None else ''",
    ),
    (
        DriftType.NULL_VIOLATION,
        None,
        "bool",
        "record['{col}'] = record['{col}'] if record.get('{col}') is not None else False",
    ),
    # ---- MISSING_COLUMN rules -----------------------------------------
    (
        DriftType.MISSING_COLUMN,
        None,
        "int",
        "record.setdefault('{col}', -1)",
    ),
    (
        DriftType.MISSING_COLUMN,
        None,
        "float",
        "record.setdefault('{col}', 0.0)",
    ),
    (
        DriftType.MISSING_COLUMN,
        None,
        "str",
        "record.setdefault('{col}', '')",
    ),
    (
        DriftType.MISSING_COLUMN,
        None,
        "bool",
        "record.setdefault('{col}', False)",
    ),
    # ---- NEW_COLUMN rules ---------------------------------------------
    (
        DriftType.NEW_COLUMN,
        None,  # any new column
        None,  # no expected type
        "record.pop('{col}', None)",
    ),
]


def _wrap_body(col: str, body: str) -> str:
    """Wrap a single-statement rule body into a proper fix function."""
    rendered = body.format(col=col)
    return (
        f"def fix(record: dict) -> dict:\n"
        f"    {rendered}\n"
        f"    return record"
    )


def apply_rules(
    item: DriftItem,
    registered_type: str | None = None,
) -> str | None:
    """Return a code string for *item* if a built-in rule matches, else None.

    Parameters
    ----------
    item:
        The :class:`~src.drift.detector.DriftItem` to fix.
    registered_type:
        The type name from the schema registry for the column (may be None).
    """
    for rule_dtype, rule_col, rule_expected, template in _RULES:
        if rule_dtype != item.drift_type:
            continue
        if rule_col is not None and rule_col != item.column:
            continue
        # For NEW_COLUMN the expected type is irrelevant.
        if rule_expected is not None and rule_expected != registered_type:
            continue
        return _wrap_body(item.column, template)
    return None
