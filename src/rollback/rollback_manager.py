"""Rollback manager: snapshots pipeline state before each patch application,
detects post-fix validation failures, automatically rolls back to the last
known-good snapshot, and logs rollback events with full diff history.
"""
from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.approval.confirmation import ApprovalRecord
from src.executor.patch_executor import AuditLog, ExecutionResult, PatchExecutor
from src.pipeline.mock_source import get_batch
from src.registry.schema_registry import SchemaRegistry


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@dataclass
class PipelineSnapshot:
    """A point-in-time capture of pipeline state for one table."""
    snapshot_id:  str
    table:        str
    records:      list[dict[str, Any]]
    patch_path:   str | None
    proposal_id:  str
    timestamp:    str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id":  self.snapshot_id,
            "table":        self.table,
            "records":      self.records,
            "patch_path":   self.patch_path,
            "proposal_id":  self.proposal_id,
            "timestamp":    self.timestamp,
        }


# ---------------------------------------------------------------------------
# Rollback event
# ---------------------------------------------------------------------------

@dataclass
class RollbackEvent:
    """Structured log entry for a rollback action."""
    proposal_id:      str
    table:            str
    snapshot_id:      str
    reason:           str
    residual_drift:   list[str]
    diff:             str            # unified diff between failed and rolled-back state
    success:          bool           # did the rollback itself succeed?
    timestamp:        str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error:            str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id":    self.proposal_id,
            "table":          self.table,
            "snapshot_id":    self.snapshot_id,
            "reason":         self.reason,
            "residual_drift": self.residual_drift,
            "diff":           self.diff,
            "success":        self.success,
            "timestamp":      self.timestamp,
            "error":          self.error,
        }

    def summary(self) -> str:
        status = "OK" if self.success else "FAIL"
        return (
            f"[RollbackEvent] {status}"
            f"  proposal={self.proposal_id}"
            f"  snapshot={self.snapshot_id}"
            f"  reason={self.reason!r}"
        )


# ---------------------------------------------------------------------------
# Rollback log
# ---------------------------------------------------------------------------

