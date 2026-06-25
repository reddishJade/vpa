# AGENTS.md

Project-level instructions for agents working on VPA.

## Project Direction

VPA is an architecture-port promotion tool, not a generic per-file coding-agent
harness.

The current design is defined by [DESIGN.md](DESIGN.md). Follow that document
when implementation details are not explicit here.

The motivating path is:

```text
box64 RISC-V/RISC-family upstream -> SW64 local port
```

The replacement implementation must be workflow-first:

- Git is a first-class execution engine.
- Reference ISA changes are mapped to target ISA changes.
- Build and tests are the correctness gate.
- LLM use is reserved for semantic porting, conflict resolution, repair, and
  actionable manual notes.

Do not optimize the old ledger-driven agent loop. Do not add new behavior that
requires record-intent/start-work-item/read/edit/diff/decision/complete/done
round trips for each file.

## Current Architecture Target

New implementation work should move toward this layered package structure:

```text
vpa/
  main.py
  orchestrator/
    promotion.py
    llm_gate.py
    models.py
  engines/
    git.py
    validation.py
    repair.py
  analysis/
    classifier.py
    change_analyzer.py
    isa_mapper.py
  ledger/
    store.py
    report.py
```

Existing files such as `agent.py`, `harness.py`, `prompt.py`, `slicer.py`, and
`tools.py` belong to the previous design. Treat them as legacy unless a task
explicitly asks for compatibility or migration.

When adding new behavior, prefer the layered workflow packages over extending the
old agent harness or creating more top-level modules.

Ownership boundaries:

- `orchestrator/` owns the commit loop, routing, safe points, group execution, and LLM gate.
- `orchestrator/models.py` owns shared workflow records, enums, and gate policy.
- `orchestrator/llm_gate.py` owns pure gate decisions from analysis plus policy.
- `engines/git.py` owns Git operations and conflict detection.
- `engines/validation.py` owns build/test execution.
- `engines/repair.py` owns LLM-backed conflict, semantic-port, and validation
  repair patch generation.
- `analysis/classifier.py` owns commit and file classification.
- `analysis/change_analyzer.py` owns lightweight diff analysis and deterministic
  signals for the LLM gate.
- `analysis/isa_mapper.py` owns reference ISA to target ISA path mapping.
- `ledger/` owns result records and reports.

## Default ISA Policy

For `box64-2-sw64`, use these defaults unless the user or CLI overrides them:

```text
target ISA directory:    src/dynarec/sw64_core3
primary reference ISA:   src/dynarec/rv64
fallback references:     src/dynarec/la64, src/dynarec/arm64
```

`rv64` is the primary reference. `la64` and `arm64` are fallback references for
triangulation, not a combined first-pass "rv64-family" source.

The first ISA mapper implementation must use path convention mapping only:

```text
src/dynarec/rv64/                  -> src/dynarec/sw64_core3/
dynarec_rv64_<suffix>.c            -> dynarec_sw64_<suffix>.c
dynarec_rv64_<suffix>.h            -> dynarec_sw64_<suffix>.h
rv64_<suffix>.c/.h/.S              -> sw64_<suffix>.c/.h/.S
```

Leave room in APIs for later symbol matching, but do not implement symbol
matching before the path mapper and CLI skeleton are working.

## Milestone Order

The first implementation milestone is the new skeleton and CLI, not mechanical
Git execution and not LLM semantic porting.

Milestone 1 acceptance criteria:

- CLI accepts upstream repo, local repo, revision range, target ISA, primary
  reference ISA, fallback references, build command, and test commands.
- The orchestrator can enumerate upstream commits in order.
- The classifier labels commits as `shared_code`, `reference_isa_change`,
  `target_isa_direct`, `cross_cutting`, `generated_or_vendor`, or `unknown`.
- The change analyzer distinguishes non-semantic reference diffs from likely
  runtime-semantic diffs well enough to drive the initial LLM gate.
- The ISA mapper maps `rv64` paths to `sw64_core3` candidates by path convention.
- The ledger writes result-log records, not agent state-machine records.
- No LLM call is required.

Milestone 2 is the mechanical Git path. Milestone 3 is reference ISA to target
ISA semantic mapping.

## Execution Rules

Classify each commit before executing it:

```text
shared_code          -> mechanical Git path
target_isa_direct    -> mechanical Git path, then validation
reference_isa_change -> change analysis, then semantic path if needed
cross_cutting        -> mechanical first, semantic repair if needed
generated_or_vendor  -> skip or manual, depending on policy
unknown              -> inspect, then route
```

Classification only answers what area a commit touched. It must not directly
mean "call the LLM."

Per-commit flow:

```text
CommitInfo + DiffContext
  -> classifier
  -> isa_mapper
  -> CommitContext(full)
  -> change_analyzer
  -> llm_gate
  -> dispatch
```

Do not pass around one partially initialized `CommitContext` with optional
fields. Build a base context from `CommitInfo + DiffContext`, then construct the
full `CommitContext` only after `ClassifiedCommit` and per-file `MappingResult`
exist.

`DiffContext` must include both raw patch text and parsed hunks:

```text
DiffContext
  commit: CommitInfo
  raw_patch: str
  files: list[FileDiff]

FileDiff
  path_before: path | null
  path_after: path | null
  status: added | modified | deleted | renamed
  language: c | header | asm | build | text | unknown
  raw_patch: str
  hunks: list[DiffHunk]
```

Raw patch text is for provenance, ledger records, debugging, and LLM context.
Parsed hunks are the normal input to analyzers.

`MappingResult` must be per reference file, not a commit-level boolean:

```text
MappingResult
  file_mappings: list[FileMapping]
  unmapped_reference_files: list[path]
```

This lets `llm_gate` distinguish all mapped, partially mapped, and fully
unmapped commits.

### Commit Grouping

Commits are executed in upstream order, grouped by consecutive same
classification. Each group becomes one execution unit with its own checkpoint
and validation:

```text
upstream commits (after classification):
  [A(shared), B(shared), C(ref_isa), D(shared), E(shared), F(ref_isa)]

groups:
  group_1: [A, B]      → merge path (shared_code)
  group_2: [C]         → ISA translation path (reference_isa_change)
  group_3: [D, E]      → merge path (shared_code)
  group_4: [F]         → ISA translation path (reference_isa_change)
```

Group boundaries are determined by classification transitions. The group type
determines the execution path:

| Group type | Execution path | Rollback granularity | Validation |
|---|---|---|---|
| `shared_code` | `git merge` (one bulk merge, not per-commit) | entire group → checkpoint | group-level |
| `reference_isa_change` | per-commit ISA translation via agent loop | per-commit → checkpoint (inside group) | group-level |
| `target_isa_direct` | path-limited `git apply` per commit | per-commit → checkpoint | group-level |
| `cross_cutting` | merge first, then ISA repair per commit | per-commit → checkpoint | group-level |
| other | skip, record | — | — |

Non-shared_code groups execute each commit individually (each has its own
checkpoint and rollback), but validation runs once at group level. This ensures
that a validation failure can be traced to exactly the group type that
introduced it --- merge group failure = shared code issue, ISA translation
group failure = porting issue.

Group boundaries are computed during the planning phase, before execution:

```python
def group_commits(plan: PromotionPlan) -> list[CommitGroup]:
    groups: list[CommitGroup] = []
    for planned in plan.commits:
        kind = _group_kind(planned)
        if groups and groups[-1].kind == kind:
            groups[-1].commits.append(planned)
        else:
            groups.append(CommitGroup(kind=kind, commits=[planned]))
    return groups
```

For `reference_isa_change`, the orchestrator must run `change_analyzer` before
semantic porting. The analyzer output must be structured, not a binary
`has_semantic_change: bool`.

Initial result shape:

```text
ChangeAnalysis
  kind: comment_only | format_only | metadata_only | refactor |
        api_shape_change | logic_change | new_symbol | mixed | unknown
  signals: list[ChangeSignal]
  changed_symbols: list[str]
  mapped_target_candidates: list[path]
```

`change_analyzer` exposes one facade:

```text
analyze(diff_context, isa_mapping) -> ChangeAnalysis
```

Internally it should use an explicitly registered analyzer chain:

```text
sub-analyzers -> ChangeSignal list -> aggregator -> ChangeAnalysis
```

Do not implement dynamic plugin discovery for analyzers. Milestone 1 registers
only zero-dependency text analyzers. Optional AST analyzers may be added later as
explicit registrations in the same chain.

Sub-analyzers only produce `ChangeSignal` values. The aggregator owns
`kind` and the final gate decision.

The analyzer should produce deterministic signals such as:

- comment-only or whitespace-only diff
- formatting-only diff after normalization
- include/header/copyright/generated metadata only
- executable statement, control-flow, macro, constant, decode table, flag logic,
  helper-call, memory-ordering, or data-structure change
- mapped target candidate exists or is missing
- shared code or tests changed in the same commit

Each signal must include a source:

```text
diff_text
normalized
symbol_text
ast
```

Milestone 1 may use only required zero-dependency sources:

- `diff_text`: raw patch text and changed hunks
- `normalized`: changed hunks after stripping comments, blank lines, and
  practical formatting noise
- `symbol_text`: conservative text-pattern extraction of symbols and function
  signature shape

