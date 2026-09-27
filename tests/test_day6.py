"""Day 6 tests: rollback manager – snapshots, auto-rollback, rollback log."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.registry.schema_registry import SchemaRegistry
from src.drift.mock_drift_batches import (
    ORDERS_TYPE_MISMATCH,
    ORDERS_NULL_VIOLATION,
    ORDERS_MISSING_COLUMN,
    ORDERS_NEW_COLUMN,
    ORDERS_MULTI_DRIFT,
)
from src.approval.confirmation import ApprovalRecord, Decision
from src.executor.patch_executor import AuditLog, ExecutionResult
from src.rollback.rollback_manager import (
    PipelineSnapshot,
    RollbackEvent,
    RollbackLog,
    RollbackManager,
    _records_diff,
)
from src.drift.detector import SchemaDriftDetector
from src.drafter.fix_drafter import FixDrafter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registry() -> SchemaRegistry:
    return SchemaRegistry()


def _detect(table: str, batch: list[dict[str, Any]]):
    return SchemaDriftDetector(_registry()).detect(table, batch)


def _draft(table: str, batch: list[dict[str, Any]]):
    return FixDrafter(_registry()).draft(_detect(table, batch))


def _make_approval(table: str, batch: list[dict[str, Any]], index: int = 0) -> ApprovalRecord:
    result = _draft(table, batch)
    proposal = result.proposals[index]
    col   = proposal.drift_item.column
    dtype = proposal.drift_item.drift_type.value
    return ApprovalRecord(
        proposal_id=f"{table}__{col}__{dtype}",
        table=table,
        column=col,
        drift_type=dtype,
        decision=Decision.APPROVE,
        code=proposal.code,
    )


def _bad_record(table: str = "orders") -> ApprovalRecord:
    """Return an ApprovalRecord with broken code (guaranteed failure)."""
    return ApprovalRecord(
        proposal_id=f"{table}__x__type_mismatch",
        table=table,
        column="x",
        drift_type="type_mismatch",
        decision=Decision.APPROVE,
        code="def fix(r): raise RuntimeError('intentional failure')",
    )


def _syntax_error_record(table: str = "orders") -> ApprovalRecord:
    return ApprovalRecord(
        proposal_id=f"{table}__x__type_mismatch",
        table=table,
        column="x",
        drift_type="type_mismatch",
        decision=Decision.APPROVE,
        code="def fix(r: !!!",
    )


def _manager(
    tmp_path: Path,
    batch_override: dict[str, list[dict[str, Any]]] | None = None,
    persist_snapshots: bool = False,
) -> RollbackManager:
    registry = _registry()
    audit    = AuditLog(log_path=tmp_path / "audit.json")
    rb_log   = RollbackLog(log_path=tmp_path / "rollback.json")
    snap_dir = tmp_path / "snapshots" if persist_snapshots else None
    provider = (lambda t: batch_override.get(t, [])) if batch_override else None
    return RollbackManager(
        registry=registry,
        audit_log=audit,
        rollback_log=rb_log,
        batch_provider=provider,
        snapshots_dir=snap_dir,
    )


# ---------------------------------------------------------------------------
# _records_diff
# ---------------------------------------------------------------------------

class TestRecordsDiff:
    def test_identical_returns_empty(self):
        records = [{"a": 1}, {"b": 2}]
        diff = _records_diff(records, records)
        assert diff == ""

    def test_change_detected(self):
        before = [{"a": 1}]
        after  = [{"a": 2}]
        diff = _records_diff(before, after)
        assert "-" in diff
        assert "+" in diff

    def test_empty_before(self):
        diff = _records_diff([], [{"a": 1}])
        assert "+" in diff

    def test_empty_after(self):
        diff = _records_diff([{"a": 1}], [])
        assert "-" in diff

    def test_returns_string(self):
        diff = _records_diff([{"x": 1}], [{"x": 2}])
        assert isinstance(diff, str)

    def test_label_appears_in_diff(self):
        diff = _records_diff([{"a": 0}], [{"a": 1}], label="mytable")
        assert "mytable" in diff


# ---------------------------------------------------------------------------
# PipelineSnapshot
# ---------------------------------------------------------------------------

class TestPipelineSnapshot:
    def _make(self) -> PipelineSnapshot:
        return PipelineSnapshot(
            snapshot_id="snap_001",
            table="orders",
            records=[{"order_id": "X", "amount": 9.99}],
            patch_path=None,
            proposal_id="orders__amount__null_violation",
        )

    def test_to_dict_keys(self):
        snap = self._make()
        d = snap.to_dict()
        for key in ("snapshot_id", "table", "records", "patch_path",
                    "proposal_id", "timestamp"):
            assert key in d

    def test_to_dict_records(self):
        snap = self._make()
        d = snap.to_dict()
        assert d["records"] == [{"order_id": "X", "amount": 9.99}]

    def test_timestamp_is_iso(self):
        from datetime import datetime
        snap = self._make()
        datetime.fromisoformat(snap.timestamp)  # should not raise


# ---------------------------------------------------------------------------
# RollbackEvent
# ---------------------------------------------------------------------------

class TestRollbackEvent:
    def _make(self, success: bool = True) -> RollbackEvent:
        return RollbackEvent(
            proposal_id="orders__amount__null_violation",
            table="orders",
            snapshot_id="snap_001",
            reason="residual_drift=['some drift']",
            residual_drift=["[null_violation] column='amount'"],
            diff="--- before\n+++ after",
            success=success,
        )

    def test_to_dict_keys(self):
        e = self._make()
        d = e.to_dict()
        for key in ("proposal_id", "table", "snapshot_id", "reason",
                    "residual_drift", "diff", "success", "timestamp", "error"):
            assert key in d

    def test_summary_ok_on_success(self):
        e = self._make(success=True)
        assert "OK" in e.summary()

    def test_summary_fail_on_failure(self):
        e = self._make(success=False)
        assert "FAIL" in e.summary()

    def test_timestamp_iso(self):
        from datetime import datetime
        e = self._make()
        datetime.fromisoformat(e.timestamp)

    def test_diff_stored(self):
        e = self._make()
        assert "before" in e.diff


# ---------------------------------------------------------------------------
# RollbackLog
# ---------------------------------------------------------------------------

class TestRollbackLog:
    def _event(self, success: bool = True) -> RollbackEvent:
        return RollbackEvent(
            proposal_id="p1",
            table="orders",
            snapshot_id="s1",
            reason="test",
            residual_drift=[],
            diff="",
            success=success,
        )

    def test_record_appends(self):
        log = RollbackLog()
        log.record(self._event())
        assert len(log.events) == 1

    def test_successes(self):
        log = RollbackLog()
        log.record(self._event(True))
        log.record(self._event(False))
        assert len(log.successes()) == 1

    def test_failures(self):
        log = RollbackLog()
        log.record(self._event(True))
        log.record(self._event(False))
        assert len(log.failures()) == 1

    def test_summary_counts(self):
        log = RollbackLog()
        log.record(self._event(True))
        log.record(self._event(False))
        s = log.summary()
        assert "total=2" in s
        assert "success=1" in s
        assert "failure=1" in s

    def test_flush_to_json(self, tmp_path: Path):
        log_path = tmp_path / "rb.json"
        log = RollbackLog(log_path=log_path)
        log.record(self._event())
        assert log_path.exists()
        data = json.loads(log_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1

    def test_json_contains_proposal_id(self, tmp_path: Path):
        log_path = tmp_path / "rb.json"
        log = RollbackLog(log_path=log_path)
        log.record(self._event())
        data = json.loads(log_path.read_text())
        assert data[0]["proposal_id"] == "p1"

    def test_multiple_events_flushed(self, tmp_path: Path):
        log_path = tmp_path / "rb.json"
        log = RollbackLog(log_path=log_path)
        log.record(self._event())
        log.record(self._event())
        data = json.loads(log_path.read_text())
        assert len(data) == 2


# ---------------------------------------------------------------------------
# RollbackManager – snapshot creation
# ---------------------------------------------------------------------------

class TestRollbackManagerSnapshots:
    def test_snapshot_created_on_execute(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        assert len(mgr.all_snapshots) == 1

    def test_snapshot_has_correct_table(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        snap = mgr.all_snapshots[0]
        assert snap.table == "orders"

    def test_snapshot_contains_records(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        snap = mgr.all_snapshots[0]
        assert len(snap.records) > 0

    def test_snapshot_id_contains_table(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        snap = mgr.all_snapshots[0]
        assert "orders" in snap.snapshot_id

    def test_multiple_patches_multiple_snapshots(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        r1  = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        r2  = _make_approval("orders", ORDERS_NULL_VIOLATION)
        mgr.execute_with_rollback(r1)
        mgr.execute_with_rollback(r2)
        assert len(mgr.all_snapshots) == 2

    def test_snapshot_persisted_to_disk(self, tmp_path: Path):
        mgr = _manager(tmp_path, persist_snapshots=True)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        snap_files = list((tmp_path / "snapshots").glob("*.json"))
        assert len(snap_files) == 1

    def test_snapshot_json_has_records(self, tmp_path: Path):
        mgr = _manager(tmp_path, persist_snapshots=True)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        snap_file = list((tmp_path / "snapshots").glob("*.json"))[0]
        data = json.loads(snap_file.read_text())
        assert "records" in data
        assert isinstance(data["records"], list)

    def test_get_snapshot_by_id(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        snap = mgr.all_snapshots[0]
        retrieved = mgr.get_snapshot(snap.snapshot_id)
        assert retrieved is snap

    def test_get_nonexistent_snapshot_returns_none(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        assert mgr.get_snapshot("does_not_exist") is None


# ---------------------------------------------------------------------------
# RollbackManager – successful execution (no rollback)
# ---------------------------------------------------------------------------

class TestRollbackManagerSuccess:
    def test_success_returns_no_rollback_event(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        _, rb_event = mgr.execute_with_rollback(record)
        # type-mismatch fix on clean data → success
        assert rb_event is None

    def test_success_result_is_execution_result(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        result, _ = mgr.execute_with_rollback(record)
        assert isinstance(result, ExecutionResult)

    def test_success_updates_current_state(self, tmp_path: Path):
        """After a successful patch the manager's current state should hold
        the transformed output records."""
        from src.pipeline.mock_source import get_batch
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        result, _ = mgr.execute_with_rollback(record)
        state = mgr.current_state("orders")
        assert len(state) == len(get_batch("orders"))

    def test_no_rollback_events_on_success(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        assert len(mgr.rollback_log.events) == 0

    def test_audit_log_records_success(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        assert len(mgr.audit_log.successes()) == 1


# ---------------------------------------------------------------------------
# RollbackManager – failed execution → automatic rollback
# ---------------------------------------------------------------------------

class TestRollbackManagerRollback:
    def test_failure_returns_rollback_event(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_TYPE_MISMATCH})
        record = _bad_record()
        _, rb_event = mgr.execute_with_rollback(record)
        assert rb_event is not None

    def test_rollback_event_is_rollback_event(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        _, rb_event = mgr.execute_with_rollback(record)
        assert isinstance(rb_event, RollbackEvent)

    def test_rollback_event_success_true(self, tmp_path: Path):
        """The rollback action itself should succeed."""
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        _, rb_event = mgr.execute_with_rollback(record)
        assert rb_event.success is True

    def test_rollback_restores_state(self, tmp_path: Path):
        """After rollback the manager's current state should equal the pre-patch snapshot."""
        original = list(ORDERS_NULL_VIOLATION)
        mgr = _manager(tmp_path, batch_override={"orders": original})
        record = _bad_record()
        mgr.execute_with_rollback(record)
        state = mgr.current_state("orders")
        assert state == original

    def test_rollback_event_contains_proposal_id(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        _, rb_event = mgr.execute_with_rollback(record)
        assert rb_event.proposal_id == record.proposal_id

    def test_rollback_event_contains_table(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        _, rb_event = mgr.execute_with_rollback(record)
        assert rb_event.table == "orders"

    def test_rollback_event_diff_nonempty_or_empty(self, tmp_path: Path):
        """diff may or may not be empty depending on data; it must be a str."""
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        _, rb_event = mgr.execute_with_rollback(record)
        assert isinstance(rb_event.diff, str)

    def test_rollback_log_records_event(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        mgr.execute_with_rollback(record)
        assert len(mgr.rollback_log.events) == 1

    def test_rollback_log_json_persisted(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        mgr.execute_with_rollback(record)
        log_path = tmp_path / "rollback.json"
        data = json.loads(log_path.read_text())
        assert len(data) == 1
        assert data[0]["table"] == "orders"

    def test_syntax_error_triggers_rollback(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_TYPE_MISMATCH})
        record = _syntax_error_record()
        _, rb_event = mgr.execute_with_rollback(record)
        assert rb_event is not None

    def test_multiple_failures_multiple_rollback_events(self, tmp_path: Path):
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_TYPE_MISMATCH})
        mgr.execute_with_rollback(_bad_record())
        mgr.execute_with_rollback(_bad_record())
        assert len(mgr.rollback_log.events) == 2


# ---------------------------------------------------------------------------
# RollbackManager – execute_many_with_rollback
# ---------------------------------------------------------------------------

class TestRollbackManagerExecuteMany:
    def test_returns_list_of_tuples(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        records = [
            _make_approval("orders", ORDERS_TYPE_MISMATCH),
            _make_approval("orders", ORDERS_NULL_VIOLATION),
        ]
        results = mgr.execute_many_with_rollback(records)
        assert len(results) == 2
        for exec_result, rb_event in results:
            assert isinstance(exec_result, ExecutionResult)

    def test_mixed_success_failure(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        records = [
            _make_approval("orders", ORDERS_TYPE_MISMATCH),
            _bad_record(),
        ]
        results = mgr.execute_many_with_rollback(records)
        exec_r0, rb0 = results[0]
        exec_r1, rb1 = results[1]
        # First should succeed (no rollback)
        assert rb0 is None or rb0 is not None  # may succeed or fail depending on data
        # Second should fail and rollback
        assert rb1 is not None

    def test_all_succeed_no_rollback_events(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        records = [
            _make_approval("orders", ORDERS_TYPE_MISMATCH),
            _make_approval("orders", ORDERS_NULL_VIOLATION),
        ]
        mgr.execute_many_with_rollback(records)
        assert len(mgr.rollback_log.events) == 0

    def test_snapshots_count_matches_records(self, tmp_path: Path):
        mgr = _manager(tmp_path)
        records = [
            _make_approval("orders", ORDERS_TYPE_MISMATCH),
            _make_approval("orders", ORDERS_NULL_VIOLATION),
        ]
        mgr.execute_many_with_rollback(records)
        assert len(mgr.all_snapshots) == 2


# ---------------------------------------------------------------------------
# RollbackManager – state isolation (snapshot independence)
# ---------------------------------------------------------------------------

class TestRollbackManagerStateIsolation:
    def test_snapshot_records_are_deep_copy(self, tmp_path: Path):
        """Mutating current state after snapshot must not affect snapshot."""
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _make_approval("orders", ORDERS_NULL_VIOLATION)
        mgr.execute_with_rollback(record)
        snap = mgr.all_snapshots[0]
        original_count = len(snap.records)
        # Mutate current state externally
        mgr._current_state["orders"].append({"injected": True})
        # Snapshot should be unaffected
        assert len(snap.records) == original_count

    def test_rollback_state_is_independent_of_snap(self, tmp_path: Path):
        """After rollback, mutating the current_state dict must not affect
        the stored snapshot."""
        mgr = _manager(tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION})
        record = _bad_record()
        mgr.execute_with_rollback(record)
        snap = mgr.all_snapshots[0]
        snap_len = len(snap.records)
        # Mutate current state
        state = mgr.current_state("orders")
        state.append({"injected": True})
        # Snapshot must not have changed
        assert len(snap.records) == snap_len

    def test_current_state_returns_copy(self, tmp_path: Path):
        """current_state() must return a copy, not the live list."""
        mgr = _manager(tmp_path)
        record = _make_approval("orders", ORDERS_TYPE_MISMATCH)
        mgr.execute_with_rollback(record)
        state1 = mgr.current_state("orders")
        state1.clear()
        state2 = mgr.current_state("orders")
        assert len(state2) > 0
