# VPA Design

VPA is an architecture-port promotion tool. Its job is not to keep a local fork
merged with upstream. Its job is to keep an unsupported target ISA implementation
moving by translating upstream-supported ISA work into the local target ISA.

The motivating path is:

```text
box64 RISC-V/RISC-family upstream -> SW64 local port
```

If the local architecture were officially supported upstream, normal Git merge,
rebase, or cherry-pick would be enough. The hard case exists because upstream
continues to evolve supported architectures while the local ISA must infer and
apply equivalent behavior elsewhere.

## Core Model

VPA has three sources of truth:

1. **Git history**: what upstream changed and what can be applied mechanically.
2. **Reference ISA implementation**: how a nearby supported ISA expresses the
   intended behavior.
3. **Build and tests**: whether the target ISA still works after promotion.

The model is workflow-first. LLM calls are not the default execution unit. They
are used when Git, structural matching, or validation expose a semantic gap.

## Repository Roles

```text
upstream repo
  Official project history. For box64, this is the source of new commits.

reference ISA
  A supported upstream architecture that is semantically close enough to guide
  target work, for example RISC-V or another RISC-family backend.

target ISA
  The local unsupported architecture, for example SW64.

local repo
  The target fork/worktree being modified.
```

The reference ISA is first-class. VPA should compare upstream changes in the
reference backend against the current target backend before asking an LLM to
invent a porting strategy.

For `box64-2-sw64`, the default reference policy is:

```text
target ISA directory:    src/dynarec/sw64_core3
primary reference ISA:   src/dynarec/rv64
fallback references:     src/dynarec/la64, src/dynarec/arm64
```

`rv64` is the primary reference because its file layout is closest to
`sw64_core3` for the dynarec decoder/pass/helper structure. `la64` and `arm64`
are useful for triangulation when `rv64` lacks coverage or when a behavior was
implemented differently across backends, but they should not be merged into a
single ambiguous "reference upstream" in the first implementation.

## Execution Strategy

VPA runs commits in upstream order, but each commit is routed by capability.

### 1. Mechanical Git Path

Use Git directly when the commit is not architecture-specific or touches files
that are shared with the local fork:

- `git cherry-pick`
- `git merge`
- `git rebase`
- `git apply -3`
- path-limited patch application

This path should not call an LLM. It records the Git operation, changed files,
and validation result.

### 2. Reference-ISA Semantic Path

Use this path when upstream changes a supported backend that has a target-ISA
counterpart and the change analyzer determines that the diff may affect runtime
behavior.

Example:

```text
src/dynarec/rv64/...
src/dynarec/sw64/...
```

Workflow:

1. Detect that the commit touches reference ISA files.
2. Analyze the reference diff with lightweight workflow heuristics.
3. If the change is non-semantic, record `no_target_change` and continue.
4. Map changed reference files/symbols to target ISA candidates.
5. Extract the upstream intent from the reference diff.
6. Locate equivalent target patterns.
7. Apply the target-side semantic change.
8. Build and run targeted tests.

This is the main value of VPA. It is not a fallback; it is a first-class
promotion mode for unsupported architecture work.

The LLM is not responsible for deciding whether every reference-ISA commit needs
semantic porting. That decision belongs to the workflow.

The workflow should call the LLM only after cheaper gates have failed to prove
that no target-side action is needed.

### 3. Validation-Repair Path

Use this path when Git or semantic porting produced code but build/tests fail.

The repair context should include:

- failing command
- compiler/test output
- files changed by the current commit
- relevant reference ISA diff
- relevant target ISA code

The LLM may propose a small repair patch. VPA reruns validation after the patch.
If the second validation fails, the commit becomes manual.

### 4. Conflict Path

Use this path when Git reports textual conflicts.

The LLM receives only the conflict set, upstream patch, reference ISA context,
and target-side code. It should resolve the conflict or mark it manual. The LLM
does not advance global progress state by tool-call bookkeeping.

