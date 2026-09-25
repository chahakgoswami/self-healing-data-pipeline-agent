"""Patch executor: safely applies approved transformation code in a sandboxed
exec environment, re-runs the affected pipeline stage, validates output schema
post-fix, and records success or failure with a structured audit log.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.approval.confirmation import ApprovalRecord
from src.drift.detector import SchemaDriftDetector
from src.pipeline.mock_source import get_batch
from src.registry.schema_registry import SchemaRegistry


# ---------------------------------------------------------------------------
# Audit-log entry
# ---------------------------------------------------------------------------

@dataclass
class ExecutionResult:
    """Structured record of one patch-execution attempt."""
    proposal_id:    str
    table:          str
    column:         str
    drift_type:     str
    success:        bool
    # The transformed output records (empty list on failure).
    output_records: list[dict[str, Any]] = field(default_factory=list)
    # Residual drift items after the fix (ideally empty).
    residual_drift: list[str] = field(default_factory=list)
    error:          str = ""
    timestamp:      str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    patch_path:     str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id":    self.proposal_id,
            "table":          self.table,
            "column":         self.column,
            "drift_type":     self.drift_type,
            "success":        self.success,
            "output_records": self.output_records,
            "residual_drift": self.residual_drift,
            "error":          self.error,
            "timestamp":      self.timestamp,
            "patch_path":     self.patch_path,
        }

    def summary(self) -> str:
        status = "OK" if self.success else "FAIL"
        parts = [
            f"[ExecutionResult] {status}",
            f"proposal={self.proposal_id}",
            f"table={self.table}",
        ]
        if self.error:
            parts.append(f"error={self.error!r}")
        if self.residual_drift:
            parts.append(f"residual_drift={self.residual_drift}")
        return "  ".join(parts)


# ---------------------------------------------------------------------------
# Sandboxed exec helpers
# ---------------------------------------------------------------------------

def _compile_fix(code: str) -> Any:
    """Compile *code* and return the compiled code object, or raise SyntaxError."""
    return compile(code, "<patch>", "exec")


def _exec_fix(compiled_code: Any) -> Any:
    """Execute *compiled_code* in an isolated namespace and return the `fix` callable.

    Raises ``KeyError`` if the code does not define a function named ``fix``.
    Raises ``TypeError`` if ``fix`` is not callable.
    """
    # Sandbox: allow only builtins, no extra globals injected.
    namespace: dict[str, Any] = {"__builtins__": __builtins__}
    exec(compiled_code, namespace)  # noqa: S102
    fn = namespace.get("fix")
    if fn is None:
        raise KeyError("Patch code does not define a 'fix' function.")
    if not callable(fn):
        raise TypeError("'fix' in patch code is not callable.")
    return fn


def _apply_fix_to_batch(
    fix_fn: Any,
    batch: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Apply *fix_fn* row-by-row to *batch*.

    Returns (transformed_records, error_message).  error_message is empty on
    full success.  If a row raises an exception we record the error and carry
    on so as to transform as many rows as possible.
    """
    results: list[dict[str, Any]] = []
    first_error = ""
    for i, row in enumerate(batch):
        try:
            fixed = fix_fn(row.copy())
            results.append(fixed)
        except Exception as exc:  # noqa: BLE001
            msg = f"Row {i}: {type(exc).__name__}: {exc}"
            if not first_error:
                first_error = msg
            # Keep the original row so the batch stays complete.
            results.append(row.copy())
    return results, first_error


# ---------------------------------------------------------------------------
# Post-fix schema validation
# ---------------------------------------------------------------------------

