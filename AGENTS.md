# VPA Agent Guide

## Scope and source of truth

VPA promotes upstream architecture changes to a locally maintained target ISA. It is a
workflow-first promotion tool, not a generic per-file coding-agent harness.

- Read [DESIGN.md](DESIGN.md) before changing architecture or promotion behavior. It is the
  source of truth for workflows, domain models, routing, and milestone scope.
- Use [README.md](README.md) for user-facing setup and usage.
- Keep this file limited to durable repository-wide working instructions. Do not duplicate
  design specifications, schemas, roadmaps, or acceptance criteria here.

## Code map

- `vpa/orchestrator/`: promotion flow, routing, checkpoints, and LLM gate
- `vpa/engines/`: Git, validation, and repair execution
- `vpa/analysis/`: classification, diff analysis, preprocessing, and ISA mapping
- `vpa/ledger/`: result persistence and reporting; never the workflow driver
- `vpa/tests/`: unit and small temporary-repository tests
- `vpa/main.py` and `vpa/config.py`: CLI and configuration

Put new behavior in the owning package. Avoid new top-level modules and avoid reviving the
legacy ledger-driven, per-file agent loop.

## Architectural invariants

- Git performs mechanical repository operations; build and tests determine correctness.
- Classify and analyze changes before dispatching them. Classification alone must not trigger
  an LLM call.
- Use an LLM only for semantic porting, conflict resolution, focused repair, or actionable
  manual notes. Keep its output patch- or decision-oriented.
- `orchestrator/llm_gate.py` is a pure decision layer: no repository mutation, configuration
  loading, or LLM calls.
- Every worktree mutation in an automated path requires an explicit checkpoint and rollback
  strategy.
- Store completed workflow facts in the ledger, not agent state-machine bookkeeping.
- Use structured subprocess results. Pass argument lists unless an explicitly user-provided
  command requires a shell.

The default port maps `src/dynarec/rv64` to `src/dynarec/sw64_core3`. Treat other reference
ISAs as configured fallbacks, not combined first-pass sources. Detailed mapping and execution
rules belong in `DESIGN.md` and code.

## Development rules

- Target Python 3.13 and preserve the typing style of adjacent code.
- Prefer small typed records and focused modules over speculative abstractions.
- Keep changes scoped; update or add focused tests for changed behavior.
- Prefer pure unit tests or temporary Git repositories. Do not require the large Box64 fixture
  for routine tests.
- Do not add mandatory AST, compiler-database, or similar heavyweight analysis dependencies
  without an explicit design change.

## Validation

From the repository root, run the checks relevant to the change. Before handing off a code
change, run the full suite unless the user requests otherwise or the environment prevents it:

```text
uv run ruff check .
uv run pyright
uv run pytest
```

Report any check that was not run or did not pass. Documentation-only changes do not require
the full Python suite unless they alter executable examples or commands.

## Repository hygiene

- Treat `box64-2-sw64/`, `box64_2_sw64.tar.gz`, and `logs/` as local or user-provided data.
  Do not add, delete, or rewrite them unless explicitly requested.
- Do not commit secrets or local configuration such as credentials in `vpa.toml`.
- Preserve unrelated worktree changes and inspect `git status` before finishing.
- Do not create commits, branches, or modify Git history unless the user asks.