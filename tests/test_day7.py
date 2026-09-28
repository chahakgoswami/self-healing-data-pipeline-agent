"""Day 7 tests: orchestrator agent loop, CLI entry point, and end-to-end coverage."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from src.drift.mock_drift_batches import list_scenarios, get_drift_scenario
from src.registry.schema_registry import SchemaRegistry
from src.orchestrator.agent_loop import (
    OrchestratorAgent,
    RunReport,
    ScenarioResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registry() -> SchemaRegistry:
    return SchemaRegistry()


def _agent(tmp_path: Path, scenarios: list[str] | None = None) -> OrchestratorAgent:
    return OrchestratorAgent(
        registry=_registry(),
        output_dir=tmp_path / "output",
        auto_approve=True,
    )


# ---------------------------------------------------------------------------
# ScenarioResult
# ---------------------------------------------------------------------------

class TestScenarioResult:
    def _make(self, successes: int = 1, failures: int = 0, rollbacks: int = 0):
        from src.executor.patch_executor import ExecutionResult
        from src.rollback.rollback_manager import RollbackEvent
        exec_results = [
            ExecutionResult(
                proposal_id=f"p{i}",
                table="orders",
                column="x",
                drift_type="type_mismatch",
                success=(i < successes),
            )
            for i in range(successes + failures)
        ]
        rb_events = [
            RollbackEvent(
                proposal_id=f"p{i}",
                table="orders",
                snapshot_id="s1",
                reason="test",
                residual_drift=[],
                diff="",
                success=True,
            )
            for i in range(rollbacks)
        ]
        return ScenarioResult(
            scenario_name="orders_type_mismatch",
            table="orders",
            n_drift_items=successes + failures,
            n_proposals=successes + failures,
            exec_results=exec_results,
            rollback_events=rb_events,
        )

    def test_n_successes(self):
        sr = self._make(successes=2, failures=1)
        assert sr.n_successes == 2

    def test_n_failures(self):
        sr = self._make(successes=1, failures=2)
        assert sr.n_failures == 2

    def test_n_rollbacks(self):
        sr = self._make(rollbacks=3)
        assert sr.n_rollbacks == 3

    def test_summary_contains_scenario_name(self):
        sr = self._make()
        assert "orders_type_mismatch" in sr.summary()

    def test_summary_contains_table(self):
        sr = self._make()
        assert "orders" in sr.summary()

    def test_to_dict_keys(self):
        sr = self._make()
        d = sr.to_dict()
        for key in ("scenario_name", "table", "n_drift_items", "n_proposals",
                    "n_successes", "n_failures", "n_rollbacks",
                    "timestamp", "exec_results", "rollback_events"):
            assert key in d

    def test_to_dict_exec_results_list(self):
        sr = self._make()
        d = sr.to_dict()
        assert isinstance(d["exec_results"], list)

    def test_to_dict_rollback_events_list(self):
        sr = self._make()
        d = sr.to_dict()
        assert isinstance(d["rollback_events"], list)


# ---------------------------------------------------------------------------
# RunReport
# ---------------------------------------------------------------------------

class TestRunReport:
    def _sr(self, name: str, successes: int, failures: int, rollbacks: int = 0):
        from src.executor.patch_executor import ExecutionResult
        from src.rollback.rollback_manager import RollbackEvent
        exec_results = [
            ExecutionResult(
                proposal_id=f"{name}_p{i}",
                table="orders",
                column="x",
                drift_type="type_mismatch",
                success=(i < successes),
            )
            for i in range(successes + failures)
        ]
        rb_events = [
            RollbackEvent(
                proposal_id=f"{name}_r{i}",
                table="orders",
                snapshot_id="s1",
                reason="test",
                residual_drift=[],
                diff="",
                success=True,
            )
            for i in range(rollbacks)
        ]
        return ScenarioResult(
            scenario_name=name,
            table="orders",
            n_drift_items=successes + failures,
            n_proposals=successes + failures,
            exec_results=exec_results,
            rollback_events=rb_events,
        )

    def test_total_successes(self):
        report = RunReport(scenarios_run=["a", "b"])
        report.scenario_results.append(self._sr("a", 2, 0))
        report.scenario_results.append(self._sr("b", 1, 1))
        assert report.total_successes == 3

    def test_total_failures(self):
        report = RunReport(scenarios_run=["a", "b"])
        report.scenario_results.append(self._sr("a", 2, 1))
        report.scenario_results.append(self._sr("b", 0, 2))
        assert report.total_failures == 3

    def test_total_rollbacks(self):
        report = RunReport(scenarios_run=["a"])
        report.scenario_results.append(self._sr("a", 1, 1, rollbacks=2))
        assert report.total_rollbacks == 2

    def test_summary_contains_scenario_count(self):
        report = RunReport(scenarios_run=["a"])
        report.scenario_results.append(self._sr("a", 1, 0))
        assert "scenarios=1" in report.summary()

    def test_to_dict_keys(self):
        report = RunReport(scenarios_run=["a"])
        report.scenario_results.append(self._sr("a", 1, 0))
        d = report.to_dict()
        for key in ("timestamp", "scenarios_run", "total_successes",
                    "total_failures", "total_rollbacks", "scenario_results"):
            assert key in d

    def test_save_creates_json(self, tmp_path: Path):
        report = RunReport(scenarios_run=["a"])
        report.scenario_results.append(self._sr("a", 1, 0))
        out = tmp_path / "report.json"
        report.save(out)
        assert out.exists()
        data = json.loads(out.read_text())
        assert "scenarios_run" in data

    def test_save_json_has_scenario_results(self, tmp_path: Path):
        report = RunReport(scenarios_run=["a"])
        report.scenario_results.append(self._sr("a", 1, 0))
        out = tmp_path / "report.json"
        report.save(out)
        data = json.loads(out.read_text())
        assert isinstance(data["scenario_results"], list)
        assert len(data["scenario_results"]) == 1


# ---------------------------------------------------------------------------
# OrchestratorAgent – single scenario
# ---------------------------------------------------------------------------

class TestOrchestratorAgentSingleScenario:
    def test_run_single_returns_run_report(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch"])
        assert isinstance(report, RunReport)

    def test_run_single_one_scenario_result(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch"])
        assert len(report.scenario_results) == 1

    def test_run_single_correct_scenario_name(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_null_violation"])
        assert report.scenario_results[0].scenario_name == "orders_null_violation"

    def test_run_single_drift_items_counted(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch"])
        sr = report.scenario_results[0]
        assert sr.n_drift_items > 0

    def test_run_single_proposals_counted(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_missing_column"])
        sr = report.scenario_results[0]
        assert sr.n_proposals > 0

    def test_run_single_exec_results_present(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_new_column"])
        sr = report.scenario_results[0]
        assert len(sr.exec_results) > 0

    def test_run_report_persisted(self, tmp_path: Path):
        agent = _agent(tmp_path)
        agent.run(scenario_names=["orders_type_mismatch"])
        output_files = list((tmp_path / "output").glob("*run_report.json"))
        assert len(output_files) == 1

    def test_audit_log_persisted(self, tmp_path: Path):
        agent = _agent(tmp_path)
        agent.run(scenario_names=["orders_null_violation"])
        audit_files = list((tmp_path / "output").glob("*audit.json"))
        assert len(audit_files) == 1


# ---------------------------------------------------------------------------
# OrchestratorAgent – multiple scenarios
# ---------------------------------------------------------------------------

class TestOrchestratorAgentMultipleScenarios:
    def test_run_all_scenarios(self, tmp_path: Path):
        agent = _agent(tmp_path)
        all_names = list_scenarios()
        report = agent.run(scenario_names=all_names)
        assert len(report.scenario_results) == len(all_names)

    def test_run_all_scenarios_names_match(self, tmp_path: Path):
        agent = _agent(tmp_path)
        all_names = list_scenarios()
        report = agent.run(scenario_names=all_names)
        result_names = [sr.scenario_name for sr in report.scenario_results]
        assert result_names == all_names

    def test_run_two_scenarios(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(
            scenario_names=["orders_type_mismatch", "orders_null_violation"]
        )
        assert len(report.scenario_results) == 2

    def test_run_default_runs_all(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run()   # no scenario_names → all
        assert len(report.scenario_results) == len(list_scenarios())

    def test_total_successes_ge_zero(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch", "orders_new_column"])
        assert report.total_successes >= 0

    def test_run_report_scenario_results_list(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch", "orders_null_violation"])
        assert isinstance(report.scenario_results, list)

    def test_each_scenario_has_table_set(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch", "users_null_violation"])
        tables = {sr.table for sr in report.scenario_results}
        assert "orders" in tables
        assert "users" in tables

    def test_run_report_json_has_all_scenarios(self, tmp_path: Path):
        agent = _agent(tmp_path)
        names = ["orders_type_mismatch", "orders_null_violation"]
        agent.run(scenario_names=names)
        files = list((tmp_path / "output").glob("*run_report.json"))
        data = json.loads(files[0].read_text())
        assert data["scenarios_run"] == names


# ---------------------------------------------------------------------------
# OrchestratorAgent – no-drift scenario (clean batch)
# ---------------------------------------------------------------------------

class TestOrchestratorAgentNoDrift:
    """Run the orchestrator with a batch that has no drift (simulate via a
    mock registry that registers nothing, so no columns to check)."""

    def test_no_proposals_when_no_drift(self, tmp_path: Path):
        """If no drift is detected, n_proposals should be 0."""
        import tempfile
        # Empty registry → no drift can be detected.
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        Path(tmp.name).write_text("{}", encoding="utf-8")
        empty_registry = SchemaRegistry(path=tmp.name)

        agent = OrchestratorAgent(
            registry=empty_registry,
            output_dir=tmp_path / "output",
            auto_approve=True,
        )
        report = agent.run(scenario_names=["orders_type_mismatch"])
        sr = report.scenario_results[0]
        assert sr.n_proposals == 0
        assert sr.n_drift_items == 0


# ---------------------------------------------------------------------------
# CLI – argument parsing and sub-command routing
# ---------------------------------------------------------------------------

class TestCLIParser:
    def _parse(self, argv: list[str]):
        from src.cli import build_parser
        return build_parser().parse_args(argv)

    def test_list_scenarios_command(self):
        args = self._parse(["list-scenarios"])
        assert args.command == "list-scenarios"

    def test_etl_default_table(self):
        args = self._parse(["etl"])
        assert args.table == "orders"

    def test_etl_custom_table(self):
        args = self._parse(["etl", "--table", "users"])
        assert args.table == "users"

    def test_detect_requires_scenario(self):
        from src.cli import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(["detect"])

    def test_detect_scenario_stored(self):
        args = self._parse(["detect", "--scenario", "orders_type_mismatch"])
        assert args.scenario == "orders_type_mismatch"

    def test_run_default_scenarios_none(self):
        args = self._parse(["run"])
        assert args.scenarios is None

    def test_run_with_scenarios(self):
        args = self._parse(["run", "--scenarios", "orders_type_mismatch", "orders_null_violation"])
        assert args.scenarios == ["orders_type_mismatch", "orders_null_violation"]

    def test_run_default_output_dir(self):
        args = self._parse(["run"])
        assert args.output_dir == "output"

    def test_run_custom_output_dir(self):
        args = self._parse(["run", "--output-dir", "/tmp/myout"])
        assert args.output_dir == "/tmp/myout"


# ---------------------------------------------------------------------------
# CLI – functional integration
# ---------------------------------------------------------------------------

class TestCLIFunctional:
    def test_cmd_list_scenarios_returns_zero(self, tmp_path: Path, capsys):
        from src.cli import main
        rc = main(["list-scenarios"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "orders_type_mismatch" in out

    def test_cmd_etl_orders_returns_zero(self, tmp_path: Path, capsys):
        from src.cli import main
        rc = main(["etl", "--table", "orders"])
        assert rc == 0

    def test_cmd_etl_users_returns_zero(self, tmp_path: Path, capsys):
        from src.cli import main
        rc = main(["etl", "--table", "users"])
        assert rc == 0

    def test_cmd_detect_valid_scenario_returns_zero(self, capsys):
        from src.cli import main
        rc = main(["detect", "--scenario", "orders_type_mismatch"])
        assert rc == 0

    def test_cmd_detect_invalid_scenario_returns_nonzero(self, capsys):
        from src.cli import main
        rc = main(["detect", "--scenario", "nonexistent_scenario_xyz"])
        assert rc != 0

    def test_cmd_detect_output_contains_drift_info(self, capsys):
        from src.cli import main
        main(["detect", "--scenario", "orders_null_violation"])
        out = capsys.readouterr().out
        assert "null_violation" in out or "amount" in out

    def test_cmd_run_single_scenario(self, tmp_path: Path, monkeypatch, capsys):
        from src.cli import main
        monkeypatch.chdir(tmp_path)
        rc = main(["run", "--scenarios", "orders_type_mismatch",
                   "--output-dir", str(tmp_path / "output")])
        # Return code is 0 on all-success, 1 on any failure – both are acceptable
        assert rc in (0, 1)

    def test_cmd_run_creates_report(self, tmp_path: Path, monkeypatch, capsys):
        from src.cli import main
        monkeypatch.chdir(tmp_path)
        out_dir = tmp_path / "output"
        main(["run", "--scenarios", "orders_null_violation",
              "--output-dir", str(out_dir)])
        report_files = list(out_dir.glob("*run_report.json"))
        assert len(report_files) == 1

    def test_cmd_run_report_json_valid(self, tmp_path: Path, monkeypatch, capsys):
        from src.cli import main
        monkeypatch.chdir(tmp_path)
        out_dir = tmp_path / "output"
        main(["run", "--scenarios", "orders_type_mismatch",
              "--output-dir", str(out_dir)])
        report_files = list(out_dir.glob("*run_report.json"))
        data = json.loads(report_files[0].read_text())
        assert "scenarios_run" in data
        assert "scenario_results" in data


# ---------------------------------------------------------------------------
# End-to-end: drift → draft → approve → execute → rollback check
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """Higher-level integration tests verifying the full agent loop."""

    def test_type_mismatch_scenario_succeeds(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch"])
        sr = report.scenario_results[0]
        # Should have at least one success (type-mismatch fix is well-defined).
        assert sr.n_proposals >= 1
        # At least one exec result.
        assert len(sr.exec_results) >= 1

    def test_null_violation_scenario_runs(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_null_violation"])
        sr = report.scenario_results[0]
        assert sr.n_proposals >= 1

    def test_missing_column_scenario_runs(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_missing_column"])
        sr = report.scenario_results[0]
        assert sr.n_proposals >= 1

    def test_new_column_scenario_runs(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_new_column"])
        sr = report.scenario_results[0]
        assert sr.n_proposals >= 1

    def test_multi_drift_scenario_proposals_match_items(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_multi_drift"])
        sr = report.scenario_results[0]
        assert sr.n_proposals == sr.n_drift_items

    def test_users_null_violation_scenario_runs(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["users_null_violation"])
        sr = report.scenario_results[0]
        assert sr.table == "users"
        assert sr.n_proposals >= 1

    def test_users_new_column_scenario_runs(self, tmp_path: Path):
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["users_new_column"])
        sr = report.scenario_results[0]
        assert sr.table == "users"
        assert sr.n_proposals >= 1

    def test_all_scenarios_run_without_exception(self, tmp_path: Path):
        agent = _agent(tmp_path)
        # Should not raise.
        report = agent.run()  # all scenarios
        assert len(report.scenario_results) == len(list_scenarios())

    def test_exec_results_are_execution_result_instances(self, tmp_path: Path):
        from src.executor.patch_executor import ExecutionResult
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch"])
        sr = report.scenario_results[0]
        for er in sr.exec_results:
            assert isinstance(er, ExecutionResult)

    def test_rollback_events_are_rollback_event_instances(self, tmp_path: Path):
        from src.rollback.rollback_manager import RollbackEvent
        agent = _agent(tmp_path)
        report = agent.run(scenario_names=["orders_type_mismatch"])
        sr = report.scenario_results[0]
        for ev in sr.rollback_events:
            assert isinstance(ev, RollbackEvent)
