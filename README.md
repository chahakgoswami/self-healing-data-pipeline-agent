# Self-Healing Data Pipeline Agent

Agentic ETL pipeline that detects schema drift, drafts fixes, and self-heals with human approval.

**Domain:** Agentic AI
**Language:** python
**Demonstrates:** You trust agents with production data, carefully.

## 7-day build plan

- [ ] Day 1: Scaffold the project structure with a simulated ETL pipeline that reads mock source schemas and produces mock output records, including a schema registry module that stores expected column names, types, and constraints in a local JSON file.
- [ ] Day 2: Build the schema drift detector that compares incoming mock data batches against the registry, classifies drift types (missing column, type mismatch, new column, null violation), and emits structured drift reports as Python dataclasses.
- [ ] Day 3: Implement the transformation-fix drafter agent that takes a drift report and uses a rule-based engine plus a templated LLM prompt (mocked via a local stub returning canned responses) to propose a Python transformation patch as an executable code string.
- [ ] Day 4: Add a human-in-the-loop confirmation layer that displays the proposed fix diff in the terminal, prompts for approve/reject/edit, and gates any pipeline mutation behind explicit approval before writing changes to a versioned patches directory.
- [ ] Day 5: Integrate the patch executor that safely applies approved transformation code in a sandboxed exec environment, re-runs the affected pipeline stage against the mock data, validates output schema post-fix, and records success or failure with a structured audit log.
- [ ] Day 6: Implement the rollback manager that snapshots pipeline state before each patch application, detects post-fix validation failures, automatically rolls back to the last known-good snapshot, and logs the rollback event with full diff history.
- [ ] Day 7: Add an end-to-end orchestrator agent loop that cycles through multiple mock drift scenarios automatically, integrate pytest tests covering drift detection, fix drafting, approval gating, patch execution, and rollback, and add a CLI entry point with scenario selection flags.

_A comprehensive README with an architecture diagram is generated on Day 7._
