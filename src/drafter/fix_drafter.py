"""Transformation-fix drafter agent.

For each :class:`~src.drift.detector.DriftItem` in a
:class:`~src.drift.detector.DriftReport` the drafter:

1. Tries the **rule-based engine** (:mod:`~src.drafter.rule_engine`) first.
2. Falls back to the **mocked LLM stub** (:mod:`~src.drafter.llm_stub`) when
   no built-in rule matches.

The result is a :class:`FixProposal` dataclass containing the proposed Python
code string together with provenance metadata.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.drift.detector import DriftItem, DriftReport
from src.registry.schema_registry import SchemaRegistry
from src.drafter.rule_engine import apply_rules
from src.drafter.prompt_template import render_prompt
from src.drafter.llm_stub import call_llm


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class FixSource(str, Enum):  # how the fix code was produced
    RULE_ENGINE = "rule_engine"
    LLM_STUB    = "llm_stub"


@dataclass
class FixProposal:
    """A proposed transformation patch for one drift item."""
    # The drift item this proposal addresses.
    drift_item:      DriftItem
    # The Python code string (a `def fix(record): ...` function).
    code:            str
    # Where the code came from.
    source:          FixSource
    # The rendered LLM prompt (None when source is RULE_ENGINE).
    prompt:          str | None = None
    # Extra metadata (table name, column type, …).
    metadata:        dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Human-readable one-liner."""
        return (
            f"[FixProposal] drift={self.drift_item.drift_type.value}"
            f"  col={self.drift_item.column!r}"
            f"  source={self.source.value}"
        )

    def render(self) -> str:
        """Full render including metadata and code block."""
        lines = [
            "=" * 60,
            self.summary(),
            f"  table   : {self.metadata.get('table', '?')}",
            f"  col_type: {self.metadata.get('registered_type', '?')}",
            "-" * 60,
            "Proposed fix code:",
            self.code,
            "=" * 60,
        ]
        return "\n".join(lines)


@dataclass
class DraftResult:
    """Aggregated drafting result for an entire :class:`DriftReport`."""
    report:    DriftReport
    proposals: list[FixProposal] = field(default_factory=list)

    @property
    def has_proposals(self) -> bool:
        return len(self.proposals) > 0

    def summary(self) -> str:
        lines = [
            f"[DraftResult] table={self.report.table!r}"
            f"  drift_items={len(self.report.items)}"
            f"  proposals={len(self.proposals)}",
        ]
        for p in self.proposals:
            lines.append(f"  {p.summary()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Drafter agent
# ---------------------------------------------------------------------------

class FixDrafter:
    """Draft transformation fixes for all items in a :class:`DriftReport`.

    Usage::

        registry = SchemaRegistry()
        drafter  = FixDrafter(registry)
        result   = drafter.draft(report)
        for proposal in result.proposals:
            print(proposal.render())
    """

    def __init__(self, registry: SchemaRegistry) -> None:
        self._registry = registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def draft(self, report: DriftReport) -> DraftResult:
        """Produce fix proposals for every item in *report*.

        Each :class:`DriftItem` gets exactly one :class:`FixProposal`.
        """
        result = DraftResult(report=report)
        columns = self._registry.get_columns(report.table)

        for item in report.items:
            registered_type: str | None = None
            col_schema = columns.get(item.column)
            if col_schema:
                registered_type = col_schema.get("type")

            proposal = self._draft_one(report, item, registered_type)
            result.proposals.append(proposal)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _draft_one(
        self,
        report: DriftReport,
        item: DriftItem,
        registered_type: str | None,
    ) -> FixProposal:
        """Draft a single fix proposal for *item*."""
        metadata = {
            "table":           report.table,
            "registered_type": registered_type,
        }

        # 1. Try rule-based engine.
        rule_code = apply_rules(item, registered_type)
        if rule_code is not None:
            return FixProposal(
                drift_item=item,
                code=rule_code,
                source=FixSource.RULE_ENGINE,
                prompt=None,
                metadata=metadata,
            )

        # 2. Fall back to mocked LLM stub.
        prompt = render_prompt(report, item, registered_type)
        llm_code = call_llm(prompt)
        return FixProposal(
            drift_item=item,
            code=llm_code,
            source=FixSource.LLM_STUB,
            prompt=prompt,
            metadata=metadata,
        )
