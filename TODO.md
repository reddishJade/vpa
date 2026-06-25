# VPA TODO

Implementation checklist matching [DESIGN.md](DESIGN.md) and [PHASES.md](PHASES.md).
Phases are sequential. Each phase depends on the previous.

## Document References

All design decisions and motivation are in `DESIGN.md`. Detailed per-step code
changes are in `PHASES.md`. This file tracks what is done and what remains.

## Phase 1: Core Infrastructure Cleanup + Agent Loop — ✅ Complete

**Goal**: remove the broken `_fallback_to_ours`, `manual_item`, `confidence`/`threshold`
machinery; replace with the agent loop (function calling) and five tools; update
ledger format to three-axis state.

### P1.1: Delete `_fallback_to_ours` (`engines/repair.py`)

- [x] Remove the `_fallback_to_ours()` function
- [x] Remove its call in `resolve_merge_conflicts()`

### P1.2: Delete all confidence/threshold/manual_item/MANUAL/NEEDS_MANUAL_REVIEW

Completed across all files:

| File | Changes |
|---|---|
| `models.py` | Removed `GatePolicy.manual_confidence_threshold`, `LedgerRecord.manual_item`, `SemanticPortResult.manual_item`, `ExecutedCommit.manual_item`, `ExecutedMerge.manual_item`, `GateDecisionKind.NEEDS_MANUAL_REVIEW`, `PromotionMethod.MANUAL`, `PromotionMethod.SEMANTIC_PORT_PENDING`, `GateDecision.confidence` |
| `llm_gate.py` | Removed `NEEDS_MANUAL_REVIEW` return path and confidence threshold comparison |
| `config.py` | Removed `manual_confidence_threshold` from `VPASettings` and `settings_from_dict` |
| `main.py` | Removed `--manual-confidence-threshold` CLI arg and policy construction |
| `promotion.py` | Removed all `manual_item` field references, `NEEDS_MANUAL_REVIEW` gate handler, `SEMANTIC_PORT_PENDING` usage |
| `repair.py` | Removed `threshold` param, `confidence`/`confidences` fields, `manual_item` references |

### P1.3: Add `AgentLoopResult` + `FailureCode` (`models.py`)

- [x] Define `FailureCode(StrEnum)` with `MAX_RETRIES`, `INTEGRITY_FAIL`, `LLM_ERROR`, `NO_LLM_CONFIGURED`
- [x] Define `AgentLoopResult` dataclass with `success`, `failure_code`, `status_reason`, `patched_files`

### P1.4: Implement `_run_tool_loop()` + `agent_loop()` (`engines/repair.py`)

- [x] `_run_tool_loop()`: message loop with function calling, tool dispatch, integrity check, max retries
- [x] `agent_loop()`: public entry point dispatching to `resolve` or `translate` operations

### P1.5: Implement five tools (`engines/repair.py`)

- [x] `read(path, line_range?)` — file content with optional line slicing
- [x] `grep(pattern, path)` — read-only regex search
- [x] `bash(cmd)` — read-only shell commands (git show :1:/:2:/:3:, grep, cat, test, diff only)
- [x] `write(path, content)` — complete file write (resolve op only)
- [x] `apply_patch(path, patch_text)` — structured diff with anchor matching (translate op only)

### P1.6: Update `LedgerRecord` to three-axis state (`models.py`, `ledger/store.py`)

- [x] Replace old `git`/`validation` fields with: `apply_status`, `apply_reason`, `integrity_status`, `validation_status`
- [x] Remove `manual_item` from `LedgerRecord`
- [ ] Add `MergeLedgerRecord` for merge-specific records (Phase 2)

### P1.7: Clean up `PromotionMethod` (`models.py`)

- [x] Remove `MANUAL`, `SEMANTIC_PORT_PENDING`
- [x] Keep `SKIP`, `CHERRY_PICK`, `PATH_LIMITED_APPLY_3WAY`, `SEMANTIC_PORT`, `MERGE`

### P1.8: Add `CommitGroup` + `_group_commits()` support

- [x] Define `CommitGroup` dataclass: `kind: CommitClass`, `commits: list[PlannedCommit]`
- [x] Implement `_group_commits(plan) -> list[CommitGroup]` in `promotion.py`
- [x] Reuse `CommitClass` for grouping (no separate `CommitGroupKind` needed)

