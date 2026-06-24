# VPA TODO

This document turns the current design in `DESIGN.md` and `AGENTS.md` into an
implementation checklist. It is phase-oriented: each phase is a prerequisite for
the next one, not an isolated delivery target.

These phases define the first usable MVP, not the full completion of VPA. Phase
3 means the reference-ISA semantic-port path has a minimal working loop; it does
not mean VPA is finished.

## Current Constraints

- Build the new `vpa` workflow, not a V2 patch over the old per-file agent loop.
- Keep Git as a first-class engine.
- Do not use LLM calls for bookkeeping, routine Git state, or per-file progress
  transitions.
- Call the LLM only after the workflow exposes a semantic gap, a conflict, or a
  focused validation failure.
- Keep `box64-2-sw64/` and `box64_2_sw64.tar.gz` out of commits unless a task
  explicitly asks for them.

## Open Design Cleanup

- [x] Standardize gate decision naming across docs and code before Phase 1
      implementation. Preferred enum: `needs_validation_only`.
- [x] Decide how to handle the existing top-level legacy modules that conflict
      with the target package layout, especially `vpa/ledger.py` vs
      `vpa/ledger/`.
- [x] Decide whether legacy modules should move under a `legacy/` namespace or
      remain untouched until replaced.

## Phase 1: Skeleton And CLI

Goal: establish the new workflow shape without LLM dependency.

### Package Layout

- [x] Create `vpa/orchestrator/`.
- [x] Create `vpa/orchestrator/models.py`.
- [x] Create `vpa/orchestrator/promotion.py`.
- [x] Create `vpa/orchestrator/llm_gate.py`.
- [x] Create `vpa/engines/`.
- [x] Create `vpa/engines/git.py`.
- [x] Create `vpa/engines/validation.py`.
- [x] Create `vpa/engines/repair.py`.
- [x] Create `vpa/analysis/`.
- [x] Create `vpa/analysis/classifier.py`.
- [x] Create `vpa/analysis/change_analyzer.py`.
- [x] Create `vpa/analysis/isa_mapper.py`.
- [x] Create the new ledger/report package after resolving the legacy
      `vpa/ledger.py` naming conflict.

### Core Models

- [x] Define `CommitInfo`.
- [x] Define `DiffContext`.
- [x] Define `FileDiff`.
- [x] Define `DiffHunk`.
- [x] Define `DiffLine`.
- [x] Define `BaseCommitContext` for `CommitInfo + DiffContext`.
- [x] Define full `CommitContext` for commit, diff, classification, and
      mapping.
- [x] Define `ClassifiedCommit`.
- [x] Define commit/file classification enums.
- [x] Define `MappingResult` and per-file `FileMapping`.
- [x] Define `ChangeSignal`.
- [x] Define `ChangeAnalysis`.
- [x] Define `GatePolicy`.
- [x] Define `GatePolicy` fields: confidence thresholds, risk preference,
      dry-run flag, and per-project overrides.
- [x] Define `GateDecision`.
- [x] Define validation and ledger result records.

### CLI

- [x] Add CLI options for upstream repo.
- [x] Add CLI options for local repo.
- [x] Add CLI options for revision range.
- [x] Add CLI options for target ISA path.
- [x] Add CLI options for primary reference ISA path.
- [x] Add CLI options for fallback reference ISA paths.
- [x] Add CLI options for build command.
- [x] Add CLI options for smoke/test commands.
- [x] Add dry-run mode.
- [x] Add output paths for ledger/report artifacts.

### Git Read Path

- [x] Enumerate upstream commits in revision order.
- [x] Read commit metadata into `CommitInfo`.
- [x] Read raw patch text.
- [x] Parse raw patch into `DiffContext`, `FileDiff`, `DiffHunk`, and
      `DiffLine`.
- [x] Keep raw patch text available for provenance, ledger records, debugging,
      and future LLM context.

### Classifier

- [x] Classify `shared_code`.
- [x] Classify `reference_isa_change`.
- [x] Classify `target_isa_direct`.
- [x] Classify `cross_cutting`.
- [x] Classify `generated_or_vendor`.
- [x] Classify `unknown`.
- [x] Keep classification separate from LLM gate decisions.

### ISA Mapper

- [x] Implement path-only mapping from `src/dynarec/rv64/` to
      `src/dynarec/sw64_core3/`.
- [x] Implement filename transforms:
      `dynarec_rv64_* -> dynarec_sw64_*`.
- [x] Implement filename transforms:
      `rv64_* -> sw64_*`.
- [x] Return per-file mapping status: `mapped`, `missing_target`, `ambiguous`,
      or `not_reference_file`.
- [x] Preserve API room for later symbol mapping without implementing it now.

### Change Analyzer

- [x] Implement explicit analyzer chain registration.
- [x] Implement sub-analyzer interface that only emits `ChangeSignal`.
- [x] Implement aggregator that owns `kind`, `confidence`, and
      `suggested_gate`.
- [x] Implement aggregator rules: highest-risk kind wins; multiple meaningful
      kinds collapse to `mixed`; signals retain full detail regardless of final
      kind.
