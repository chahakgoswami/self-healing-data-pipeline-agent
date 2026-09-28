"""End-to-end orchestrator agent loop.

Cycles through multiple mock drift scenarios automatically:
  1. Detect drift in each scenario batch.
  2. Draft fix proposals.
  3. Auto-approve all proposals (non-interactive mode).
  4. Execute patches via the RollbackManager (with automatic rollback on failure).
  5. Collect and return a structured run report.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.approval.confirmation import ApprovalRecord, Decision
from src.drift.detector import SchemaDriftDetector
from src.drift.mock_drift_batches import get_drift_scenario, list_scenarios
from src.drafter.fix_drafter import FixDrafter
from src.executor.patch_executor import AuditLog, ExecutionResult
from src.registry.schema_registry import SchemaRegistry
from src.rollback.rollback_manager import RollbackEvent, RollbackLog, RollbackManager


# ---------------------------------------------------------------------------
# Per-scenario result
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    """Outcome of running the agent loop against one drift scenario."""
    scenario_name:  str
    table:          str
    n_drift_items:  int
    n_proposals:    int
    exec_results:   list[ExecutionResult] = field(default_factory=list)
    rollback_events: list[RollbackEvent]  = field(default_factory=list)
    timestamp:      str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def n_successes(self) -> int:
        return sum(1 for r in self.exec_results if r.success)

    @property
    def n_failures(self) -> int:
        return sum(1 for r in self.exec_results if not r.success)

    @property
    def n_rollbacks(self) -> int:
        return len(self.rollback_events)

    def summary(self) -> str:
        return (
            f"[ScenarioResult] scenario={self.scenario_name!r}"
            f"  table={self.table!r}"
            f"  drift={self.n_drift_items}"
            f"  proposals={self.n_proposals}"
            f"  success={self.n_successes}"
            f"  failure={self.n_failures}"
            f"  rollbacks={self.n_rollbacks}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_name":   self.scenario_name,
            "table":           self.table,
            "n_drift_items":   self.n_drift_items,
            "n_proposals":     self.n_proposals,
            "n_successes":     self.n_successes,
            "n_failures":      self.n_failures,
            "n_rollbacks":     self.n_rollbacks,
            "timestamp":       self.timestamp,
            "exec_results":    [r.to_dict() for r in self.exec_results],
            "rollback_events": [e.to_dict() for e in self.rollback_events],
        }


# ---------------------------------------------------------------------------
# Full run report
# ---------------------------------------------------------------------------

@dataclass
class RunReport:
    """Aggregated report for an entire orchestrator run."""
    scenarios_run:    list[str]
    scenario_results: list[ScenarioResult] = field(default_factory=list)
    timestamp:        str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def total_successes(self) -> int:
        return sum(r.n_successes for r in self.scenario_results)

    @property
    def total_failures(self) -> int:
        return sum(r.n_failures for r in self.scenario_results)

    @property
    def total_rollbacks(self) -> int:
        return sum(r.n_rollbacks for r in self.scenario_results)

    def summary(self) -> str:
        lines = [
            f"[RunReport] scenarios={len(self.scenario_results)}"
            f"  total_success={self.total_successes}"
            f"  total_failure={self.total_failures}"
            f"  total_rollbacks={self.total_rollbacks}",
        ]
        for sr in self.scenario_results:
            lines.append(f"  {sr.summary()}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp":        self.timestamp,
            "scenarios_run":    self.scenarios_run,
            "total_successes":  self.total_successes,
            "total_failures":   self.total_failures,
            "total_rollbacks":  self.total_rollbacks,
            "scenario_results": [r.to_dict() for r in self.scenario_results],
        }

    def save(self, path: Path) -> None:
        """Persist the report as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        print(f"[orchestrator] Run report saved to: {path}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class OrchestratorAgent:
    """Agent that runs the full self-healing pipeline loop over selected scenarios.

    In non-interactive (automatic) mode every proposal is auto-approved so the
    loop can run without human input.  This is the default for CLI usage and
    testing.  Interactive mode is a future extension.
    """

    def __init__(
        self,
        registry: SchemaRegistry | None = None,
        *,
        output_dir: Path = Path("output"),
        auto_approve: bool = True,
    ) -> None:
        self._registry    = registry or SchemaRegistry()
        self._output_dir  = output_dir
        self._auto_approve = auto_approve

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, scenario_names: list[str] | None = None) -> RunReport:
        """Run the agent loop over *scenario_names* (all scenarios if None).

        Returns a :class:`RunReport` summarising the outcomes.
        """
        names = scenario_names or list_scenarios()
        report = RunReport(scenarios_run=names)

        print()
        print("=" * 70)
        print(f"  ORCHESTRATOR AGENT  –  {len(names)} scenario(s)")
        print("=" * 70)

        for name in names:
            print(f"\n>>> Scenario: {name}")
            sr = self._run_scenario(name)
            report.scenario_results.append(sr)
            print(sr.summary())

        print()
        print(report.summary())

        # Persist the report.
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        report.save(self._output_dir / f"{ts}__run_report.json")

        return report

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_scenario(self, name: str) -> ScenarioResult:
        """Run a single drift scenario through the full pipeline."""
        table, batch = get_drift_scenario(name)

        # 1. Detect drift.
        detector = SchemaDriftDetector(self._registry)
        drift_report = detector.detect(table, batch)
        print(f"  [detect] {drift_report.summary()}")

        # 2. Draft fixes.
        drafter = FixDrafter(self._registry)
        draft_result = drafter.draft(drift_report)
        print(f"  [draft]  {draft_result.summary()}")

        if not draft_result.has_proposals:
            return ScenarioResult(
                scenario_name=name,
                table=table,
                n_drift_items=len(drift_report.items),
                n_proposals=0,
            )

        # 3. Build approval records (auto-approve all).
        approval_records = self._build_approvals(table, draft_result)

        # 4. Execute with rollback, using the drifted batch as source.
        audit_log    = AuditLog(log_path=self._output_dir / f"{name}__audit.json")
        rollback_log = RollbackLog(log_path=self._output_dir / f"{name}__rollback.json")
        manager      = RollbackManager(
            registry=self._registry,
            audit_log=audit_log,
            rollback_log=rollback_log,
            batch_provider=lambda t, _b=batch: _b,  # use the drifted batch
        )

        pairs = manager.execute_many_with_rollback(approval_records)

        exec_results    = [pair[0] for pair in pairs]
        rollback_events = [pair[1] for pair in pairs if pair[1] is not None]

        return ScenarioResult(
            scenario_name=name,
            table=table,
            n_drift_items=len(drift_report.items),
            n_proposals=len(draft_result.proposals),
            exec_results=exec_results,
            rollback_events=rollback_events,
        )

    def _build_approvals(
        self,
        table: str,
        draft_result,
    ) -> list[ApprovalRecord]:
        """Convert all proposals to auto-approved ApprovalRecords."""
        records: list[ApprovalRecord] = []
        for proposal in draft_result.proposals:
            col   = proposal.drift_item.column
            dtype = proposal.drift_item.drift_type.value
            records.append(
                ApprovalRecord(
                    proposal_id=f"{table}__{col}__{dtype}",
                    table=table,
                    column=col,
                    drift_type=dtype,
                    decision=Decision.APPROVE,
                    code=proposal.code,
                )
            )
        return records
