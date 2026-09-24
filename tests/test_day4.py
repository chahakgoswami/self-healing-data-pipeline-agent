"""Day 4 tests: human-in-the-loop confirmation layer."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
from src.drafter.fix_drafter import FixDrafter, DraftResult, FixProposal, FixSource
from src.approval.confirmation import (
    ApprovalRecord,
    ApprovalSession,
    Decision,
    _make_diff,
    _write_patch,
    _prompt_for_proposal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registry() -> SchemaRegistry:
    return SchemaRegistry()


def _detect(table: str, batch: list[dict[str, Any]]):
    return SchemaDriftDetector(_registry()).detect(table, batch)


def _draft(table: str, batch: list[dict[str, Any]]) -> DraftResult:
    return FixDrafter(_registry()).draft(_detect(table, batch))


def _first_proposal(table: str, batch: list[dict[str, Any]]) -> FixProposal:
    result = _draft(table, batch)
    assert result.proposals, "Expected at least one proposal"
    return result.proposals[0]


# ---------------------------------------------------------------------------
# _make_diff
# ---------------------------------------------------------------------------

class TestMakeDiff:
    def test_diff_detects_added_line(self):
        diff = _make_diff("", "def fix(r): return r")
        assert "+" in diff

    def test_diff_detects_removed_line(self):
        diff = _make_diff("def fix(r): return r", "")
        assert "-" in diff

    def test_identical_strings_produce_empty_diff(self):
        code = "def fix(r):\n    return r"
        diff = _make_diff(code, code)
        assert diff == ""

    def test_diff_contains_label(self):
        diff = _make_diff("old", "new", label="my_fix")
        assert "my_fix" in diff

    def test_diff_is_string(self):
        diff = _make_diff("a", "b")
        assert isinstance(diff, str)


# ---------------------------------------------------------------------------
# _write_patch
# ---------------------------------------------------------------------------

class TestWritePatch:
    def test_creates_patch_file(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        path = _write_patch(proposal, proposal.code, tmp_path)
        assert path.exists()

    def test_patch_file_contains_code(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        path = _write_patch(proposal, proposal.code, tmp_path)
        content = path.read_text()
        assert "def fix" in content

    def test_patch_file_contains_table_comment(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_NULL_VIOLATION)
        path = _write_patch(proposal, proposal.code, tmp_path)
        content = path.read_text()
        assert "orders" in content

    def test_patch_filename_contains_column(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        path = _write_patch(proposal, proposal.code, tmp_path)
        assert proposal.drift_item.column in path.name

    def test_patch_filename_contains_drift_type(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        path = _write_patch(proposal, proposal.code, tmp_path)
        assert proposal.drift_item.drift_type.value in path.name

    def test_patch_created_in_patches_dir(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_NULL_VIOLATION)
        sub = tmp_path / "versioned_patches"
        path = _write_patch(proposal, proposal.code, sub)
        assert path.parent == sub

    def test_write_creates_dir_if_missing(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_MISSING_COLUMN)
        new_dir = tmp_path / "deep" / "nested"
        _write_patch(proposal, proposal.code, new_dir)
        assert new_dir.exists()

    def test_edited_code_persisted_not_original(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        edited = "def fix(record): record['customer_id'] = 0; return record"
        path = _write_patch(proposal, edited, tmp_path)
        content = path.read_text()
        assert edited in content
        # Original code should NOT overwrite edited
        # (we can't guarantee original is absent – just verify edited is present)


# ---------------------------------------------------------------------------
# ApprovalRecord
# ---------------------------------------------------------------------------

class TestApprovalRecord:
    def _make_record(self, decision: Decision = Decision.APPROVE) -> ApprovalRecord:
        return ApprovalRecord(
            proposal_id="orders__amount__null_violation",
            table="orders",
            column="amount",
            drift_type="null_violation",
            decision=decision,
            code="def fix(r): return r",
        )

    def test_to_dict_keys(self):
        rec = self._make_record()
        d = rec.to_dict()
        assert "proposal_id" in d
        assert "decision" in d
        assert "code" in d
        assert "timestamp" in d

    def test_to_dict_decision_value_is_string(self):
        rec = self._make_record(Decision.REJECT)
        d = rec.to_dict()
        assert d["decision"] == "reject"

    def test_timestamp_is_iso_format(self):
        rec = self._make_record()
        # Should not raise
        from datetime import datetime
        datetime.fromisoformat(rec.timestamp)

    def test_patch_path_none_by_default(self):
        rec = self._make_record()
        assert rec.patch_path is None


# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------

class TestDecision:
    def test_approve_value(self):
        assert Decision.APPROVE.value == "approve"

    def test_reject_value(self):
        assert Decision.REJECT.value == "reject"

    def test_edit_value(self):
        assert Decision.EDIT.value == "edit"


# ---------------------------------------------------------------------------
# _prompt_for_proposal – simulate user inputs
# ---------------------------------------------------------------------------

class TestPromptForProposal:
    """Unit-test the interactive prompt by injecting a fake input function."""

    def test_approve_returns_approve_decision(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        inputs = iter(["a"])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.decision == Decision.APPROVE

    def test_approve_writes_patch_file(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        inputs = iter(["a"])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.patch_path is not None
        assert Path(record.patch_path).exists()

    def test_reject_returns_reject_decision(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_NULL_VIOLATION)
        inputs = iter(["r", ""])  # reject, empty note
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.decision == Decision.REJECT

    def test_reject_no_patch_file(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_NULL_VIOLATION)
        inputs = iter(["r", "no thanks"])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.patch_path is None

    def test_reject_stores_note(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_NULL_VIOLATION)
        inputs = iter(["r", "bad fix"])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.note == "bad fix"

    def test_edit_approve_returns_edit_decision(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        edited_code = "def fix(record):\n    record['customer_id'] = 0\n    return record"
        # Simulate: choose edit, type code lines, end marker, confirm
        input_lines = [
            "e",
            "def fix(record):",
            "    record['customer_id'] = 0",
            "    return record",
            "###END###",
            "y",
        ]
        inputs = iter(input_lines)
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.decision == Decision.EDIT

    def test_edit_approve_writes_edited_code(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        input_lines = [
            "e",
            "def fix(record):",
            "    record['customer_id'] = 0",
            "    return record",
            "###END###",
            "y",
        ]
        inputs = iter(input_lines)
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        content = Path(record.patch_path).read_text()
        assert "customer_id" in content

    def test_edit_not_confirmed_reprompts_then_approve(self, tmp_path: Path):
        """If the user doesn't confirm the edit, they get to choose again."""
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        input_lines = [
            "e",
            "def fix(record): return record",
            "###END###",
            "n",    # don't confirm the edit
            "a",    # then approve the original
        ]
        inputs = iter(input_lines)
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.decision == Decision.APPROVE

    def test_invalid_input_then_approve(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_MISSING_COLUMN)
        inputs = iter(["x", "?", "a"])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.decision == Decision.APPROVE

    def test_record_proposal_id_format(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_NULL_VIOLATION)
        inputs = iter(["r", ""])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        # proposal_id must contain table, column, drift_type
        assert "orders" in record.proposal_id
        assert record.column in record.proposal_id
        assert record.drift_type in record.proposal_id

    def test_full_approve_input_string(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_TYPE_MISMATCH)
        inputs = iter(["approve"])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.decision == Decision.APPROVE

    def test_full_reject_input_string(self, tmp_path: Path):
        proposal = _first_proposal("orders", ORDERS_NULL_VIOLATION)
        inputs = iter(["reject", ""])
        record = _prompt_for_proposal(
            proposal, tmp_path, _input_fn=lambda _="": next(inputs)
        )
        assert record.decision == Decision.REJECT