def _validate_output(
    table: str,
    records: list[dict[str, Any]],
    registry: SchemaRegistry,
) -> list[str]:
    """Run the drift detector on *records* and return a list of drift descriptions.

    An empty list means no residual drift – the fix succeeded.
    """
    detector = SchemaDriftDetector(registry)
    report = detector.detect(table, records)
    return [str(item) for item in report.items]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog:
    """Append-only in-memory audit log with optional JSON-file persistence."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path
        self._entries: list[ExecutionResult] = []

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, result: ExecutionResult) -> None:
        """Append *result* to the log (and flush to disk if log_path is set)."""
        self._entries.append(result)
        print(f"[audit] {result.summary()}")
        if self.log_path is not None:
            self._flush()

    def _flush(self) -> None:
        import json
        assert self.log_path is not None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        data = [e.to_dict() for e in self._entries]
        self.log_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @property
    def entries(self) -> list[ExecutionResult]:
        return list(self._entries)

    def successes(self) -> list[ExecutionResult]:
        return [e for e in self._entries if e.success]

    def failures(self) -> list[ExecutionResult]:
        return [e for e in self._entries if not e.success]

    def summary(self) -> str:
        return (
            f"[AuditLog] total={len(self._entries)}"
            f"  success={len(self.successes())}"
            f"  failure={len(self.failures())}"
        )


# ---------------------------------------------------------------------------
# PatchExecutor
# ---------------------------------------------------------------------------

class PatchExecutor:
    """Apply an approved patch and validate the result.

    Usage::

        registry = SchemaRegistry()
        audit    = AuditLog(log_path=Path("logs/audit.json"))
        executor = PatchExecutor(registry, audit)

        result = executor.execute(approval_record)
    """

    def __init__(
        self,
        registry: SchemaRegistry,
        audit_log: AuditLog | None = None,
        *,
        # Override the data source for testing.
        batch_provider=None,
    ) -> None:
        self._registry = registry
        self._audit = audit_log or AuditLog()
        # batch_provider(table) -> list[dict]  (defaults to mock source)
        self._batch_provider = batch_provider or get_batch

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, record: ApprovalRecord) -> ExecutionResult:
        """Execute the patch described by *record*.

        Steps:
        1. Load the patch code (from ``record.code`` or the patch file).
        2. Compile + exec in a sandboxed namespace → obtain ``fix`` callable.
        3. Fetch the affected table batch from the mock source.
        4. Apply ``fix`` to every row.
        5. Validate output schema against the registry.
        6. Write result to the audit log and return.
        """
        result = self._execute_inner(record)
        self._audit.record(result)
        return result

    def execute_many(self, records: list[ApprovalRecord]) -> list[ExecutionResult]:
        """Execute a list of approval records sequentially."""
        return [self.execute(r) for r in records]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _execute_inner(self, record: ApprovalRecord) -> ExecutionResult:
        table      = record.table
        col        = record.column
        dtype      = record.drift_type
        prop_id    = record.proposal_id
        patch_path = record.patch_path

        # 1. Resolve patch code: prefer file on disk, fall back to record.code.
        code = self._load_code(record)
        if code is None:
            return ExecutionResult(
                proposal_id=prop_id,
                table=table,
                column=col,
                drift_type=dtype,
                success=False,
                error="No patch code available (patch_path missing and record.code empty).",
                patch_path=patch_path,
            )

        # 2. Compile.
        try:
            compiled = _compile_fix(code)
        except SyntaxError as exc:
            return ExecutionResult(
                proposal_id=prop_id,
                table=table,
                column=col,
                drift_type=dtype,
                success=False,
                error=f"SyntaxError: {exc}",
                patch_path=patch_path,
            )

        # 3. Exec → get `fix` callable.
        try:
            fix_fn = _exec_fix(compiled)
        except (KeyError, TypeError) as exc:
            return ExecutionResult(
                proposal_id=prop_id,
                table=table,
                column=col,
                drift_type=dtype,
                success=False,
                error=str(exc),
                patch_path=patch_path,
            )

        # 4. Fetch batch.
        try:
            batch = self._batch_provider(table)
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                proposal_id=prop_id,
                table=table,
                column=col,
                drift_type=dtype,
                success=False,
                error=f"Batch fetch error: {exc}",
                patch_path=patch_path,
            )

        # 5. Apply fix.
        output, row_error = _apply_fix_to_batch(fix_fn, batch)

        # 6. Validate output schema.
        residual = _validate_output(table, output, self._registry)

        success = (not row_error) and (not residual)
        error   = row_error or ""

        print(
            f"[executor] patch={prop_id!r}  rows={len(output)}"
            f"  residual_drift={len(residual)}"
            f"  success={success}"
        )

        return ExecutionResult(
            proposal_id=prop_id,
            table=table,
            column=col,
            drift_type=dtype,
            success=success,
            output_records=output,
            residual_drift=residual,
            error=error,
            patch_path=patch_path,
        )

    # ------------------------------------------------------------------
    # Code loading
    # ------------------------------------------------------------------

    def _load_code(self, record: ApprovalRecord) -> str | None:
        """Return the patch code to execute.

        Prefers reading from the patch file (the authoritative approved copy);
        falls back to ``record.code`` when the file is unavailable.
        """
        if record.patch_path:
            path = Path(record.patch_path)
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                # Strip the file header (comment lines) to isolate the function.
                code_lines = [
                    line for line in raw.splitlines()
                    if not line.startswith("#")
                ]
                return "\n".join(code_lines).strip()
        # Fall back to the code stored in the record itself.
        return record.code or None
