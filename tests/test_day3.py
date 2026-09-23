"""Day 3 tests: transformation-fix drafter agent."""
from __future__ import annotations

import textwrap
from typing import Any

import pytest

from src.registry.schema_registry import SchemaRegistry
from src.drift.detector import DriftReport, DriftItem, DriftType, SchemaDriftDetector
from src.drift.mock_drift_batches import (
    ORDERS_MISSING_COLUMN,
    ORDERS_TYPE_MISMATCH,
    ORDERS_NEW_COLUMN,
    ORDERS_NULL_VIOLATION,
    ORDERS_MULTI_DRIFT,
    USERS_NULL_VIOLATION,
    USERS_NEW_COLUMN,
)
from src.drafter.fix_drafter import FixDrafter, FixProposal, DraftResult, FixSource
from src.drafter.rule_engine import apply_rules
from src.drafter.prompt_template import render_prompt
from src.drafter.llm_stub import call_llm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _registry() -> SchemaRegistry:
    return SchemaRegistry()  # real registry from data/schema_registry.json


def _detector() -> SchemaDriftDetector:
    return SchemaDriftDetector(_registry())


def _drafter() -> FixDrafter:
    return FixDrafter(_registry())


def _detect(table: str, batch: list[dict[str, Any]]) -> DriftReport:
    return _detector().detect(table, batch)


def _is_valid_fix_fn(code: str) -> bool:
    """Return True if *code* defines a callable `fix` accepting one dict."""
    ns: dict[str, Any] = {}
    try:
        exec(compile(code, "<fix>", "exec"), ns)  # noqa: S102
    except SyntaxError:
        return False
    fn = ns.get("fix")
    return callable(fn)


def _apply_fix(code: str, record: dict[str, Any]) -> dict[str, Any]:
    """Execute fix code and apply it to *record*."""
    ns: dict[str, Any] = {}
    exec(compile(code, "<fix>", "exec"), ns)  # noqa: S102
    return ns["fix"](record.copy())


# ---------------------------------------------------------------------------
# FixProposal & DraftResult dataclasses
# ---------------------------------------------------------------------------

class TestDataclasses:
    def _make_item(self, dtype=DriftType.MISSING_COLUMN, col="amount"):
        return DriftItem(drift_type=dtype, column=col, expected="float")

    def _make_proposal(self, code="def fix(r): return r"):
        item = self._make_item()
        return FixProposal(
            drift_item=item,
            code=code,
            source=FixSource.RULE_ENGINE,
        )

    def test_fix_proposal_fields(self):
        p = self._make_proposal()
        assert p.source == FixSource.RULE_ENGINE
        assert p.prompt is None
        assert isinstance(p.metadata, dict)

    def test_fix_proposal_summary_contains_drift_type(self):
        p = self._make_proposal()
        assert "missing_column" in p.summary()

    def test_fix_proposal_summary_contains_column(self):
        p = self._make_proposal()
        assert "amount" in p.summary()

    def test_fix_proposal_render_contains_code(self):
        p = self._make_proposal("def fix(r): return r")
        rendered = p.render()
        assert "def fix" in rendered

    def test_draft_result_has_proposals_false_when_empty(self):
        report = DriftReport(table="t", n_rows=0)
        result = DraftResult(report=report)
        assert not result.has_proposals

    def test_draft_result_has_proposals_true(self):
        report = DriftReport(table="t", n_rows=1)
        result = DraftResult(report=report)
        result.proposals.append(self._make_proposal())
        assert result.has_proposals

    def test_draft_result_summary_mentions_table(self):
        report = DriftReport(table="orders", n_rows=2)
        result = DraftResult(report=report)
        assert "orders" in result.summary()

    def test_fix_source_enum_values(self):
        assert FixSource.RULE_ENGINE.value == "rule_engine"
        assert FixSource.LLM_STUB.value == "llm_stub"


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------

