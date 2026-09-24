"""Human-in-the-loop confirmation layer.

Displays the proposed fix diff in the terminal, prompts for approve/reject/edit,
and gates any pipeline mutation behind explicit approval before writing changes
to a versioned patches directory.
"""
from __future__ import annotations

import difflib
import json
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.drafter.fix_drafter import DraftResult, FixProposal


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PATCHES_DIR = Path("patches")


# ---------------------------------------------------------------------------
# Decision type
# ---------------------------------------------------------------------------

class Decision(str, Enum):
    APPROVE = "approve"
    REJECT  = "reject"
    EDIT    = "edit"


# ---------------------------------------------------------------------------
# ApprovalRecord
# ---------------------------------------------------------------------------

@dataclass
class ApprovalRecord:
    """A persisted record of one human decision on one FixProposal."""
    proposal_id:  str          # e.g. "orders__amount__null_violation"
    table:        str
    column:       str
    drift_type:   str
    decision:     Decision
    code:         str          # the (possibly edited) code that was approved
    timestamp:    str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    patch_path:   str | None = None   # where the patch file was written
    note:         str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "table":       self.table,
            "column":      self.column,
            "drift_type":  self.drift_type,
            "decision":    self.decision.value,
            "code":        self.code,
            "timestamp":   self.timestamp,
            "patch_path":  self.patch_path,
            "note":        self.note,
        }


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def _make_diff(old_code: str, new_code: str, label: str = "fix") -> str:
    """Return a unified-diff string between *old_code* and *new_code*."""
    old_lines = old_code.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"{label}_original",
        tofile=f"{label}_proposed",
        lineterm="",
    )
    return "\n".join(diff_lines)


# ---------------------------------------------------------------------------
# Patch file writer
# ---------------------------------------------------------------------------

def _write_patch(
    proposal: FixProposal,
    approved_code: str,
    patches_dir: Path,
) -> Path:
    """Write the approved patch to a versioned file and return its path."""
    patches_dir.mkdir(parents=True, exist_ok=True)

    table      = proposal.metadata.get("table", "unknown")
    col        = proposal.drift_item.column
    dtype      = proposal.drift_item.drift_type.value
    ts         = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    filename   = f"{ts}__{table}__{col}__{dtype}.py"
    patch_path = patches_dir / filename

    # Build a self-documenting patch file.
    header = textwrap.dedent(f"""\
        # Auto-generated patch – approved by human
        # table      : {table}
        # column     : {col}
        # drift_type : {dtype}
        # timestamp  : {ts}
        # source     : {proposal.source.value}
        # -------------------------------------------------------
    """)
    patch_path.write_text(header + approved_code + "\n", encoding="utf-8")
    print(f"[approval] Patch written to: {patch_path}")
    return patch_path


# ---------------------------------------------------------------------------
# Interactive prompt (one proposal)
# ---------------------------------------------------------------------------