### 5. Manual Path

Use this path when the semantic mapping is unsafe.

Manual items must be actionable:

- upstream commit
- reference ISA file/symbol
- target ISA candidate file/symbol
- why the mapping is uncertain
- suggested next inspection or test

## Routing Rules

Each commit is classified before execution:

```text
shared_code          -> mechanical Git path
target_isa_direct    -> mechanical Git path, then validation
reference_isa_change -> change analysis, then reference-ISA semantic path if needed
cross_cutting        -> mechanical first, semantic repair if needed
generated_or_vendor  -> skip or manual, depending on project policy
unknown              -> inspect, then route
```

For `box64-2-sw64`, the important class is `reference_isa_change`: a commit that
touches RISC-V/RISC-family dynarec behavior may imply equivalent SW64 work even
when no SW64 file appears in the upstream commit.

Classification answers "what area did this commit touch?" It does not answer
"does this commit require LLM semantic porting?" The second question is answered
by the orchestrator's LLM gate using change-analysis facts.

## Per-Commit Data Flow

The orchestrator constructs per-commit context once and passes it downward.
Classifier, mapper, analyzer, gate, porter, validator, and ledger should not
independently query Git for the same commit facts.

Construction order is explicit:

```text
CommitInfo + DiffContext
  -> classifier
  -> isa_mapper
  -> CommitContext(full)
  -> change_analyzer
  -> llm_gate
  -> dispatch
```

Do not model this as one partially filled object with optional fields. Use a
base context for `CommitInfo + DiffContext`, then construct the full
`CommitContext` after classification and ISA mapping are available.

Core records:

```text
CommitContext
  commit: CommitInfo
  diff_context: DiffContext
  classification: ClassifiedCommit
  isa_mapping: MappingResult

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

DiffHunk
  old_start: int
  old_count: int
  new_start: int
  new_count: int
  section: str | null
  lines: list[DiffLine]

DiffLine
  kind: context | added | removed
  text: str
```

`DiffContext` includes both raw patch text and parsed hunks. Raw patch text is
for provenance, debugging, ledger records, and LLM context. Parsed hunks are the
normal input for sub-analyzers.

`MappingResult` is per reference file, not a commit-level boolean:

```text
MappingResult
  file_mappings: list[FileMapping]
  unmapped_reference_files: list[path]

FileMapping
  reference_file: path
  target_candidates: list[path]
  status: mapped | missing_target | ambiguous | not_reference_file
```

This lets the gate distinguish all mapped, partially mapped, and fully unmapped
commits.

Per-commit dispatch:

```text
no_target_change    -> skip, record
needs_validation    -> validator, record
needs_semantic_port -> semantic_porter, validator, record
needs_manual_review -> manual record
```

Module dependencies should point downward only:

```text
orchestrator
  -> classifier, isa_mapper, change_analyzer, llm_gate
  -> semantic_porter, validator
  -> ledger, report

change_analyzer -> DiffContext, MappingResult only
llm_gate        -> ChangeAnalysis, GatePolicy, CommitContext only
semantic_porter -> injected LLM client
validator       -> configured subprocess commands
ledger          -> filesystem only
```

## Change Analysis And LLM Gate

The LLM invocation decision belongs in the promotion orchestrator, not inside
the LLM repair engine.

The gate should be an orchestrator-level decision module, not a stateful engine:

```text
llm_gate.decide(change_analysis, policy, context) -> GateDecision
```

`llm_gate` should live under `orchestrator/` because its policy is part of the
promotion workflow. It must not own repository state, mutate the worktree, call
the LLM, or read configuration by itself. The orchestrator constructs
`GatePolicy` from CLI/config values and passes it explicitly into the gate.

`GatePolicy` is where confidence thresholds, risk preference, dry-run behavior,
and project-specific choices belong. The gate returns a decision with reasons;
the orchestrator decides which engine to call next.

