"""CLI entry point for the Self-Healing Data Pipeline Agent.

Usage examples::

    # Run all drift scenarios through the orchestrator
    python -m src.cli run

    # Run specific scenarios
    python -m src.cli run --scenarios orders_type_mismatch orders_null_violation

    # List all available drift scenarios
    python -m src.cli list-scenarios

    # Run the ETL pipeline only (no drift detection)
    python -m src.cli etl --table orders

    # Detect drift for a named scenario and print the report
    python -m src.cli detect --scenario orders_multi_drift
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def cmd_list_scenarios(args: argparse.Namespace) -> int:
    """List all available drift scenario names."""
    from src.drift.mock_drift_batches import list_scenarios
    names = list_scenarios()
    print(f"Available drift scenarios ({len(names)}):")
    for name in names:
        print(f"  {name}")
    return 0


def cmd_etl(args: argparse.Namespace) -> int:
    """Run the mock ETL pipeline for a table and print the output records."""
    from src.pipeline.etl import run_pipeline
    table = args.table
    print(f"Running ETL pipeline for table: {table!r}")
    records = run_pipeline(table)
    print(f"Output ({len(records)} records):")
    for rec in records:
        print(" ", rec)
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    """Run drift detection for a named scenario and print the report."""
    from src.drift.mock_drift_batches import get_drift_scenario
    from src.drift.detector import SchemaDriftDetector
    from src.registry.schema_registry import SchemaRegistry

    name = args.scenario
    try:
        table, batch = get_drift_scenario(name)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    registry = SchemaRegistry()
    detector = SchemaDriftDetector(registry)
    report   = detector.detect(table, batch)
    print(report.summary())
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the full orchestrator agent loop."""
    from src.orchestrator.agent_loop import OrchestratorAgent

    scenarios   = args.scenarios or None   # None  → all
    output_dir  = Path(args.output_dir)

    agent = OrchestratorAgent(
        output_dir=output_dir,
        auto_approve=True,
    )
    report = agent.run(scenario_names=scenarios)
    print()
    print("Run complete.")
    print(f"  Total successes : {report.total_successes}")
    print(f"  Total failures  : {report.total_failures}")
    print(f"  Total rollbacks : {report.total_rollbacks}")
    return 0 if report.total_failures == 0 else 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="self-healing-pipeline",
        description="Self-Healing Data Pipeline Agent CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ---- list-scenarios ------------------------------------------------
    sub.add_parser(
        "list-scenarios",
        help="List all available mock drift scenarios.",
    )

    # ---- etl -----------------------------------------------------------
    p_etl = sub.add_parser(
        "etl",
        help="Run the mock ETL pipeline for a table.",
    )
    p_etl.add_argument(
        "--table",
        default="orders",
        help="Table name to run (default: orders).",
    )

    # ---- detect --------------------------------------------------------
    p_detect = sub.add_parser(
        "detect",
        help="Detect drift for a named scenario and print the report.",
    )
    p_detect.add_argument(
        "--scenario",
        required=True,
        help="Drift scenario name (use list-scenarios to see available options).",
    )

    # ---- run -----------------------------------------------------------
    p_run = sub.add_parser(
        "run",
        help="Run the full orchestrator agent loop over drift scenarios.",
    )
    p_run.add_argument(
        "--scenarios",
        nargs="+",
        metavar="SCENARIO",
        default=None,
        help="One or more scenario names to run (default: all scenarios).",
    )
    p_run.add_argument(
        "--output-dir",
        default="output",
        help="Directory for run reports and audit logs (default: output).",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args   = parser.parse_args(argv)

    handlers = {
        "list-scenarios": cmd_list_scenarios,
        "etl":            cmd_etl,
        "detect":         cmd_detect,
        "run":            cmd_run,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
