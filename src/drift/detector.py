"""Schema drift detector: compares incoming data batches against the registry.

Drift types detected:
- MISSING_COLUMN  : a column expected by the registry is absent from the batch
- TYPE_MISMATCH   : a column's runtime value type differs from the registered type
- NEW_COLUMN      : a column present in the batch is not registered
- NULL_VIOLATION  : a non-nullable column contains a None value in the batch
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.registry.schema_registry import SchemaRegistry


# ---------------------------------------------------------------------------
# Enumerations & dataclasses
# ---------------------------------------------------------------------------

class DriftType(str, Enum):
    MISSING_COLUMN = "missing_column"
    TYPE_MISMATCH  = "type_mismatch"
    NEW_COLUMN     = "new_column"
    NULL_VIOLATION = "null_violation"


@dataclass
class DriftItem:
    """A single drift finding for one column (and optionally one row index)."""
    drift_type:  DriftType
    column:      str
    # For TYPE_MISMATCH / NULL_VIOLATION we include which row triggered it.
    row_index:   int | None = None
    # Extra context (e.g. expected vs actual type).
    expected:    Any = None
    actual:      Any = None
    message:     str = ""

    def __str__(self) -> str:  # pragma: no cover
        parts = [f"[{self.drift_type.value}] column={self.column!r}"]
        if self.row_index is not None:
            parts.append(f"row={self.row_index}")
        if self.expected is not None:
            parts.append(f"expected={self.expected!r}")
        if self.actual is not None:
            parts.append(f"actual={self.actual!r}")
        if self.message:
            parts.append(self.message)
        return "  ".join(parts)


@dataclass
class DriftReport:
    """Aggregated result of a drift-detection run for one table batch."""
    table:   str
    # Total number of rows in the batch that was inspected.
    n_rows:  int
    items:   list[DriftItem] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def has_drift(self) -> bool:
        return len(self.items) > 0

    def by_type(self, drift_type: DriftType) -> list[DriftItem]:
        """Return all items of a given drift type."""
        return [i for i in self.items if i.drift_type == drift_type]

    def summary(self) -> str:
        if not self.has_drift:
            return f"[DriftReport] table={self.table!r}  n_rows={self.n_rows}  NO DRIFT DETECTED"
        lines = [
            f"[DriftReport] table={self.table!r}  n_rows={self.n_rows}  "
            f"drift_items={len(self.items)}",
        ]
        for item in self.items:
            lines.append(f"  {item}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

# Map Python built-in type names as they appear in the registry.
_TYPE_MAP: dict[str, type] = {
    "str":      str,
    "int":      int,
    "float":    float,
    "bool":     bool,
    "list":     list,
    "dict":     dict,
    "NoneType": type(None),
}


class SchemaDriftDetector:
    """Compare an incoming batch of records against a SchemaRegistry entry.

    Usage::

        registry = SchemaRegistry()
        detector = SchemaDriftDetector(registry)
        report   = detector.detect("orders", batch)
    """

    def __init__(self, registry: SchemaRegistry) -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, table: str, batch: list[dict[str, Any]]) -> DriftReport:
        """Detect schema drift in *batch* for *table*.

        Returns a :class:`DriftReport` (which may have zero items if no drift
        is found).
        """
        report = DriftReport(table=table, n_rows=len(batch))

        registered_cols: dict[str, dict[str, Any]] = self._registry.get_columns(table)
        if not registered_cols:
            # Nothing to compare against — we can't detect drift.
            return report

        if not batch:
            # Empty batch: every registered column is effectively missing.
            for col in registered_cols:
                report.items.append(
                    DriftItem(
                        drift_type=DriftType.MISSING_COLUMN,
                        column=col,
                        message="Batch is empty; column presence cannot be verified.",
                    )
                )
            return report

        # ----------------------------------------------------------------
        # 1. MISSING_COLUMN  – registered columns absent from the batch
        # ----------------------------------------------------------------
        # We collect the union of all keys seen across the batch so that
        # a column present in *some* rows is not falsely flagged.
        batch_cols: set[str] = set()
        for row in batch:
            batch_cols.update(row.keys())

        for col in registered_cols:
            if col not in batch_cols:
                report.items.append(
                    DriftItem(
                        drift_type=DriftType.MISSING_COLUMN,
                        column=col,
                        expected=col,
                        message=f"Column '{col}' expected by registry but absent from batch.",
                    )
                )

        # ----------------------------------------------------------------
        # 2. NEW_COLUMN – batch columns not present in the registry
        # ----------------------------------------------------------------
        for col in batch_cols:
            if col not in registered_cols:
                report.items.append(
                    DriftItem(
                        drift_type=DriftType.NEW_COLUMN,
                        column=col,
                        actual=col,
                        message=f"Column '{col}' found in batch but not registered.",
                    )
                )

        # ----------------------------------------------------------------
        # 3. TYPE_MISMATCH & NULL_VIOLATION – per-row checks
        # ----------------------------------------------------------------
        # Track which (col, drift_type) pairs we've already reported once
        # so we don't emit duplicate items for the same column.
        _reported_type_mismatch: set[str] = set()
        _reported_null_violation: set[str] = set()

        for row_idx, row in enumerate(batch):
            for col, col_schema in registered_cols.items():
                if col not in row:
                    # Already captured as MISSING_COLUMN above.
                    continue

                value = row[col]

                # NULL_VIOLATION
                if value is None:
                    nullable = col_schema.get("nullable", True)
                    if not nullable and col not in _reported_null_violation:
                        report.items.append(
                            DriftItem(
                                drift_type=DriftType.NULL_VIOLATION,
                                column=col,
                                row_index=row_idx,
                                expected="non-null",
                                actual=None,
                                message=(
                                    f"Column '{col}' is non-nullable but received "
                                    f"None at row {row_idx}."
                                ),
                            )
                        )
                        _reported_null_violation.add(col)
                    # Skip type check when value is None.
                    continue

                # TYPE_MISMATCH
                expected_type_name: str = col_schema.get("type", "")
                expected_type = _TYPE_MAP.get(expected_type_name)
                if expected_type is not None and col not in _reported_type_mismatch:
                    # Special-case: int values are also valid floats by convention
                    # (registry says float, batch sends int literal) — allow it.
                    actual_type = type(value)
                    compatible = isinstance(value, expected_type) or (
                        expected_type is float and isinstance(value, int)
                    )
                    if not compatible:
                        report.items.append(
                            DriftItem(
                                drift_type=DriftType.TYPE_MISMATCH,
                                column=col,
                                row_index=row_idx,
                                expected=expected_type_name,
                                actual=actual_type.__name__,
                                message=(
                                    f"Column '{col}' expected type '{expected_type_name}' "
                                    f"but got '{actual_type.__name__}' at row {row_idx}."
                                ),
                            )
                        )
                        _reported_type_mismatch.add(col)

        return report