`ast` is optional later capability only. Do not require libclang, tree-sitter,
compiler databases, or macro expansion in Milestone 1. AST-backed signals may
raise confidence later, but VPA must still work without them.

The orchestrator's LLM gate owns the final decision:

```text
no_target_change
needs_semantic_port
needs_validation_only
```

The LLM gate should be a pure orchestrator module:

```text
llm_gate.decide(change_analysis, policy, context) -> GateDecision
```

`llm_gate` must not own repository state, mutate the worktree, call the LLM, or
read configuration by itself. The orchestrator constructs `GatePolicy` from
CLI/config values and passes it explicitly. Confidence thresholds, risk
preference, dry-run behavior, and project-specific choices belong in
`GatePolicy`.

Initial gate policy:

- comment-only, whitespace-only, or formatting-only reference diffs become
  `no_target_change`.
- obvious runtime-semantic reference diffs with mapped target candidates become
  `needs_semantic_port`.
- refactors or API shape changes should usually become
  `needs_validation_only`, not automatic LLM semantic porting.
- semantic reference diffs without a mapped target candidate become
  `needs_semantic_port`.
- target-direct or shared-code changes go through Git/validation before any LLM
  repair decision.

When the analyzer is uncertain, prefer `needs_semantic_port` over silently
skipping a reference ISA commit. Record the signals that caused the decision.

Dispatch outcomes:

```text
no_target_change    -> skip, record
needs_validation_only -> already handled by merge or path-limited apply
needs_semantic_port -> semantic_porter, validator, record
```

Use Git directly from the orchestrator for mechanical work. Do not ask an LLM to
drive Git state through tool-call bookkeeping.

Allowed Git-engine responsibilities include:

- create temporary work branches
- create checkpoints
- cherry-pick commits
- apply patches with three-way fallback
- inspect conflicts
- abort back to the last checkpoint
- commit successful promoted changes

Every operation that mutates a worktree must have a clear rollback/checkpoint
story before it is used in an automated path.

The orchestrator checkpoint granularity is one checkpoint per upstream commit.
Engines may create internal temporary checkpoints, but rollback at the
orchestrator boundary always means returning to the state before the current
upstream commit began.

## LLM Boundary

Do not use an LLM for:

- bookkeeping
- per-file state transitions
- routine Git discovery
- exact string replacement dry-runs when a structural or Git operation is enough
- deciding whether a workflow step is complete when validation can answer it
- deciding whether every reference ISA commit should call an LLM

Use an LLM only when the workflow has a semantic gap:

- explain a reference ISA diff
- map reference ISA behavior to target ISA code
- resolve semantic conflicts
- repair focused build/test failures

LLM output should be patch-oriented or decision-oriented.

Only the orchestrator's LLM gate decides when `engines/repair.py` is called.

## Ledger And Reports

The ledger is a result log, not the execution driver.

Record facts after workflow steps:

- upstream commit and subject
- classification
- execution method
- files changed
- reference ISA context
- target ISA context
- validation result
- whether an LLM was used

Reports should summarize promotion facts useful to a maintainer. Do not expose
internal agent turn bookkeeping.

## Coding Standards

Use Python 3.13.

Follow existing project tooling:

```powershell
uv run ruff check .
uv run pyright
uv run pytest
```

Keep changes scoped. Prefer small, testable modules with explicit dataclasses or
typed records for workflow results. Avoid speculative abstractions.

Use structured subprocess wrappers for Git and validation commands. Capture
stdout, stderr, exit code, cwd, and command arguments in result objects.

Do not shell out through a string when an argument list is sufficient. If a user
provided command must be executed through a shell, keep that boundary explicit in
the API and result record.

Validation for Milestone 1 and Milestone 2 is configuration-driven:

- configured build command
- configured smoke test commands
- optional glob-based test rules

Do not implement automatic box64 file-to-test inference in the initial
milestones.

## Testing Expectations

Add focused tests for new workflow modules.

For Milestone 1, tests should cover:

- commit/file classification
- `DiffContext` raw patch plus parsed hunks
- staged `CommitContext` construction without optional half-filled fields
- LLM gate decisions for comment-only, formatting-only, and semantic diffs
- structured `ChangeAnalysis` output and signal source recording
- operation without AST dependencies
- `rv64` to `sw64_core3` path mapping
- per-file `MappingResult`
- missing mapped target files
- CLI argument parsing
- ledger record serialization

Tests should not require the large `box64-2-sw64` worktree unless the task
explicitly asks for integration testing. Prefer small temporary Git repositories
or pure unit tests.

## Repository Hygiene

The root `box64-2-sw64/` directory and `box64_2_sw64.tar.gz` may be local test
fixtures or user-provided data. Do not add, delete, or rewrite them unless the
user explicitly asks.

Before committing, check the working tree and commit only the intended files.