For each `reference_isa_change`, the orchestrator should collect deterministic
signals before deciding whether to call the LLM:

- changed file paths and mapped target candidates
- diff hunks with comments and blank lines stripped
- whether only comments, formatting, includes, copyright, or generated metadata
  changed
- whether executable statements, control flow, constants, macros, instruction
  decoding, flag handling, helper calls, or data structures changed
- whether an equivalent target-side file exists
- whether the commit also changes shared code or tests

The analyzer output should be structured, not a binary
`has_semantic_change: bool`. Binary output loses the distinction between logic
changes, refactors, API shape changes, new symbols, and non-semantic churn.

Initial structured result:

```text
ChangeAnalysis
  kind: comment_only | format_only | metadata_only | refactor |
        api_shape_change | logic_change | new_symbol | mixed | unknown
  confidence: 0.0-1.0
  signals: list[ChangeSignal]
  changed_symbols: list[str]
  mapped_target_candidates: list[path]
  suggested_gate: no_target_change | needs_semantic_port |
                  needs_manual_review | needs_validation_only
```

The `suggested_gate` is advisory. The orchestrator owns the final gate decision.

`change_analyzer` should expose a single facade:

```text
analyze(diff_context, isa_mapping) -> ChangeAnalysis
```

Internally it should use an explicitly registered analyzer chain, not a dynamic
plugin framework:

```text
sub-analyzers -> ChangeSignal list -> aggregator -> ChangeAnalysis
```

Sub-analyzers only produce `ChangeSignal` values. They do not choose
`ChangeAnalysis.kind`, do not set the final confidence, and do not decide whether
to call the LLM. The aggregator owns `kind`, `confidence`, and
`suggested_gate`. The orchestrator owns the final gate decision.

Milestone 1 should register only zero-dependency text analyzers. Optional AST
analyzers may be added later as explicitly registered analyzers in the same
chain. They must not bypass the aggregator or the orchestrator's gate.

Each signal must record its source:

```text
diff_text     Required zero-dependency analysis from patch text.
normalized    Required zero-dependency analysis after stripping comments,
              whitespace, and other non-semantic text where practical.
symbol_text   Required zero-dependency symbol and signature extraction by
              conservative text patterns.
ast           Optional later capability, used only to raise confidence or
              refine a decision.
```

Milestone 1 must not require libclang, tree-sitter, compiler databases, or macro
expansion. The required analyzer path is pure diff-text and text normalization:

- strip blank lines and comments from changed hunks
- detect comment-only, whitespace-only, and formatting-only changes
- detect added/removed symbol-like identifiers
- detect function signature shape changes conservatively
- detect obvious runtime code patterns such as branches, returns, assignments,
  macro definitions, helper calls, opcode tables, constants, and flag updates

AST-backed signals such as `normalized_ast_equivalent` are allowed only as an
optional later enhancement. If AST support is unavailable, VPA must still work.
The result may have lower confidence, and the orchestrator may choose
`needs_manual_review` instead of directly choosing `needs_semantic_port`.

Initial decision outcomes:

```text
no_target_change       Deterministic analysis says the reference change is
                       non-semantic for the target.
needs_semantic_port    The reference change may affect runtime behavior and has
                       a plausible target mapping.
needs_manual_review    The diff is semantic but target mapping is missing or
                       ambiguous.
needs_validation_only  The change is shared or target-direct and should be
                       validated without LLM semantic porting.
```

Initial heuristics should be intentionally conservative:

- comment-only or whitespace-only reference diffs -> `no_target_change`
- formatting-only changes after parsing/normalization -> `no_target_change`
- renamed local variables with no structural change -> `no_target_change` when
  detectable without LLM
- changes to opcode decode tables, emitted instructions, flag logic, helper
  calls, memory ordering, constants, macros, or control flow ->
  `needs_semantic_port`