### P1.9: Update tests

- [x] `test_phase1_workflow.py` — no `NEEDS_MANUAL_REVIEW` refs remained
- [x] `test_phase2_mechanical_git.py` — removed `manual_item` assertions, `fallback_files` → `failed_files`, `SEMANTIC_PORT_PENDING` → `SEMANTIC_PORT`
- [x] `test_phase3_semantic_port.py` — removed `manual_item` assertion, `SEMANTIC_PORT_PENDING` → `SEMANTIC_PORT`
- [x] `test_ledger_store.py` — updated record format to three-axis, removed `manual_item`

### P1.10: Run `ruff` + `pyright` + `pytest`

- [x] `ruff check .` — All checks passed
- [x] `pyright` — 0 errors, 0 warnings, 0 informations
- [x] `pytest` — 20 passed in 1.38s

---

## Phase 2: Merge Conflict Stratified Resolution — ✅ Complete

**Goal**: replace the current `_execute_merge` which used `_fallback_to_ours`
globally with the per-file stratified strategy.

### P2.1: File category classifier

- [x] Add `ConflictCategory` enum: `ISA_BACKEND`, `NON_SOURCE`, `SOURCE`
- [x] Add `classify_conflict_file(path)` in `analysis/classifier.py`

### P2.2: ISA backend → `checkout --theirs` + `git add`

- [x] `_resolve_isa_conflict(path)` — `git checkout --theirs` + `git add`

### P2.3: Non-source files → agent loop + ledger

- [x] `_resolve_non_source_conflict(path)` — agent loop, fallback to theirs

### P2.4: Source files → agent loop + ledger

- [x] `_resolve_source_conflict(path)` — agent loop, exhausted on failure

### P2.5: Integrate into `_execute_merge`

- [x] Replace old flat LLM loop with per-file stratified dispatch
- [x] Roll back entire merge if any source file is exhausted
- [x] Record `MergeLedgerRecord` on completion

---

## Phase 3: ISA Translation Agent — ✅ Complete

**Goal**: replace `_execute_semantic_port` with agent loop using `isa_translate`
op and structured ChangeSet output. Per-commit three-axis ledger display.

### P3.1: `isa_translate` agent loop

- [x] `isa_translate()` method in `RepairEngine` builds context + calls agent loop
- [x] `_execute_isa_translate()` in `promotion.py` replaces `_execute_semantic_port()`
- [x] Agent loop calls `_llm_call`; final response parsed as JSON ChangeSet on `op="translate"`

### P3.2: ChangeSet parsing and application

- [x] `_apply_changeset()` parses `{changes: [{op, path, edits/content}]}`
- [x] Each edit validated: exact anchor match, single hit required
- [x] `modify`/`replace` applies in memory; `create` writes new file
- [x] Paths resolved relative to `repo_root`

### P3.3: Per-commit three-axis ledger

- [x] `_ledger_record()` uses `apply_status` (committed/skipped/rolled_back) with `apply_reason`
- [x] `render_run` merge section: apply/integrity/validation three-axis
- [x] `render_run` per-commit section: apply/validation two-axis (integrity not tracked per-commit)
- [x] `_apply_status_label()` maps `GitOperationStatus` to human labels

---

## Phase 4: Range Recovery And Operational MVP

**Goal**: persist progress, resume interrupted runs, detect worktree drift.

- [ ] Persist per-commit progress in ledger or run-state file
- [ ] Resume partially completed run from last successful checkpoint
- [ ] Detect worktree mismatch with recorded safe point
- [ ] Record enough metadata to explain and recover failed runs

---

## Post-MVP

- [ ] Optional AST analyzer (libclang/tree-sitter when available)
- [ ] Symbol-name mapping beyond path convention
- [ ] Fallback reference triangulation (la64, arm64)
- [ ] Validation-repair retry policy
- [ ] Conflict-resolution repair path
- [ ] Integration tests against reduced fixture

---

## Definition Of Done

- [ ] `uv run ruff check .`
- [ ] `uv run pyright`
- [ ] `uv run pytest`
- [ ] `git diff --check`
- [ ] Only intended files are staged
- [ ] `box64-2-sw64/` and `box64_2_sw64.tar.gz` remain untracked unless explicitly requested
