"""Quick smoke-test: run the ETL pipeline and print results."""
from __future__ import annotations

import sys
from pathlib import Path

# Make sure the project root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.pipeline.etl import run_pipeline
from src.registry.schema_registry import SchemaRegistry


def main() -> None:
    registry = SchemaRegistry()
    print("Registered tables:", registry.tables())
    print()

    for table in ["orders", "users"]:
        print(f"{'=' * 50}")
        print(f"Running pipeline for: {table}")
        print(f"{'=' * 50}")
        records = run_pipeline(table)
        print(f"Output records ({table}):")
        for rec in records:
            print(" ", rec)
        print()

        schema = registry.get_schema(table)
        if schema:
            print(f"Registered schema columns: {list(schema['columns'].keys())}")
        print()


if __name__ == "__main__":
    main()