- [x] Implement diff-text analyzer.
- [x] Implement normalization analyzer for blank lines, comments, and practical
      formatting noise.
- [x] Implement conservative symbol/signature text analyzer.
- [x] Detect comment-only changes.
- [x] Detect whitespace/format-only changes.
- [x] Detect metadata/include-only changes.
- [x] Detect likely runtime semantic patterns: branches, returns, assignments,
      macro definitions, helper calls, opcode tables, constants, flag updates,
      and data-structure changes.
- [x] Define the initial risk order used by the aggregator.
- [x] Ensure Phase 1 works without libclang, tree-sitter, compiler
      databases, or macro expansion.

### LLM Gate

- [x] Implement pure `llm_gate.decide(change_analysis, policy, context)`.
- [x] Ensure gate does not mutate worktree, read config, call Git, or call an
      LLM.
- [x] Route non-semantic reference changes to `no_target_change`.
- [x] Route obvious semantic reference changes with mapped targets to
      `needs_semantic_port`.
- [x] Route semantic reference changes with missing/ambiguous target mapping to
      `needs_manual_review`.
- [x] Route shared or target-direct changes to `needs_validation_only` without
      LLM semantic porting.
- [x] Preserve gate reasons for ledger/report output.

### Ledger And Report

- [x] Implement append-only result records.
- [ ] Record commit, subject, classification, method, changed files, reference
      context, target context, validation result, LLM use, and manual item.
- [x] Keep ledger independent from execution control.
- [x] Generate a human-readable summary report.
- [x] Generate a machine-readable report artifact.

### Phase 1 Tests

- [x] Test CLI argument parsing.
- [x] Test commit/file classification.
- [x] Test raw patch plus parsed hunk `DiffContext`.
- [x] Test staged context construction without optional half-filled fields.
- [x] Test `rv64` to `sw64_core3` path mapping.
- [x] Test per-file `MappingResult`.
- [x] Test missing mapped target files.
- [x] Test comment-only and format-only change analysis.
- [x] Test obvious semantic diff analysis.
- [x] Test signal source recording.
- [x] Test gate decisions.
- [x] Test operation without AST dependencies.
- [x] Test ledger record serialization.

## Phase 2: Mechanical Git Path

Goal: run Git-first promotion end to end for commits that do not require
semantic porting.

- [ ] Create one orchestrator checkpoint per upstream commit.
- [ ] Implement temporary work branch support.
- [ ] Implement cherry-pick path.
- [ ] Implement path-limited patch application where useful.
- [ ] Define path-limited patch policy in the orchestrator: use classifier
      results and gate decisions to choose path-limited application; keep the
      Git engine as the executor, not the policy owner.
- [ ] Implement `git apply -3` fallback.
- [ ] Detect textual conflicts.
- [ ] Abort back to the current commit checkpoint.
- [ ] Run configured build command.
- [ ] Run configured smoke/test commands.
- [ ] Record success, conflict, validation failure, rollback, or manual result.
- [ ] Add tests with small temporary Git repositories.

## Phase 3: Reference ISA To Target ISA Semantic Mapping

Goal: handle `rv64` changes that imply `sw64_core3` work.

- [ ] Detect reference-ISA-only commits.
- [ ] Map touched reference files to target candidates.
- [ ] Build compact semantic-port context from reference diff and target file.
- [ ] Inject LLM client into `engines/repair.py`.
- [ ] Ask LLM for patch-oriented semantic port output only after gate approval.
- [ ] Apply proposed target-side patch through a controlled patch path.
- [ ] Validate patched target state.
- [ ] Record manual items when mapping is unsafe.
- [ ] Add tests for semantic-port context construction without requiring the
      large `box64-2-sw64` fixture.

## Phase 4: Range Recovery And Operational MVP

Goal: make the MVP usable on real upstream ranges without restarting from
scratch after the first failure.

- [ ] Persist per-commit progress in the ledger or a separate run-state file.
- [ ] Resume a partially completed commit range from the last successful
      checkpoint.
- [ ] Skip or continue past manual commits according to explicit policy.
- [ ] Detect when the local worktree no longer matches the recorded safe point.
- [ ] Record enough checkpoint metadata to explain and recover failed runs.
- [ ] Add tests for interrupted runs and resumed ranges.

## Post-MVP Roadmap

- [ ] Add optional AST analyzer as an explicit registered analyzer.
- [ ] Add symbol-name mapping from reference symbols to target candidates.
- [ ] Add confidence improvements from normalized AST equivalence.
- [ ] Add richer file-to-test policy tables.
- [ ] Add targeted test inference after configured validation is stable.
- [ ] Add fallback reference triangulation using `la64` and `arm64`.
- [ ] Add validation-repair retry policy with one focused repair attempt.
- [ ] Add conflict-resolution repair path.
- [ ] Add integration tests against a reduced fixture derived from
      `box64-2-sw64`.

## Definition Of Done

- [ ] `uv run ruff check .`
- [ ] `uv run pyright`
- [ ] `uv run pytest`
- [ ] `git diff --check`
- [ ] Only intended files are staged.
- [ ] `box64-2-sw64/` and `box64_2_sw64.tar.gz` remain untracked unless
      explicitly requested.