class TestRuleEngine:
    def _item(self, dtype, col, expected=None, actual=None):
        return DriftItem(
            drift_type=dtype,
            column=col,
            expected=expected,
            actual=actual,
        )

    def test_type_mismatch_int_returns_code(self):
        item = self._item(DriftType.TYPE_MISMATCH, "customer_id")
        code = apply_rules(item, registered_type="int")
        assert code is not None
        assert "int" in code

    def test_type_mismatch_float_returns_code(self):
        item = self._item(DriftType.TYPE_MISMATCH, "amount")
        code = apply_rules(item, registered_type="float")
        assert code is not None
        assert "float" in code

    def test_null_violation_str_returns_code(self):
        item = self._item(DriftType.NULL_VIOLATION, "email")
        code = apply_rules(item, registered_type="str")
        assert code is not None

    def test_null_violation_float_returns_code(self):
        item = self._item(DriftType.NULL_VIOLATION, "amount")
        code = apply_rules(item, registered_type="float")
        assert code is not None
        assert "0.0" in code

    def test_missing_column_int_returns_code(self):
        item = self._item(DriftType.MISSING_COLUMN, "customer_id")
        code = apply_rules(item, registered_type="int")
        assert code is not None
        assert "-1" in code

    def test_missing_column_str_returns_code(self):
        item = self._item(DriftType.MISSING_COLUMN, "status")
        code = apply_rules(item, registered_type="str")
        assert code is not None

    def test_new_column_returns_code(self):
        item = self._item(DriftType.NEW_COLUMN, "discount_pct")
        code = apply_rules(item, registered_type=None)
        assert code is not None
        assert "pop" in code or "del" in code or "discount_pct" in code

    def test_unknown_type_returns_none_for_type_mismatch(self):
        item = self._item(DriftType.TYPE_MISMATCH, "weirdcol")
        code = apply_rules(item, registered_type="json")
        # 'json' is not a known primitive type → no rule should match
        assert code is None

    def test_produced_code_is_valid_python(self):
        item = self._item(DriftType.TYPE_MISMATCH, "customer_id")
        code = apply_rules(item, registered_type="int")
        assert _is_valid_fix_fn(code)

    def test_rule_engine_code_is_executable_type_mismatch(self):
        item = self._item(DriftType.TYPE_MISMATCH, "customer_id")
        code = apply_rules(item, registered_type="int")
        record = {"customer_id": "123"}
        result = _apply_fix(code, record)
        assert result["customer_id"] == 123

    def test_rule_engine_code_is_executable_null_violation(self):
        item = self._item(DriftType.NULL_VIOLATION, "amount")
        code = apply_rules(item, registered_type="float")
        record = {"amount": None}
        result = _apply_fix(code, record)
        assert result["amount"] == 0.0

    def test_rule_engine_code_is_executable_missing_column(self):
        item = self._item(DriftType.MISSING_COLUMN, "status")
        code = apply_rules(item, registered_type="str")
        record = {"order_id": "X"}
        result = _apply_fix(code, record)
        assert "status" in result

    def test_rule_engine_code_is_executable_new_column(self):
        item = self._item(DriftType.NEW_COLUMN, "discount_pct")
        code = apply_rules(item, registered_type=None)
        record = {"order_id": "X", "discount_pct": 0.1}
        result = _apply_fix(code, record)
        assert "discount_pct" not in result


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

class TestPromptTemplate:
    def _report_and_item(self):
        report = DriftReport(table="orders", n_rows=2)
        item = DriftItem(
            drift_type=DriftType.TYPE_MISMATCH,
            column="customer_id",
            expected="int",
            actual="str",
            message="bad type",
        )
        report.items.append(item)
        return report, item

    def test_prompt_contains_table(self):
        report, item = self._report_and_item()
        prompt = render_prompt(report, item, registered_type="int")
        assert "orders" in prompt

    def test_prompt_contains_drift_type(self):
        report, item = self._report_and_item()
        prompt = render_prompt(report, item)
        assert "type_mismatch" in prompt

    def test_prompt_contains_column(self):
        report, item = self._report_and_item()
        prompt = render_prompt(report, item)
        assert "customer_id" in prompt

    def test_prompt_contains_search_token(self):
        report, item = self._report_and_item()
        prompt = render_prompt(report, item)
        assert "TYPE_MISMATCH:customer_id" in prompt

    def test_prompt_contains_registered_type(self):
        report, item = self._report_and_item()
        prompt = render_prompt(report, item, registered_type="int")
        assert "int" in prompt

    def test_prompt_is_nonempty_string(self):
        report, item = self._report_and_item()
        prompt = render_prompt(report, item)
        assert isinstance(prompt, str) and len(prompt) > 50