- semantic reference change with no mapped target candidate ->
  `needs_manual_review`

When the analyzer is uncertain, prefer `needs_semantic_port` or
`needs_manual_review` over silently skipping the commit. The gate should record
which deterministic signals caused the decision.

## ISA Mapping

The first implementation should use path convention mapping only.

For `box64-2-sw64`, the default mapping rules are:

```text
src/dynarec/rv64/                  -> src/dynarec/sw64_core3/
dynarec_rv64_<suffix>.c            -> dynarec_sw64_<suffix>.c
dynarec_rv64_<suffix>.h            -> dynarec_sw64_<suffix>.h
rv64_<suffix>.c/.h/.S              -> sw64_<suffix>.c/.h/.S
```

Missing mapped files are not immediate failures. They become `manual` or
`needs_symbol_mapping` depending on whether nearby target files exist.

Symbol-name matching is intentionally deferred. It is useful, but adding it
before the path mapper and CLI exist would make the first milestone harder to
verify. The mapper API should leave room for a later symbol pass:

```text
path_map(reference_file) -> target candidates
symbol_map(reference_symbol, target_candidates) -> ranked target symbols
```

## Git Is A First-Class Engine

VPA should use Git primitives directly from the orchestrator, not through an LLM
tool loop.

Allowed orchestrator operations include:

- create a temporary work branch
- create checkpoints
- cherry-pick commits
- apply patches with three-way fallback
- inspect conflicts
- abort a failed operation back to the last checkpoint
- commit successful promoted changes

These operations are workflow operations, not agent actions. They should be
guarded by explicit safe points and clear rollback behavior.

The orchestrator checkpoint granularity is one checkpoint per upstream commit.
Engines may create internal temporary checkpoints, but those are not part of the
orchestrator contract. From the orchestrator's perspective, rollback means
returning to the state before the current upstream commit began.

## LLM Boundary

The LLM should not be asked to:

- call bookkeeping tools for every file
- record intent before every edit
- dry-run exact string replacements
- signal completion of each work item
- discover basic Git state that the orchestrator already knows
- decide whether to call itself for every reference-ISA commit

The LLM should be asked to:

- explain the intent of a reference ISA diff
- map reference ISA behavior to target ISA code
- resolve semantic conflicts
- repair targeted build/test failures
- produce an actionable manual note when mapping is unsafe

The LLM output should be patch-oriented or decision-oriented, not a long sequence
of state transition tool calls.

The orchestrator may call the LLM only after the LLM gate returns
`needs_semantic_port`, after Git reports conflicts, or after validation failure
requires a focused repair attempt.

## Ledger

The ledger is a result log, not the driver of execution.

It records what happened after each workflow step. It does not force the LLM to
advance a state machine.

### Commit Record

```json
{
  "commit": "abc123",
  "subject": "update dynarec flag handling",
  "classification": "reference_isa_change",
  "status": "ported",
  "method": "reference_isa_semantic_port",
  "llm_used": true,
  "reference": {
    "isa": "rv64",
    "files": ["src/dynarec/rv64/emit_flags.c"],
    "symbols": ["emit_flags_update"]
  },
  "target": {
    "isa": "sw64",
    "files": ["src/dynarec/sw64/emit_flags.c"],
    "symbols": ["sw64_emit_flags_update"]
  },
  "git": {
    "operation": "semantic_patch",
    "changed_files": ["src/dynarec/sw64/emit_flags.c"]
  },
  "validation": {
    "build": "passed",
    "targeted_tests": "passed"
  },
  "manual": null
}
```

### Manual Record

