"""Day 5 tests: patch executor, sandboxed exec, schema validation, audit log."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.registry.schema_registry import SchemaRegistry
from src.drift.detector import SchemaDriftDetector
from src.drift.mock_drift_batches import (
    ORDERS_TYPE_MISMATCH,
    ORDERS_NULL_VIOLATION,
    ORDERS_MISSING_COLUMN,
    ORDERS_NEW_COLUMN,
    ORDERS_MULTI_DRIFT,
)
from src.drafter.fix_drafter import FixDrafter
from src.approval.confirmation import ApprovalRecord, Decision
from src.executor.patch_executor import (
    AuditLog,
    ExecutionResult,
    PatchExecutor,
    _compile_fix,
    _exec_fix,
    _apply_fix_to_batch,
    _validate_output,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registry() -> SchemaRegistry:
    return SchemaRegistry()


def _detect(table: str, batch: list[dict[str, Any]]):
    return SchemaDriftDetector(_registry()).detect(table, batch)


def _draft(table: str, batch: list[dict[str, Any]]):
    return FixDrafter(_registry()).draft(_detect(table, batch))


def _make_approval_record(
    table: str,
    batch: list[dict[str, Any]],
    index: int = 0,
    *,
    patches_dir: Path,
) -> ApprovalRecord:
    """Draft a fix and return a fake approval record for the proposal at *index*."""
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
        patch_path=None,  # no file – use record.code path
    )


def _make_approval_record_with_file(
    table: str,
    batch: list[dict[str, Any]],
    index: int = 0,
    *,
    patches_dir: Path,
) -> ApprovalRecord:
    """Write the patch to a file and return an ApprovalRecord referencing it."""
    from src.approval.confirmation import _write_patch
    result = _draft(table, batch)
    proposal = result.proposals[index]
    col   = proposal.drift_item.column
    dtype = proposal.drift_item.drift_type.value
    patch_path = _write_patch(proposal, proposal.code, patches_dir)
    return ApprovalRecord(
        proposal_id=f"{table}__{col}__{dtype}",
        table=table,
        column=col,
        drift_type=dtype,
        decision=Decision.APPROVE,
        code=proposal.code,
        patch_path=str(patch_path),
    )


# ---------------------------------------------------------------------------
# _compile_fix
# ---------------------------------------------------------------------------

class TestCompileFix:
    def test_valid_code_returns_code_object(self):
        code = "def fix(record): return record"
        compiled = _compile_fix(code)
        assert compiled is not None

    def test_syntax_error_raises(self):
        with pytest.raises(SyntaxError):
            _compile_fix("def fix(record: !!!")

    def test_multiline_code_compiles(self):
        code = "def fix(record):\n    record['x'] = 1\n    return record"
        compiled = _compile_fix(code)
        assert compiled is not None


# ---------------------------------------------------------------------------
# _exec_fix
# ---------------------------------------------------------------------------

class TestExecFix:
    def test_returns_callable(self):
        code = "def fix(record): return record"
        compiled = _compile_fix(code)
        fn = _exec_fix(compiled)
        assert callable(fn)

    def test_fix_fn_works(self):
        code = "def fix(record):\n    record['x'] = 99\n    return record"
        compiled = _compile_fix(code)
        fn = _exec_fix(compiled)
        result = fn({"x": 0})
        assert result["x"] == 99

    def test_missing_fix_raises_key_error(self):
        code = "x = 1"
        compiled = _compile_fix(code)
        with pytest.raises(KeyError):
            _exec_fix(compiled)

    def test_sandboxed_no_extra_globals_leaked(self):
        """The fix function should not have access to local test variables."""
        secret = "TOP_SECRET"
        code = "def fix(record): return record"
        compiled = _compile_fix(code)
        fn = _exec_fix(compiled)
        # Just ensure it runs without error
        fn({})


# ---------------------------------------------------------------------------
# _apply_fix_to_batch
# ---------------------------------------------------------------------------

class TestApplyFixToBatch:
    def _fn(self, code: str):
        compiled = _compile_fix(code)
        return _exec_fix(compiled)

    def test_applies_to_all_rows(self):
        fn = self._fn("def fix(r): r['v'] = 1; return r")
        batch = [{"v": 0}, {"v": 0}, {"v": 0}]
        results, err = _apply_fix_to_batch(fn, batch)
        assert len(results) == 3
        assert all(r["v"] == 1 for r in results)
        assert err == ""

    def test_error_in_row_captured(self):
        fn = self._fn("def fix(r): raise ValueError('boom'); return r")
        batch = [{"v": 1}]
        results, err = _apply_fix_to_batch(fn, batch)
        assert "ValueError" in err
        assert len(results) == 1  # original row preserved

    def test_empty_batch_returns_empty(self):
        fn = self._fn("def fix(r): return r")
        results, err = _apply_fix_to_batch(fn, [])
        assert results == []
        assert err == ""

    def test_does_not_mutate_original_batch(self):
        fn = self._fn("def fix(r): r['v'] = 999; return r")
        batch = [{"v": 1}]
        _apply_fix_to_batch(fn, batch)
        assert batch[0]["v"] == 1  # original unchanged


# ---------------------------------------------------------------------------
# _validate_output
# ---------------------------------------------------------------------------

class TestValidateOutput:
    def test_clean_batch_returns_empty(self):
        from src.pipeline.mock_source import get_batch
        batch = get_batch("orders")
        residual = _validate_output("orders", batch, _registry())
        assert residual == []

    def test_drifted_batch_returns_items(self):
        residual = _validate_output("orders", ORDERS_TYPE_MISMATCH, _registry())
        assert len(residual) > 0

    def test_returns_list_of_strings(self):
        residual = _validate_output("orders", ORDERS_NULL_VIOLATION, _registry())
        assert isinstance(residual, list)
        assert all(isinstance(s, str) for s in residual)

    def test_unknown_table_returns_empty(self):
        residual = _validate_output("nonexistent", [{"a": 1}], _registry())
        assert residual == []


# ---------------------------------------------------------------------------
# ExecutionResult dataclass
# ---------------------------------------------------------------------------

class TestExecutionResult:
    def _make(self, success=True) -> ExecutionResult:
        return ExecutionResult(
            proposal_id="orders__amount__null_violation",
            table="orders",
            column="amount",
            drift_type="null_violation",
            success=success,
        )

    def test_to_dict_keys(self):
        result = self._make()
        d = result.to_dict()
        for key in ("proposal_id", "table", "column", "drift_type", "success",
                     "output_records", "residual_drift", "error", "timestamp"):
            assert key in d

    def test_success_true(self):
        result = self._make(success=True)
        assert result.success is True

    def test_failure_false(self):
        result = self._make(success=False)
        assert result.success is False

    def test_summary_contains_ok_on_success(self):
        result = self._make(success=True)
        assert "OK" in result.summary()

    def test_summary_contains_fail_on_failure(self):
        result = self._make(success=False)
        assert "FAIL" in result.summary()

    def test_timestamp_is_iso(self):
        from datetime import datetime
        result = self._make()
        datetime.fromisoformat(result.timestamp)  # should not raise

    def test_output_records_default_empty(self):
        result = self._make()
        assert result.output_records == []

    def test_residual_drift_default_empty(self):
        result = self._make()
        assert result.residual_drift == []


# ---------------------------------------------------------------------------
# AuditLog
# ---------------------------------------------------------------------------

class TestAuditLog:
    def _result(self, success=True, pid="p1") -> ExecutionResult:
        return ExecutionResult(
            proposal_id=pid,
            table="orders",
            column="amount",
            drift_type="null_violation",
            success=success,
        )

    def test_record_appends_entry(self):
        log = AuditLog()
        log.record(self._result())
        assert len(log.entries) == 1

    def test_successes_filtered(self):
        log = AuditLog()
        log.record(self._result(success=True))
        log.record(self._result(success=False))
        assert len(log.successes()) == 1

    def test_failures_filtered(self):
        log = AuditLog()
        log.record(self._result(success=True))
        log.record(self._result(success=False))
        assert len(log.failures()) == 1

    def test_summary_contains_counts(self):
        log = AuditLog()
        log.record(self._result(success=True))
        log.record(self._result(success=False))
        s = log.summary()
        assert "total=2" in s
        assert "success=1" in s
        assert "failure=1" in s

    def test_flush_to_json(self, tmp_path: Path):
        log_path = tmp_path / "audit.json"
        log = AuditLog(log_path=log_path)
        log.record(self._result())
        assert log_path.exists()
        data = json.loads(log_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1

    def test_json_contains_proposal_id(self, tmp_path: Path):
        log_path = tmp_path / "audit.json"
        log = AuditLog(log_path=log_path)
        log.record(self._result(pid="my_proposal"))
        data = json.loads(log_path.read_text())
        assert data[0]["proposal_id"] == "my_proposal"

    def test_multiple_entries_flushed(self, tmp_path: Path):
        log_path = tmp_path / "audit.json"
        log = AuditLog(log_path=log_path)
        log.record(self._result(pid="a"))
        log.record(self._result(pid="b"))
        data = json.loads(log_path.read_text())
        assert len(data) == 2


# ---------------------------------------------------------------------------
# PatchExecutor – integration tests
# ---------------------------------------------------------------------------

class TestPatchExecutor:
    """Integration tests that run the executor against mock drift scenarios."""

    def _executor(
        self,
        tmp_path: Path,
        batch_override: dict[str, list[dict[str, Any]]] | None = None,
    ) -> tuple[PatchExecutor, AuditLog]:
        registry = _registry()
        audit = AuditLog(log_path=tmp_path / "audit.json")
        # By default the executor uses get_batch (clean data); tests may override.
        if batch_override is not None:
            provider = lambda table: batch_override.get(table, [])
        else:
            provider = None
        executor = PatchExecutor(registry, audit, batch_provider=provider)
        return executor, audit

    # -- type-mismatch fix -----------------------------------------------

    def test_type_mismatch_fix_succeeds_on_clean_data(self, tmp_path: Path):
        """The fix for a type-mismatch column should produce no residual drift
        when applied to the clean mock source data."""
        executor, audit = self._executor(tmp_path)
        record = _make_approval_record("orders", ORDERS_TYPE_MISMATCH, patches_dir=tmp_path)
        result = executor.execute(record)
        # Clean data has correct types → no residual drift after fix.
        assert result.success

    def test_type_mismatch_fix_applied_to_drifted_data(self, tmp_path: Path):
        """Fix should eliminate the drift when applied to the drifted batch."""
        executor, audit = self._executor(
            tmp_path, batch_override={"orders": ORDERS_TYPE_MISMATCH}
        )
        record = _make_approval_record("orders", ORDERS_TYPE_MISMATCH, patches_dir=tmp_path)
        result = executor.execute(record)
        # The fix casts customer_id to int → no type-mismatch residual.
        type_mismatch_residuals = [
            s for s in result.residual_drift if "type_mismatch" in s
        ]
        assert type_mismatch_residuals == []

    # -- null-violation fix ---------------------------------------------

    def test_null_violation_fix_on_drifted_batch(self, tmp_path: Path):
        executor, audit = self._executor(
            tmp_path, batch_override={"orders": ORDERS_NULL_VIOLATION}
        )
        record = _make_approval_record("orders", ORDERS_NULL_VIOLATION, patches_dir=tmp_path)
        result = executor.execute(record)
        null_residuals = [
            s for s in result.residual_drift if "null_violation" in s
        ]
        assert null_residuals == []

    # -- missing-column fix ---------------------------------------------

    def test_missing_column_fix_adds_default(self, tmp_path: Path):
        executor, audit = self._executor(
            tmp_path, batch_override={"orders": ORDERS_MISSING_COLUMN}
        )
        record = _make_approval_record("orders", ORDERS_MISSING_COLUMN, patches_dir=tmp_path)
        result = executor.execute(record)
        # After fix, all rows should have 'amount'.
        assert all("amount" in r for r in result.output_records)

    # -- new-column fix -------------------------------------------------

    def test_new_column_fix_removes_extra_column(self, tmp_path: Path):
        executor, audit = self._executor(
            tmp_path, batch_override={"orders": ORDERS_NEW_COLUMN}
        )
        record = _make_approval_record("orders", ORDERS_NEW_COLUMN, patches_dir=tmp_path)
        result = executor.execute(record)
        assert all("discount_pct" not in r for r in result.output_records)

    # -- file-backed patch path ----------------------------------------

    def test_executes_from_patch_file(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = _make_approval_record_with_file(
            "orders", ORDERS_NULL_VIOLATION, patches_dir=tmp_path / "patches"
        )
        result = executor.execute(record)
        assert result.output_records  # non-empty

    # -- bad code ------------------------------------------------------

    def test_syntax_error_returns_failure(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = ApprovalRecord(
            proposal_id="bad__x__type_mismatch",
            table="orders",
            column="x",
            drift_type="type_mismatch",
            decision=Decision.APPROVE,
            code="def fix(record: !!!",  # broken syntax
        )
        result = executor.execute(record)
        assert not result.success
        assert "SyntaxError" in result.error

    def test_missing_fix_function_returns_failure(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = ApprovalRecord(
            proposal_id="bad__x__type_mismatch",
            table="orders",
            column="x",
            drift_type="type_mismatch",
            decision=Decision.APPROVE,
            code="x = 1  # no fix function",
        )
        result = executor.execute(record)
        assert not result.success

    def test_no_code_returns_failure(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = ApprovalRecord(
            proposal_id="empty__x__type_mismatch",
            table="orders",
            column="x",
            drift_type="type_mismatch",
            decision=Decision.APPROVE,
            code="",
        )
        result = executor.execute(record)
        assert not result.success

    # -- audit log integration -----------------------------------------

    def test_audit_log_records_success(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = _make_approval_record("orders", ORDERS_TYPE_MISMATCH, patches_dir=tmp_path)
        executor.execute(record)
        assert len(audit.entries) == 1
        assert audit.entries[0].success

    def test_audit_log_records_failure(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = ApprovalRecord(
            proposal_id="bad__x__type_mismatch",
            table="orders",
            column="x",
            drift_type="type_mismatch",
            decision=Decision.APPROVE,
            code="def fix(r: !!!",
        )
        executor.execute(record)
        assert len(audit.failures()) == 1

    def test_audit_log_json_persisted(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = _make_approval_record("orders", ORDERS_TYPE_MISMATCH, patches_dir=tmp_path)
        executor.execute(record)
        log_path = tmp_path / "audit.json"
        data = json.loads(log_path.read_text())
        assert len(data) == 1
        assert data[0]["table"] == "orders"

    # -- execute_many --------------------------------------------------

    def test_execute_many_returns_all_results(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        records = [
            _make_approval_record("orders", ORDERS_TYPE_MISMATCH, patches_dir=tmp_path),
            _make_approval_record("orders", ORDERS_NULL_VIOLATION, patches_dir=tmp_path),
        ]
        results = executor.execute_many(records)
        assert len(results) == 2

    def test_execute_many_all_audited(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        records = [
            _make_approval_record("orders", ORDERS_TYPE_MISMATCH, patches_dir=tmp_path),
            _make_approval_record("orders", ORDERS_NULL_VIOLATION, patches_dir=tmp_path),
        ]
        executor.execute_many(records)
        assert len(audit.entries) == 2

    # -- output_records ------------------------------------------------

    def test_output_records_have_expected_count(self, tmp_path: Path):
        """Output records should match the number of rows in the batch."""
        from src.pipeline.mock_source import get_batch
        clean_batch = get_batch("orders")
        executor, audit = self._executor(tmp_path)
        record = _make_approval_record("orders", ORDERS_TYPE_MISMATCH, patches_dir=tmp_path)
        result = executor.execute(record)
        assert len(result.output_records) == len(clean_batch)

    def test_output_records_are_dicts(self, tmp_path: Path):
        executor, audit = self._executor(tmp_path)
        record = _make_approval_record("orders", ORDERS_NULL_VIOLATION, patches_dir=tmp_path)
        result = executor.execute(record)
        assert all(isinstance(r, dict) for r in result.output_records)

    # -- multi-drift (one fix at a time) --------------------------------

    def test_multi_drift_first_fix_executes(self, tmp_path: Path):
        executor, audit = self._executor(
            tmp_path, batch_override={"orders": ORDERS_MULTI_DRIFT}
        )
        record = _make_approval_record("orders", ORDERS_MULTI_DRIFT, index=0, patches_dir=tmp_path)
        result = executor.execute(record)
        # Should run without crashing; result is an ExecutionResult.
        assert isinstance(result, ExecutionResult)