def _prompt_for_proposal(
    proposal: FixProposal,
    patches_dir: Path,
    *,
    _input_fn=input,   # injectable for tests
) -> ApprovalRecord:
    """Display diff and prompt the human for a decision on a single proposal."""
    table     = proposal.metadata.get("table", "?")
    col       = proposal.drift_item.column
    dtype     = proposal.drift_item.drift_type.value
    source    = proposal.source.value
    prop_id   = f"{table}__{col}__{dtype}"

    # ---- display ---------------------------------------------------------
    print()
    print("=" * 70)
    print(f"  FIX PROPOSAL  |  table={table!r}  col={col!r}  drift={dtype}")
    print(f"  source: {source}")
    print("=" * 70)

    # Show diff relative to an empty baseline so the user sees the new code.
    diff = _make_diff("", proposal.code, label=f"{table}.{col}")
    if diff:
        print("--- Proposed patch diff ---")
        print(diff)
    else:
        print("(No diff – code is empty)")

    print()
    print("Full proposed code:")
    print("-" * 40)
    print(proposal.code)
    print("-" * 40)

    # ---- decision loop ---------------------------------------------------
    while True:
        raw = _input_fn(
            "\n[A]pprove / [R]eject / [E]dit ? "
        ).strip().lower()

        if raw in ("a", "approve"):
            patch_path = _write_patch(proposal, proposal.code, patches_dir)
            record = ApprovalRecord(
                proposal_id=prop_id,
                table=table,
                column=col,
                drift_type=dtype,
                decision=Decision.APPROVE,
                code=proposal.code,
                patch_path=str(patch_path),
            )
            print(f"[approval] Approved: {prop_id}")
            return record

        elif raw in ("r", "reject"):
            note = _input_fn("Optional rejection note (press Enter to skip): ").strip()
            record = ApprovalRecord(
                proposal_id=prop_id,
                table=table,
                column=col,
                drift_type=dtype,
                decision=Decision.REJECT,
                code=proposal.code,
                note=note,
            )
            print(f"[approval] Rejected: {prop_id}")
            return record

        elif raw in ("e", "edit"):
            print("Enter your edited code below.")
            print("Type '###END###' on its own line when done.")
            lines: list[str] = []
            while True:
                line = _input_fn("")
                if line.strip() == "###END###":
                    break
                lines.append(line)
            edited_code = "\n".join(lines).strip()

            if not edited_code:
                print("[approval] Empty code – edit cancelled, please try again.")
                continue

            # Show diff between original and edited.
            edit_diff = _make_diff(proposal.code, edited_code, label=f"{table}.{col}.edited")
            print("\n--- Diff (original → edited) ---")
            print(edit_diff if edit_diff else "(no change)")

            confirm = _input_fn("\nApprove edited code? [y/N] ").strip().lower()
            if confirm in ("y", "yes"):
                patch_path = _write_patch(proposal, edited_code, patches_dir)
                record = ApprovalRecord(
                    proposal_id=prop_id,
                    table=table,
                    column=col,
                    drift_type=dtype,
                    decision=Decision.EDIT,
                    code=edited_code,
                    patch_path=str(patch_path),
                )
                print(f"[approval] Approved (edited): {prop_id}")
                return record
            else:
                print("[approval] Edit not confirmed – please choose again.")
        else:
            print("  Please enter 'a', 'r', or 'e'.")


# ---------------------------------------------------------------------------
# Session (many proposals)
# ---------------------------------------------------------------------------

@dataclass
class ApprovalSession:
    """Manages the human-review loop for an entire :class:`DraftResult`."""
    draft_result: DraftResult
    patches_dir:  Path = field(default_factory=lambda: DEFAULT_PATCHES_DIR)
    records:      list[ApprovalRecord] = field(default_factory=list)

    # ---- summary helpers ------------------------------------------------

    @property
    def approved(self) -> list[ApprovalRecord]:
        return [r for r in self.records if r.decision in (Decision.APPROVE, Decision.EDIT)]

    @property
    def rejected(self) -> list[ApprovalRecord]:
        return [r for r in self.records if r.decision == Decision.REJECT]

    def summary(self) -> str:
        lines = [
            f"[ApprovalSession] table={self.draft_result.report.table!r}",
            f"  total    : {len(self.records)}",
            f"  approved : {len(self.approved)}",
            f"  rejected : {len(self.rejected)}",
        ]
        for rec in self.records:
            lines.append(
                f"    {rec.proposal_id}  →  {rec.decision.value}"
                + (f"  patch={rec.patch_path}" if rec.patch_path else "")
            )
        return "\n".join(lines)

    # ---- session log ----------------------------------------------------

    def save_session_log(self) -> Path:
        """Write a JSON session log to the patches directory."""
        self.patches_dir.mkdir(parents=True, exist_ok=True)
        ts       = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        table    = self.draft_result.report.table
        log_path = self.patches_dir / f"{ts}__session__{table}.json"
        data     = [r.to_dict() for r in self.records]
        log_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"[approval] Session log written to: {log_path}")
        return log_path

    # ---- main entry-point -----------------------------------------------

    def run(
        self,
        *,
        _input_fn=input,   # injectable for tests
    ) -> list[ApprovalRecord]:
        """Run the interactive review loop for every proposal in the draft result.

        Returns the list of :class:`ApprovalRecord` objects (one per proposal).
        """
        proposals = self.draft_result.proposals
        if not proposals:
            print("[approval] No proposals to review.")
            return []

        print()
        print("#" * 70)
        print(f"  HUMAN REVIEW SESSION  –  {len(proposals)} proposal(s) for table "
              f"{self.draft_result.report.table!r}")
        print("#" * 70)

        for i, proposal in enumerate(proposals, start=1):
            print(f"\n--- Proposal {i}/{len(proposals)} ---")
            record = _prompt_for_proposal(
                proposal,
                self.patches_dir,
                _input_fn=_input_fn,
            )
            self.records.append(record)

        print()
        print(self.summary())
        return self.records