```json
{
  "commit": "def456",
  "subject": "change vector load lowering",
  "classification": "reference_isa_change",
  "status": "manual",
  "method": "manual_required",
  "reference": {
    "isa": "rv64",
    "files": ["src/dynarec/rv64/vector.c"],
    "symbols": ["lower_vector_load"]
  },
  "target": {
    "isa": "sw64",
    "files": ["src/dynarec/sw64/vector.c"],
    "symbols": []
  },
  "manual": {
    "reason": "No clear SW64 equivalent for the new RV64 vector load lowering path.",
    "next_step": "Identify whether SW64 implements this path in scalar fallback or needs a new lowering helper."
  }
}
```

## Validation

Validation is the main correctness gate.

For `box64-2-sw64`, useful validation layers are:

- compile the SW64 target
- run available box64 test sets
- run focused dynarec tests when touched files are dynarec-related
- run smoke tests for binaries that exercise translated instructions

Validation failures should route to repair once. Repeated failure becomes manual.

Milestone 1 and Milestone 2 validation should stay configuration-driven:

- configured build command
- configured smoke test commands
- optional glob-based test rules

Automatic file-to-test inference is a later capability. Early validation should
not try to infer all box64 targeted tests from changed files. A simple policy
table is acceptable:

```text
src/dynarec/**        -> build + configured dynarec/smoke tests
src/dynarec/rv64/**   -> no direct target test unless semantic port changes target files
src/dynarec/sw64*/**  -> build + configured SW64-focused tests
tests/**              -> run configured touched/nearby tests when available
build files           -> configure/build validation
```

## Report

The final report should answer:

- which upstream commits were mechanically applied
- which commits required reference-ISA semantic porting
- which target files changed
- which validations passed or failed
- which commits need manual architecture judgment
- what upstream range is now covered by the target ISA

The report should not expose internal agent turn bookkeeping. It should expose
promotion facts useful to a maintainer.

## Implementation Direction

The new VPA should be built around a layered package structure:

```text
vpa/
  main.py
  orchestrator/
    promotion.py       Commit loop, routing, safe points, LLM gate decisions
    models.py          Shared workflow records and enums
  engines/
    git.py             Git operations, checkpoints, conflict inspection
    validation.py      Build/test execution and result parsing
    repair.py          LLM-backed conflict, semantic-port, and repair patches
  analysis/
    classifier.py      Commit and file classification
    change_analyzer.py Lightweight diff semantics and LLM gate signals
    isa_mapper.py      Reference ISA -> target ISA file/symbol mapping
  ledger/
    store.py           Append result records
    report.py          Human and machine-readable summaries
```

The old agent-loop design should not be optimized further. The replacement
should start from the architecture-port workflow above.

## Milestones

### Milestone 1: New VPA Skeleton And CLI

Goal: create the new workflow shape without depending on the old per-file agent
loop.

Acceptance criteria:

- CLI accepts upstream repo, local repo, revision range, target ISA, primary
  reference ISA, fallback references, build command, and test commands.
- The orchestrator can enumerate upstream commits in order.
- The classifier can label commits as `shared_code`, `reference_isa_change`,
  `target_isa_direct`, `cross_cutting`, `generated_or_vendor`, or `unknown`.
- The change analyzer can distinguish comment/format-only reference changes from
  likely runtime-semantic reference changes well enough to drive the initial LLM
  gate.
- The ISA mapper can map `rv64` paths to `sw64_core3` candidate paths using path
  convention only.
- The ledger writes result-log records, not agent state-machine records.
- No LLM call is required for this milestone.

### Milestone 2: Mechanical Git Path

Goal: run the Git-first path end to end.

Acceptance criteria:

- Create checkpoint.
- Try cherry-pick or three-way patch application.
- Run build and configured fast tests.
- Record success, conflict, validation failure, or rollback result.

### Milestone 3: Reference ISA To Target ISA Semantic Mapping

Goal: handle `rv64` changes that imply `sw64_core3` work.

Acceptance criteria:

- Detect reference-ISA-only commits.
- Map touched `rv64` files to `sw64_core3` candidates.
- Build a compact semantic-port context from reference diff and target file.
- Produce a patch or manual item.
- Validate the result.
