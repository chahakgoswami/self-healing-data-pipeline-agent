"""Prompt template renderer for the LLM-based fix drafter."""
from __future__ import annotations

from src.drift.detector import DriftItem, DriftReport

# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_TEMPLATE = """\
You are a Python data-engineering assistant.
You are given a drift report for table '{table}' and must write a Python
function called `fix(record: dict) -> dict` that corrects ONE drift item.

Drift item to fix:
  type       : {drift_type}
  column     : {column}
  expected   : {expected}
  actual     : {actual}
  message    : {message}

Registered column type : {registered_type}

Requirements:
- The function must be named exactly `fix`.
- It must accept a single `dict` argument and return a `dict`.
- It must address ONLY the drift item described above.
- Do not import anything; use only built-ins.
- Keep it concise (< 10 lines).

Search token (do not remove): {search_token}

Python code only — no markdown, no explanation:
"""


def render_prompt(
    report: DriftReport,
    item: DriftItem,
    registered_type: str | None = None,
) -> str:
    """Render the LLM prompt for a single *item* from *report*.

    The ``search_token`` field embeds a compact ``DRIFT_TYPE:column`` string
    that the LLM stub uses to select the correct canned response.
    """
    search_token = f"{item.drift_type.value.upper().replace('_', '_')}:{item.column}"
    # Normalise to SCREAMING format expected by the stub patterns.
    search_token = f"{item.drift_type.value.upper()}:{item.column}"

    return _TEMPLATE.format(
        table=report.table,
        drift_type=item.drift_type.value,
        column=item.column,
        expected=item.expected,
        actual=item.actual,
        message=item.message,
        registered_type=registered_type or "unknown",
        search_token=search_token,
    )
