"""Schema registry: persists expected column names, types, and constraints."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY_PATH = Path(os.environ.get("SCHEMA_REGISTRY_PATH", "data/schema_registry.json"))

# Canonical Python type names we record.
_VALID_TYPES = {"str", "int", "float", "bool", "list", "dict", "NoneType"}


class SchemaRegistry:
    """Load, query, and persist table schemas.

    Schema file format::

        {
            "<table>": {
                "columns": {
                    "<col>": {
                        "type": "<python type name>",
                        "nullable": <bool>,
                        "constraints": { ... }   # optional, arbitrary KV
                    },
                    ...
                }
            },
            ...
        }
    """

    def __init__(self, path: Path | str = DEFAULT_REGISTRY_PATH) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        else:
            self._data = {}

    def save(self) -> None:
        """Persist the registry to disk (creates parent dirs if needed)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        print(f"[registry] Schema saved to {self.path}")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def tables(self) -> list[str]:
        """Return names of all registered tables."""
        return list(self._data.keys())

    def get_schema(self, table: str) -> dict[str, Any] | None:
        """Return the schema dict for *table*, or None if not registered."""
        return self._data.get(table)

    def get_columns(self, table: str) -> dict[str, Any]:
        """Return the columns sub-dict for *table* (empty dict if unknown)."""
        schema = self.get_schema(table)
        if schema is None:
            return {}
        return schema.get("columns", {})

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def register_table(self, table: str, columns: dict[str, dict[str, Any]]) -> None:
        """Register or overwrite the schema for *table*.

        *columns* example::

            {
                "order_id":    {"type": "str",   "nullable": False},
                "customer_id": {"type": "int",   "nullable": False},
                "amount":      {"type": "float", "nullable": False},
            }
        """
        self._data[table] = {"columns": columns}

    def infer_and_register(self, table: str, records: list[dict[str, Any]]) -> None:
        """Infer a schema from *records* and register it.

        Inferred constraints:
        - ``nullable``: True if any record has ``None`` for that column.
        """
        if not records:
            raise ValueError("Cannot infer schema from an empty record list.")

        columns: dict[str, dict[str, Any]] = {}
        for row in records:
            for col, val in row.items():
                if col not in columns:
                    columns[col] = {
                        "type": type(val).__name__,
                        "nullable": val is None,
                        "constraints": {},
                    }
                else:
                    # If we see a non-None value where we previously saw None,
                    # keep the non-None type but mark nullable True.
                    if val is None:
                        columns[col]["nullable"] = True
                    elif columns[col]["type"] == "NoneType":
                        columns[col]["type"] = type(val).__name__
        self.register_table(table, columns)
