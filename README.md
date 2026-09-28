# Self-Healing Data Pipeline Agent

Agentic ETL pipeline that detects schema drift, drafts fixes, and self-heals with human approval.

**Domain:** Agentic AI  
**Language:** Python 3.10+  
**Demonstrates:** You trust agents with production data, carefully.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      CLI Entry Point (src/cli.py)                   │
│  commands: run | detect | etl | list-scenarios                      │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Orchestrator Agent (src/orchestrator/agent_loop.py)    │
│  - Cycles through mock drift scenarios                              │
│  - Collects RunReport with per-scenario ScenarioResult              │
└──────┬───────────┬───────────────┬───────────────┬─────────────────┘
       │           │               │               │
       ▼           ▼               ▼               ▼
┌──────────┐ ┌──────────┐ ┌─────────────┐ ┌─────────────────────┐
│  Schema  │ │  Drift   │ │    Fix      │ │  Approval Layer     │
│ Registry │ │ Detector │ │  Drafter   │ │  (auto / human)     │
│          │ │          │ │            │ │                     │
│data/     │ │detector  │ │rule_engine │ │confirmation.py      │
│schema_   │ │.py       │ │llm_stub    │ │ApprovalRecord       │
│registry  │ │DriftReport│ │fix_drafter │ │Decision enum        │
│.json     │ │DriftItem │ │FixProposal │ │ApprovalSession      │
└──────────┘ └──────────┘ └─────────────┘ └─────────┬───────────┘
                                                     │
                                                     ▼
                                        ┌────────────────────────┐
                                        │   Patch Executor       │
                                        │   (sandboxed exec)     │
                                        │                        │
                                        │  patch_executor.py     │
                                        │  _compile_fix()        │
                                        │  _exec_fix()           │
                                        │  _apply_fix_to_batch() │
                                        │  _validate_output()    │
                                        │  AuditLog              │
                                        └────────────┬───────────┘
                                                     │
                                                     ▼
                                        ┌────────────────────────┐
                                        │   Rollback Manager     │
                                        │                        │
                                        │  rollback_manager.py   │
                                        │  PipelineSnapshot      │
                                        │  RollbackEvent         │
                                        │  RollbackLog           │
                                        └────────────────────────┘
```

---

## Project layout

```
self-healing-pipeline/
├── data/
│   └── schema_registry.json       # Expected schemas for orders + users
├── src/
│   ├── cli.py                      # CLI entry point
│   ├── pipeline/
│   │   ├── mock_source.py          # Simulated upstream data source
│   │   └── etl.py                  # Extract → Transform → Load stages
│   ├── registry/
│   │   └── schema_registry.py      # Schema load / save / infer
│   ├── drift/
│   │   ├── detector.py             # SchemaDriftDetector + dataclasses
│   │   └── mock_drift_batches.py   # Canned drifted batches for all scenarios
│   ├── drafter/
│   │   ├── fix_drafter.py          # FixDrafter agent (rule → LLM fallback)
│   │   ├── rule_engine.py          # Rule-based fix templates
│   │   ├── prompt_template.py      # LLM prompt renderer
│   │   └── llm_stub.py             # Mocked LLM (canned responses)
│   ├── approval/
│   │   └── confirmation.py         # Human-in-the-loop layer
│   ├── executor/
│   │   └── patch_executor.py       # Sandboxed exec + audit log
│   ├── rollback/
│   │   └── rollback_manager.py     # Snapshot + auto-rollback + rollback log
│   └── orchestrator/
│       └── agent_loop.py           # End-to-end agent orchestrator
├── tests/
│   ├── test_day1.py
│   ├── test_day2.py
│   ├── test_day3.py
│   ├── test_day4.py
│   ├── test_day5.py
│   ├── test_day6.py
│   └── test_day7.py
├── scripts/
│   └── run_pipeline.py
├── pyproject.toml
└── README.md
```

---

## Quick start

```bash
# Install (editable)
pip install -e .[dev]

# Run all tests
pytest

# List available drift scenarios
python -m src.cli list-scenarios

# Detect drift for a scenario
python -m src.cli detect --scenario orders_type_mismatch

# Run the ETL pipeline for a table
python -m src.cli etl --table orders

# Run the full self-healing agent loop (all scenarios)
python -m src.cli run

# Run selected scenarios only
python -m src.cli run --scenarios orders_type_mismatch orders_null_violation

# Write output to a custom directory
python -m src.cli run --output-dir /tmp/pipeline_output
```

---

## 7-day build plan

- [x] Day 1: Scaffold ETL pipeline + schema registry.
- [x] Day 2: Schema drift detector (missing column, type mismatch, new column, null violation).
- [x] Day 3: Transformation-fix drafter agent (rule engine + mocked LLM stub).
- [x] Day 4: Human-in-the-loop confirmation layer (approve / reject / edit).
- [x] Day 5: Patch executor with sandboxed exec, schema validation, and audit log.
- [x] Day 6: Rollback manager with snapshots, auto-rollback, and rollback log.
- [x] Day 7: End-to-end orchestrator agent loop, pytest suite, CLI entry point.

---

## Key design decisions

| Concern | Approach |
|---|---|
| No real LLM calls | `llm_stub.py` pattern-matches the prompt and returns canned code |
| Safety | Every patch runs in a sandboxed `exec` namespace; no filesystem writes without approval |
| Human gating | All pipeline mutations require explicit `approve / reject / edit` confirmation |
| Rollback | State is snapshotted before every patch; failures auto-restore the snapshot |
| Auditability | Every execution and rollback event is recorded to a JSON log |
| Testability | All I/O is injectable (input functions, batch providers, log paths) |
