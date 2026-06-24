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
counterpart.

Example:

```text
src/dynarec/rv64/...
src/dynarec/sw64/...
```

Workflow:

1. Detect that the commit touches reference ISA files.
2. Map changed reference files/symbols to target ISA candidates.
3. Extract the upstream intent from the reference diff.
4. Locate equivalent target patterns.
5. Apply the target-side semantic change.
6. Build and run targeted tests.

This is the main value of VPA. It is not a fallback; it is a first-class
promotion mode for unsupported architecture work.

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
reference_isa_change -> reference-ISA semantic path
cross_cutting        -> mechanical first, semantic repair if needed
generated_or_vendor  -> skip or manual, depending on project policy
unknown              -> inspect, then route
```

For `box64-2-sw64`, the important class is `reference_isa_change`: a commit that
touches RISC-V/RISC-family dynarec behavior may imply equivalent SW64 work even
when no SW64 file appears in the upstream commit.

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

## LLM Boundary

The LLM should not be asked to:

- call bookkeeping tools for every file
- record intent before every edit
- dry-run exact string replacements
- signal completion of each work item
- discover basic Git state that the orchestrator already knows

The LLM should be asked to:

- explain the intent of a reference ISA diff
- map reference ISA behavior to target ISA code
- resolve semantic conflicts
- repair targeted build/test failures
- produce an actionable manual note when mapping is unsafe

The LLM output should be patch-oriented or decision-oriented, not a long sequence
of state transition tool calls.

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

The new VPA should be built around these modules:

```text
vpa/
  git_engine.py        Git operations, checkpoints, conflict inspection
  classifier.py        Commit and file classification
  isa_mapper.py        Reference ISA -> target ISA file/symbol mapping
  semantic_porter.py   LLM-backed reference-to-target patch generation
  validator.py         Build/test execution and result parsing
  ledger.py            Append result records
  report.py            Human and machine-readable summaries
  main.py              Orchestrator CLI
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