# ---------------------------------------------------------------------------
# LLM stub
# ---------------------------------------------------------------------------

class TestLLMStub:
    def test_returns_string(self):
        result = call_llm("some prompt")
        assert isinstance(result, str)

    def test_type_mismatch_customer_id_returns_int_cast(self):
        prompt = "TYPE_MISMATCH:customer_id"
        code = call_llm(prompt)
        assert "int" in code
        assert _is_valid_fix_fn(code)

    def test_null_violation_amount_returns_default(self):
        prompt = "NULL_VIOLATION:amount"
        code = call_llm(prompt)
        assert _is_valid_fix_fn(code)

    def test_new_column_discount_pct_drops_column(self):
        prompt = "NEW_COLUMN:discount_pct"
        code = call_llm(prompt)
        assert _is_valid_fix_fn(code)
        result = _apply_fix(code, {"order_id": "X", "discount_pct": 0.1})
        assert "discount_pct" not in result

    def test_missing_column_amount_adds_default(self):
        prompt = "MISSING_COLUMN:amount"
        code = call_llm(prompt)
        assert _is_valid_fix_fn(code)

    def test_unknown_prompt_returns_passthrough(self):
        code = call_llm("completely unrelated prompt xyz123")
        assert _is_valid_fix_fn(code)
        record = {"a": 1}
        result = _apply_fix(code, record)
        assert result == {"a": 1}

    def test_null_violation_email_returns_placeholder(self):
        prompt = "NULL_VIOLATION:email"
        code = call_llm(prompt)
        assert _is_valid_fix_fn(code)
        result = _apply_fix(code, {"email": None})
        assert result["email"] is not None


# ---------------------------------------------------------------------------
# FixDrafter – integration
# ---------------------------------------------------------------------------