class RollbackLog:
    """Append-only in-memory rollback event log with optional JSON persistence."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path
        self._events: list[RollbackEvent] = []

    def record(self, event: RollbackEvent) -> None:
        self._events.append(event)
        print(f"[rollback] {event.summary()}")
        if self.log_path is not None:
            self._flush()

    def _flush(self) -> None:
        assert self.log_path is not None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        data = [e.to_dict() for e in self._events]
        self.log_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def events(self) -> list[RollbackEvent]:
        return list(self._events)

    def successes(self) -> list[RollbackEvent]:
        return [e for e in self._events if e.success]

    def failures(self) -> list[RollbackEvent]:
        return [e for e in self._events if not e.success]

    def summary(self) -> str:
        return (
            f"[RollbackLog] total={len(self._events)}"
            f"  success={len(self.successes())}"
            f"  failure={len(self.failures())}"
        )


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def _records_diff(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    label: str = "records",
) -> str:
    """Produce a unified diff of JSON-serialised record lists."""
    before_text = json.dumps(before, indent=2, default=str).splitlines(keepends=True)
    after_text  = json.dumps(after,  indent=2, default=str).splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        before_text,
        after_text,
        fromfile=f"{label}_before",
        tofile=f"{label}_after",
        lineterm="",
    )
    return "\n".join(diff_lines)


# ---------------------------------------------------------------------------
# RollbackManager
# ---------------------------------------------------------------------------

class RollbackManager:
    """Wrap a :class:`PatchExecutor` with snapshot + automatic rollback logic.

    For each :meth:`execute_with_rollback` call the manager:

    1. Takes a snapshot of the current (pre-fix) table records.
    2. Delegates execution to the inner :class:`PatchExecutor`.
    3. If the execution result reports *failure* (residual drift or error),
       restores the snapshot and emits a :class:`RollbackEvent`.
    4. All snapshots and rollback events are retained in memory and optionally
       persisted to disk.
    """

    def __init__(
        self,
        registry: SchemaRegistry,
        audit_log: AuditLog | None = None,
        rollback_log: RollbackLog | None = None,
        *,
        batch_provider=None,
        snapshots_dir: Path | None = None,
    ) -> None:
        self._registry      = registry
        self._rollback_log  = rollback_log or RollbackLog()
        self._snapshots: dict[str, PipelineSnapshot] = {}
        # Live mutable state – each table's current records.
        self._current_state: dict[str, list[dict[str, Any]]] = {}
        # Snapshots directory for optional persistence.
        self._snapshots_dir = snapshots_dir

        # The inner executor uses *this* manager's current_state as data source
        # so that successive patches operate on the latest transformed records.
        self._batch_provider = batch_provider or get_batch

        self._audit_log = audit_log or AuditLog()
        self._executor = PatchExecutor(
            registry=registry,
            audit_log=self._audit_log,
            batch_provider=self._get_current_batch,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute_with_rollback(
        self, record: ApprovalRecord
    ) -> tuple[ExecutionResult, RollbackEvent | None]:
        """Execute *record* with automatic rollback on failure.

        Returns ``(execution_result, rollback_event_or_None)``.
        ``rollback_event`` is None when execution succeeded (no rollback needed).
        """
        table = record.table

        # 1. Ensure current_state is initialised for this table.
        if table not in self._current_state:
            try:
                self._current_state[table] = [
                    row.copy() for row in self._batch_provider(table)
                ]
            except Exception as exc:  # noqa: BLE001
                # Can't even load the initial state – execute anyway and let
                # the executor handle it.
                self._current_state[table] = []
                print(f"[rollback] Warning: could not pre-load batch for {table!r}: {exc}")

        # 2. Take snapshot of pre-fix state.
        snapshot = self._take_snapshot(record)

        # 3. Execute the patch.
        result = self._executor.execute(record)

        # 4. If execution succeeded, update current state; else rollback.
        if result.success:
            # Commit the new output records as the current live state.
            if result.output_records:
                self._current_state[table] = [
                    row.copy() for row in result.output_records
                ]
            print(
                f"[rollback] Patch succeeded for proposal={record.proposal_id!r};"
                f" state updated."
            )
            return result, None
        else:
            # Rollback.
            rollback_event = self._rollback(record, result, snapshot)
            return result, rollback_event

    def execute_many_with_rollback(
        self, records: list[ApprovalRecord]
    ) -> list[tuple[ExecutionResult, RollbackEvent | None]]:
        """Execute a sequence of records, rolling back failures individually."""
        return [self.execute_with_rollback(r) for r in records]

    # ------------------------------------------------------------------
    # Snapshot management
    # ------------------------------------------------------------------

    def _take_snapshot(self, record: ApprovalRecord) -> PipelineSnapshot:
        table = record.table
        ts    = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        sid   = f"{table}__{record.proposal_id}__{ts}"

        snap = PipelineSnapshot(
            snapshot_id=sid,
            table=table,
            records=copy.deepcopy(self._current_state.get(table, [])),
            patch_path=record.patch_path,
            proposal_id=record.proposal_id,
        )
        self._snapshots[sid] = snap
        print(f"[rollback] Snapshot taken: {sid}  rows={len(snap.records)}")

        if self._snapshots_dir is not None:
            self._persist_snapshot(snap)

        return snap

    def _persist_snapshot(self, snap: PipelineSnapshot) -> None:
        assert self._snapshots_dir is not None
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        path = self._snapshots_dir / f"{snap.snapshot_id}.json"
        path.write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
        print(f"[rollback] Snapshot persisted: {path}")

    def get_snapshot(self, snapshot_id: str) -> PipelineSnapshot | None:
        return self._snapshots.get(snapshot_id)

    @property
    def all_snapshots(self) -> list[PipelineSnapshot]:
        return list(self._snapshots.values())

    # ------------------------------------------------------------------
    # Rollback logic
    # ------------------------------------------------------------------

    def _rollback(
        self,
        record: ApprovalRecord,
        failed_result: ExecutionResult,
        snapshot: PipelineSnapshot,
    ) -> RollbackEvent:
        table = record.table

        # Compute diff: failed output vs. snapshot (pre-fix) records.
        failed_records = failed_result.output_records or self._current_state.get(table, [])
        diff = _records_diff(
            failed_records,
            snapshot.records,
            label=f"{table}.rollback",
        )

        reason = (
            f"execution_failed: error={failed_result.error!r}"
            if failed_result.error
            else f"residual_drift={failed_result.residual_drift}"
        )

        try:
            # Restore the pre-fix state.
            self._current_state[table] = copy.deepcopy(snapshot.records)
            print(
                f"[rollback] Rolled back table={table!r} to snapshot={snapshot.snapshot_id!r}"
                f"  rows={len(snapshot.records)}"
            )
            rollback_ok = True
            rollback_error = ""
        except Exception as exc:  # noqa: BLE001
            rollback_ok    = False
            rollback_error = str(exc)
            print(f"[rollback] ERROR during rollback: {exc}")

        event = RollbackEvent(
            proposal_id=record.proposal_id,
            table=table,
            snapshot_id=snapshot.snapshot_id,
            reason=reason,
            residual_drift=failed_result.residual_drift,
            diff=diff,
            success=rollback_ok,
            error=rollback_error,
        )
        self._rollback_log.record(event)
        return event

    # ------------------------------------------------------------------
    # Internal batch provider for the executor
    # ------------------------------------------------------------------

    def _get_current_batch(self, table: str) -> list[dict[str, Any]]:
        """Return the current live records for *table*.

        Falls back to the original batch_provider if no state exists yet.
        """
        if table in self._current_state:
            return [row.copy() for row in self._current_state[table]]
        return self._batch_provider(table)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    @property
    def rollback_log(self) -> RollbackLog:
        return self._rollback_log

    @property
    def audit_log(self) -> AuditLog:
        return self._audit_log

    def current_state(self, table: str) -> list[dict[str, Any]]:
        """Return a copy of the current live records for *table*."""
        return copy.deepcopy(self._current_state.get(table, []))
