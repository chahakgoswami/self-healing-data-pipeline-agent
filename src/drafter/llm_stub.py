"""Mocked LLM stub that returns canned responses for transformation fix prompts.

In production this would call an actual LLM API.  Here we pattern-match on
keywords in the rendered prompt and return a plausible canned code snippet.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Canned response library
# ---------------------------------------------------------------------------

# Each entry is (pattern_in_prompt, code_snippet).
# The FIRST matching pattern wins.
_CANNED_RESPONSES: list[tuple[str, str]] = [
    (
        "MISSING_COLUMN:amount",
        """\
def fix(record: dict) -> dict:
    """Fill missing 'amount' with a default of 0.0."""
    if 'amount' not in record or record['amount'] is None:
        record['amount'] = 0.0
    return record
""",
    ),
    (
        "MISSING_COLUMN:status",
        """\
def fix(record: dict) -> dict:
    """Fill missing 'status' with default value 'unknown'."""
    if 'status' not in record or record['status'] is None:
        record['status'] = 'unknown'
    return record
""",
    ),
    (
        "MISSING_COLUMN:customer_id",
        """\
def fix(record: dict) -> dict:
    """Fill missing 'customer_id' with sentinel value -1."""
    if 'customer_id' not in record or record['customer_id'] is None:
        record['customer_id'] = -1
    return record
""",
    ),
    (
        "TYPE_MISMATCH:customer_id",
        """\
def fix(record: dict) -> dict:
    """Cast 'customer_id' to int."""
    try:
        record['customer_id'] = int(record['customer_id'])
    except (TypeError, ValueError):
        record['customer_id'] = -1
    return record
""",
    ),
    (
        "TYPE_MISMATCH:amount",
        """\
def fix(record: dict) -> dict:
    """Cast 'amount' to float."""
    try:
        record['amount'] = float(record['amount'])
    except (TypeError, ValueError):
        record['amount'] = 0.0
    return record
""",
    ),
    (
        "TYPE_MISMATCH:age",
        """\
def fix(record: dict) -> dict:
    """Cast 'age' to int."""
    try:
        record['age'] = int(record['age'])
    except (TypeError, ValueError):
        record['age'] = 0
    return record
""",
    ),
    (
        "NULL_VIOLATION:amount",
        """\
def fix(record: dict) -> dict:
    """Replace None 'amount' with 0.0."""
    if record.get('amount') is None:
        record['amount'] = 0.0
    return record
""",
    ),
    (
        "NULL_VIOLATION:email",
        """\
def fix(record: dict) -> dict:
    """Replace None 'email' with a placeholder."""
    if record.get('email') is None:
        record['email'] = 'unknown@example.com'
    return record
""",
    ),
    (
        "NULL_VIOLATION:customer_id",
        """\
def fix(record: dict) -> dict:
    """Replace None 'customer_id' with sentinel -1."""
    if record.get('customer_id') is None:
        record['customer_id'] = -1
    return record
""",
    ),
    (
        "NEW_COLUMN:discount_pct",
        """\
def fix(record: dict) -> dict:
    """Drop unexpected column 'discount_pct'."""
    record.pop('discount_pct', None)
    return record
""",
    ),
    (
        "NEW_COLUMN:phone",
        """\
def fix(record: dict) -> dict:
    """Drop unexpected column 'phone'."""
    record.pop('phone', None)
    return record
""",
    ),
    (
        "NEW_COLUMN:region",
        """\
def fix(record: dict) -> dict:
    """Drop unexpected column 'region'."""
    record.pop('region', None)
    return record
""",
    ),
]

# Generic fallback used when no specific pattern matches.
_FALLBACK_CODE = """\
def fix(record: dict) -> dict:
    """Generic pass-through fix (no specific rule matched)."""
    return record
"""


def call_llm(prompt: str) -> str:
    """Simulate an LLM call: inspect *prompt* and return a canned code string.

    The stub scans for substrings like ``MISSING_COLUMN:amount`` that the
    :mod:`drafter.fix_drafter` module embeds in the prompt template.
    """
    for pattern, code in _CANNED_RESPONSES:
        if pattern in prompt:
            return code.strip()
    return _FALLBACK_CODE.strip()