class TestFixDrafter:
    def test_draft_returns_draft_result(self):
        report = _detect("orders", ORDERS_MISSING_COLUMN)
        result = _drafter().draft(report)
        assert isinstance(result, DraftResult)

    def test_one_proposal_per_drift_item(self):
        report = _detect("orders", ORDERS_TYPE_MISMATCH)
        result = _drafter().draft(report)
        assert len(result.proposals) == len(report.items)

    def test_has_proposals_true_when_drift(self):
        report = _detect("orders", ORDERS_NULL_VIOLATION)
        result = _drafter().draft(report)
        assert result.has_proposals

    def test_has_proposals_false_when_no_drift(self):
        from src.pipeline.mock_source import get_batch
        report = _detect("orders", get_batch("orders"))
        result = _drafter().draft(report)
        assert not result.has_proposals

    def test_missing_column_proposal_code_valid(self):
        report = _detect("orders", ORDERS_MISSING_COLUMN)
        result = _drafter().draft(report)
        for p in result.proposals:
            assert _is_valid_fix_fn(p.code), f"Invalid code for {p.drift_item.column}"

    def test_type_mismatch_proposal_code_valid(self):
        report = _detect("orders", ORDERS_TYPE_MISMATCH)
        result = _drafter().draft(report)
        for p in result.proposals:
            assert _is_valid_fix_fn(p.code)

    def test_new_column_proposal_code_valid(self):
        report = _detect("orders", ORDERS_NEW_COLUMN)
        result = _drafter().draft(report)
        for p in result.proposals:
            assert _is_valid_fix_fn(p.code)

    def test_null_violation_proposal_code_valid(self):
        report = _detect("orders", ORDERS_NULL_VIOLATION)
        result = _drafter().draft(report)
        for p in result.proposals:
            assert _is_valid_fix_fn(p.code)

    def test_missing_column_uses_rule_engine(self):
        """MISSING_COLUMN on a known type column should come from rule engine."""
        report = _detect("orders", ORDERS_MISSING_COLUMN)
        result = _drafter().draft(report)
        mc_proposals = [
            p for p in result.proposals
            if p.drift_item.drift_type == DriftType.MISSING_COLUMN
        ]
        assert mc_proposals, "Expected at least one MISSING_COLUMN proposal"
        for p in mc_proposals:
            assert p.source == FixSource.RULE_ENGINE

    def test_type_mismatch_uses_rule_engine(self):
        report = _detect("orders", ORDERS_TYPE_MISMATCH)
        result = _drafter().draft(report)
        tm_proposals = [
            p for p in result.proposals
            if p.drift_item.drift_type == DriftType.TYPE_MISMATCH
        ]
        assert tm_proposals
        for p in tm_proposals:
            assert p.source == FixSource.RULE_ENGINE

    def test_new_column_uses_rule_engine(self):
        report = _detect("orders", ORDERS_NEW_COLUMN)
        result = _drafter().draft(report)
        nc_proposals = [
            p for p in result.proposals
            if p.drift_item.drift_type == DriftType.NEW_COLUMN
        ]
        assert nc_proposals
        for p in nc_proposals:
            assert p.source == FixSource.RULE_ENGINE

    def test_multi_drift_one_proposal_per_item(self):
        report = _detect("orders", ORDERS_MULTI_DRIFT)
        result = _drafter().draft(report)
        assert len(result.proposals) == len(report.items)
        for p in result.proposals:
            assert _is_valid_fix_fn(p.code)

    def test_proposal_metadata_contains_table(self):
        report = _detect("orders", ORDERS_TYPE_MISMATCH)
        result = _drafter().draft(report)
        for p in result.proposals:
            assert p.metadata.get("table") == "orders"

    def test_proposal_metadata_contains_registered_type(self):
        report = _detect("orders", ORDERS_TYPE_MISMATCH)
        result = _drafter().draft(report)
        for p in result.proposals:
            assert "registered_type" in p.metadata

    def test_type_mismatch_fix_actually_casts_value(self):
        """End-to-end: proposed fix corrects a type-mismatched record."""
        report = _detect("orders", ORDERS_TYPE_MISMATCH)
        result = _drafter().draft(report)
        tm_proposal = next(
            p for p in result.proposals
            if p.drift_item.drift_type == DriftType.TYPE_MISMATCH
        )
        bad_record = {"customer_id": "two-oh-one", "amount": 9.99}
        fixed = _apply_fix(tm_proposal.code, bad_record)
        assert isinstance(fixed["customer_id"], int)

    def test_null_violation_fix_replaces_none(self):
        report = _detect("orders", ORDERS_NULL_VIOLATION)
        result = _drafter().draft(report)
        nv_proposal = next(
            p for p in result.proposals
            if p.drift_item.drift_type == DriftType.NULL_VIOLATION
        )
        record = {"amount": None}
        fixed = _apply_fix(nv_proposal.code, record)
        assert fixed["amount"] is not None

    def test_new_column_fix_removes_extra_column(self):
        report = _detect("orders", ORDERS_NEW_COLUMN)
        result = _drafter().draft(report)
        nc_proposal = next(
            p for p in result.proposals
            if p.drift_item.drift_type == DriftType.NEW_COLUMN
        )
        record = {"order_id": "X", "discount_pct": 0.1}
        fixed = _apply_fix(nc_proposal.code, record)
        assert "discount_pct" not in fixed

    def test_users_null_violation_proposal(self):
        report = _detect("users", USERS_NULL_VIOLATION)
        result = _drafter().draft(report)
        assert result.has_proposals
        for p in result.proposals:
            assert _is_valid_fix_fn(p.code)

    def test_users_new_column_proposal(self):
        report = _detect("users", USERS_NEW_COLUMN)
        result = _drafter().draft(report)
        assert result.has_proposals
        nc = [
            p for p in result.proposals
            if p.drift_item.drift_type == DriftType.NEW_COLUMN
        ]
        assert nc
        for p in nc:
            assert _is_valid_fix_fn(p.code)

    def test_draft_result_summary_mentions_proposals(self):
        report = _detect("orders", ORDERS_TYPE_MISMATCH)
        result = _drafter().draft(report)
        assert "proposals" in result.summary()