# ---------------------------------------------------------------------------
# ApprovalSession
# ---------------------------------------------------------------------------

class TestApprovalSession:
    def _session(
        self,
        batch,
        table: str = "orders",
        patches_dir: Path | None = None,
        tmp_path: Path | None = None,
    ) -> ApprovalSession:
        draft = _draft(table, batch)
        pdir = patches_dir or (tmp_path / "patches" if tmp_path else Path("patches"))
        return ApprovalSession(draft_result=draft, patches_dir=pdir)

    def test_run_returns_records(self, tmp_path: Path):
        session = self._session(ORDERS_TYPE_MISMATCH, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["a"] * n)
        records = session.run(_input_fn=lambda _="": next(inputs))
        assert len(records) == n

    def test_all_approved_session(self, tmp_path: Path):
        session = self._session(ORDERS_NULL_VIOLATION, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["a"] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        assert len(session.approved) == n
        assert len(session.rejected) == 0

    def test_all_rejected_session(self, tmp_path: Path):
        session = self._session(ORDERS_NULL_VIOLATION, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        # reject then empty note for each
        inputs = iter(["r", ""] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        assert len(session.rejected) == n
        assert len(session.approved) == 0

    def test_no_proposals_returns_empty(self, tmp_path: Path):
        from src.pipeline.mock_source import get_batch
        draft = _draft("orders", get_batch("orders"))  # no drift
        session = ApprovalSession(draft_result=draft, patches_dir=tmp_path / "p")
        records = session.run(_input_fn=lambda _="": "a")
        assert records == []

    def test_summary_contains_table(self, tmp_path: Path):
        session = self._session(ORDERS_TYPE_MISMATCH, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["a"] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        assert "orders" in session.summary()

    def test_summary_contains_approved_count(self, tmp_path: Path):
        session = self._session(ORDERS_TYPE_MISMATCH, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["a"] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        assert "approved" in session.summary()

    def test_save_session_log_creates_json(self, tmp_path: Path):
        session = self._session(ORDERS_TYPE_MISMATCH, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["a"] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        log_path = session.save_session_log()
        assert log_path.exists()
        data = json.loads(log_path.read_text())
        assert isinstance(data, list)
        assert len(data) == n

    def test_session_log_contains_decision(self, tmp_path: Path):
        session = self._session(ORDERS_NULL_VIOLATION, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["r", ""] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        log_path = session.save_session_log()
        data = json.loads(log_path.read_text())
        assert all(d["decision"] == "reject" for d in data)

    def test_multi_drift_session(self, tmp_path: Path):
        session = self._session(ORDERS_MULTI_DRIFT, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["a"] * n)
        records = session.run(_input_fn=lambda _="": next(inputs))
        assert len(records) == n
        assert all(r.decision == Decision.APPROVE for r in records)

    def test_approved_patches_exist_on_disk(self, tmp_path: Path):
        session = self._session(ORDERS_MISSING_COLUMN, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["a"] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        for rec in session.approved:
            assert rec.patch_path is not None
            assert Path(rec.patch_path).exists()

    def test_rejected_have_no_patch_files(self, tmp_path: Path):
        session = self._session(ORDERS_MISSING_COLUMN, tmp_path=tmp_path)
        n = len(session.draft_result.proposals)
        inputs = iter(["r", ""] * n)
        session.run(_input_fn=lambda _="": next(inputs))
        for rec in session.rejected:
            assert rec.patch_path is None

    def test_edit_decision_counted_as_approved(self, tmp_path: Path):
        session = self._session(ORDERS_TYPE_MISMATCH, tmp_path=tmp_path)
        # For each proposal, do an edit+confirm
        input_lines = []
        for _ in session.draft_result.proposals:
            input_lines += [
                "e",
                "def fix(record): return record",
                "###END###",
                "y",
            ]
        inputs = iter(input_lines)
        session.run(_input_fn=lambda _="": next(inputs))
        assert len(session.approved) == len(session.draft_result.proposals)
