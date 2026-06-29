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

## Repository State: The Local Fork Is The Work Target

The local repo is a heavily diverged fork. Upstream and local have changed the
same shared files independently. The merge-tree analysis on the actual
`box64-2-sw64` fork found:

- 331 conflict files when merging `upstream/main` into the local fork
- 100% of those files had local modifications (the local fork changed every
  conflicted file)
- 116 of the 331 are ISA backend files (rv64/arm64/la64)
- ISA backend files have zero SW64-specific local commits -- all local changes
  to ISA backends came from upstream syncs
- The remaining 215 are shared source files (emu, wrapped, include, tools, etc.)
  with real semantic divergence

This data drives the conflict stratification strategy.

## Execution Strategy

### Commit Execution Order

Commits are executed in upstream order. The Git operation is always per-commit
(`git cherry-pick` or path-limited `git apply`) so that the resulting local
history preserves the source of every upstream change. Classification grouping
is still computed during planning for reporting and for optional validation
batching, but each upstream commit gets its own checkpoint and is validated
individually.

```text
upstream commits (after classification):
  [A(shared), B(shared), C(ref_isa), D(shared), E(shared), F(ref_isa)]

execution:
  A → cherry-pick, validate
  B → cherry-pick, validate
  C → cherry-pick reference files, semantic port to target ISA, validate
  D → cherry-pick, validate
  E → cherry-pick, validate
  F → cherry-pick reference files, semantic port to target ISA, validate
```

Group boundaries are determined by classification transitions and are used for
plan rendering and reporting:

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

### Preprocessing

Before classification, generated and vendor paths are removed from the diff
context so they are never analyzed, mapped, or executed. For box64 this covers
paths such as `src/wrapped/generated/` and `src/wrapped32/generated/`.

### Preprocessor Conditional Hook

Path-only classification cannot detect ISA-specific changes that live inside
shared source files guarded by C preprocessor conditionals. Box64 uses blocks
such as `#if defined(RV64)` and, increasingly, `#if defined(RV64) || defined(SW64)`.

During planning VPA fetches the parent version of each shared file from the
upstream repository and parses the `#if`/`#ifdef`/`#elif`/`#else`/`#endif`
stack. Changed hunks that fall inside an RV64-only or SW64-only conditional
block upgrade the file classification from `shared_code` to `cross_cutting`,
because the same logical change usually needs a corresponding update in the
target ISA. Changes inside `#if defined(RV64) || defined(SW64)` remain
`shared_code`: the SW64 code path already receives the change mechanically.

### 1. Mechanical Git Path (Per-Commit Cherry-Pick)

VPA's main execution unit is the upstream commit, applied in order with
`git cherry-pick`. Each successful cherry-pick keeps the original upstream
subject and adds `(cherry picked from commit <sha>)`, preserving provenance.

- `shared_code` and `cross_cutting` commits are cherry-picked.
- `target_isa_direct` commits use path-limited `git apply -3` when only target
  files should be taken.
- `reference_isa_change` commits are also cherry-picked to keep the reference
  ISA files in sync with upstream, followed by semantic porting to the target
  ISA when required.

This path does not call an LLM. It records the Git operation, changed files,
and validation result.

### 2. Reference-ISA Semantic Path

Use this path when upstream changes a supported backend that has a target-ISA
counterpart and the change analyzer determines that the diff may affect runtime
behavior.

Workflow:

1. Detect that the commit touches reference ISA files (or RV64-only conditional
   blocks inside shared files).
2. Analyze the reference diff with lightweight workflow heuristics.
3. If the change is non-semantic, record `no_target_change` and continue.
4. Map changed reference files/symbols to target ISA candidates.
5. LLM translates the RV64 diff into a structured SW64 ChangeSet.
6. The ChangeSet is applied with anchored edit matching (not raw `git apply`).
7. Build and run targeted tests.

The LLM output for ISA translation is a structured ChangeSet with per-file
anchored edits (`old -> new`), not a raw unified diff. This allows the harness
to verify baseline integrity before writing.

### 3. Validation-Repair Path

Use this path when Git or semantic porting produced code but build/tests fail.

The repair context should include:
- failing command and its output
- files changed by the current commit
- relevant reference ISA diff
- relevant target ISA code

The LLM may propose a small repair patch. VPA reruns validation after the patch.
If the second validation fails, the commit enters `exhausted` state in the
ledger.

### 4. Cherry-Pick Conflict Stratified Resolution

Use this path when `git cherry-pick` reports textual conflicts.

Because commits are atomic, a cherry-pick with any unresolved conflict causes
the entire upstream commit to be rolled back. Conflicts are classified by file
path into three categories:

| Category | Criteria | Action (v1.0.0) |
|---|---|---|
| ISA backend | `src/dynarec/{rv64,arm64,la64}/*` | Record pending human review, rollback commit |
| Non-source | `*.md`, `*.yml`, `CMakeLists.txt`, docs, CI, etc. | LLM agent loop; fallback `--theirs` |
| Source files | All other `src/` files | LLM agent loop; failure = rollback commit |

In v1.0.0 ISA backend conflicts are deliberately **not** auto-resolved. The
reference ISA files often carry semantic changes that must be mirrored in the
target ISA (`sw64_core3`), so a blind `checkout --theirs` is unsafe. Instead,
VPA writes a `PendingConflictRecord` to the ledger and rolls the commit back.
Subsequent upstream commits that touch the same pending file are skipped until
a human resolves the conflict and appends a resolution record to the ledger.

LLM-based resolution of ISA backend conflicts is reserved for v2+.

The per-file classification uses glob matching:

```python
def classify_conflict(path: Path) -> ConflictCategory:
    if str(path).startswith(("src/dynarec/rv64/", "src/dynarec/arm64/", "src/dynarec/la64/")):
        return ConflictCategory.ISA_BACKEND
    if path.suffix in {".md", ".yml", ".yaml", ".toml", ".cfg"} or path.name == "CMakeLists.txt":
        return ConflictCategory.NON_SOURCE
    return ConflictCategory.SOURCE
```

## Agent Loop Design

Both merge conflict resolution and ISA translation use a shared agent loop with
function calling. The loop is:

```text
1. Harness provides initial context (file path, operation type)
2. LLM decides which tool to call
3. Harness executes the tool, returns result to LLM
4. LLM continues calling tools or signals completion
5. Harness validates the result (integrity check)
6. If check fails, retry (max N times) or fail
```

The agent loop returns:

```python
@dataclass
class AgentLoopResult:
    success: bool
    failure_code: FailureCode | None
    status_reason: str | None

class FailureCode(StrEnum):
    MAX_RETRIES = "max_retries"
    INTEGRITY_FAIL = "integrity_fail"
    LLM_ERROR = "llm_error"
    NO_LLM_CONFIGURED = "no_llm_configured"
```

`failure_code` drives orchestrator logic (e.g., INTEGRITY_FAIL means preflight
should be checked before retrying). `status_reason` goes to the ledger for
downstream agents to read. There is no `manual_item` field -- agents do not
write hints for humans.

### Tool Set

Both agent paths share a common tool set:

| Tool | Parameters | Scope | Used by |
|---|---|---|---|---|
| `read` | `path`, `line_range?` | Read file content | resolve, translate |
| `write` | `path`, `content` | Write complete file | resolve |
| `grep` | `pattern`, `path`, `context_lines?` | Recursive search in .c/.h files via grep(1) | resolve, translate |
| `glob` | `pattern`, `path` | Find files by glob pattern under a directory | resolve, translate |
| `bash` | `cmd` | Arbitrary shell command | resolve (git show :1:/:2:/:3:) |
| `apply_patch` | `path`, `patch_text` | Apply structured diff | translate |

Tool boundary rules:
- `grep` is read-only and searches .c/.h files recursively via grep(1); paths are validated to be within the repo
- `glob` is read-only and searches a directory tree by glob pattern; limit 200 results
- `bash` is restricted to git commands and integrity checks
- `write` is the only tool that creates or overwrites files
- `apply_patch` verifies all hunks before writing any file

### Rollback

Both paths use the same checkpoint-based rollback:

```text
checkpoint = git rev-parse HEAD
execute(...)
if failure:
    git reset --hard checkpoint
```

This is commit-level granularity. The orchestrator does not attempt per-file
recovery. The ledger records each file's before/after state for audit, but the
rollback mechanism is always `git reset --hard checkpoint`.

## Gate Decisions

Each commit is classified before execution. The gate produces one of:

```text
no_target_change       -> skip, record
needs_semantic_port    -> ISA translation agent, validate, record
needs_validation_only  -> already handled by merge or path-limited apply
```

`NEEDS_MANUAL_REVIEW` and `MANUAL` do not exist as gate decisions. When a gate
cannot determine the correct path (low confidence, missing mapping), it prefers
`needs_semantic_port` and lets the agent loop or downstream ledger state handle
it.

## Three-Axis Ledger State

Every operation records three independent axes:

```json
{
  "apply": {
    "status": "committed | rolled_back | exhausted",
    "method": "merge | semantic_port | skip",
    "reason": "integrity check failed: residual conflict markers"
  },
  "integrity": {
    "status": "passed | failed",
    "checks": ["baseline_hash", "anchored_match", "no_markers"]
  },
  "validation": {
    "status": "passed | failed | not_configured",
    "build": "passed | failed | not_run",
    "smoke": ["passed", "not_run"]
  }
}
```

These three axes are independent:
- A file can be committed (apply=committed) but fail integrity (unlikely but
  possible if checks are misconfigured)
- A file can be committed and pass integrity but fail validation (build breaks
  for unrelated reasons)
- validation=not_configured is not the same as validation=passed

### Merge Record

```json
{
  "type": "merge",
  "strategy": "upstream/main",
  "total_conflicts": 331,
  "by_category": {
    "isa_backend": 116,
    "non_source": 21,
    "source": 194
  },
  "resolutions": [
    {"path": "src/dynarec/rv64/foo.c", "action": "theirs", "category": "isa_backend"},
    {"path": "README.md", "action": "llm_resolved", "category": "non_source"},
    {"path": "src/emu/x64run.c", "action": "exhausted", "category": "source",
     "reason": "agent_loop exceeded max_retries=3",
     "failure_code": "max_retries",
     "after_hash": "abc123..."}
  ]
}
```

### Commit Record (ISA Translation)

```json
{
  "type": "commit",
  "sha": "abc123...",
  "subject": "update dynarec flag handling",
  "classification": "reference_isa_change",
  "gate": "needs_semantic_port",
  "apply": {
    "status": "committed",
    "method": "semantic_port",
    "reason": null
  },
  "integrity": {"status": "passed", "checks": ["baseline_hash", "anchored_match", "no_markers"]},
  "validation": {"status": "not_configured", "build": "not_run", "smoke": []}
}
```

### Needs-Human Record

When all automated paths fail:

```json
{
  "type": "commit",
  "sha": "def456...",
  "subject": "change vector load lowering",
  "classification": "reference_isa_change",
  "gate": "needs_semantic_port",
  "apply": {
    "status": "exhausted",
    "method": "semantic_port",
    "reason": "agent_loop exceeded max_retries=3: anchored edit matching failed on src/dynarec/sw64_core3/vector.c: old string not found in file",
    "failure_code": "max_retries"
  },
  "integrity": {"status": "passed", "checks": ["baseline_hash", "anchored_match", "no_markers"]},
  "validation": {"status": "not_run"}
 }
 ```

The `exhausted` state does not mean "a person should open an editor and fix
this." It means "the automated paths did not produce a safe result; a downstream
agent or process should inspect this record and decide how to proceed." The
ledger is the communication mechanism between automated passes, not a todo list
for human maintainers.

## Per-Commit Data Flow

Construction order is explicit:

```text
CommitInfo + DiffContext
  -> preprocessor hook (shared-file conditional blocks)
  -> classifier
  -> isa_mapper
  -> CommitContext(full)
  -> change_analyzer
  -> llm_gate
  -> dispatch
```

Do not model this as one partially filled object with optional fields. Use a
base context for `CommitInfo + DiffContext`, enrich it with conditional
classification, then construct the full `CommitContext` after classification
and ISA mapping are available.

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

Module dependencies should point downward only:

```text
orchestrator
  -> classifier, isa_mapper, change_analyzer, llm_gate
  -> git_engine, repair_engine, validator
  -> ledger, report

change_analyzer -> DiffContext, MappingResult only
llm_gate        -> ChangeAnalysis, GatePolicy, CommitContext only
repair_engine   -> injected LLM client (OpenAI compatible API)
validator       -> configured subprocess commands
ledger          -> filesystem only
```

## Change Analysis And LLM Gate

The LLM invocation decision belongs in the promotion orchestrator, not inside
the LLM repair engine.

The gate should be an orchestrator-level decision module:

```text
llm_gate.decide(change_analysis, policy, context) -> GateDecision
```

`llm_gate` must not own repository state, mutate the worktree, call the LLM, or
read configuration by itself. The orchestrator constructs `GatePolicy` from
CLI/config values and passes it explicitly into the gate.

`GatePolicy` is where confidence thresholds, risk preference, dry-run behavior,
and project-specific choices belong. The gate returns a decision with reasons;
the orchestrator decides which engine to call next.

Initial gate decisions:

```text
no_target_change       Deterministic analysis says the reference change is
                       non-semantic for the target.
needs_semantic_port    The reference change may affect runtime behavior.
needs_validation_only  The change is shared or target-direct and should be
                       validated without LLM semantic porting.
```

`NEEDS_MANUAL_REVIEW` is deliberately absent. When the gate cannot determine
the correct path, it prefers `needs_semantic_port` and lets the agent loop or
downstream ledger state handle the case. The `manual_item` / `confidence` /
`threshold` concepts are removed entirely -- the agent loop's integrity check
provides an objective binary result (passed / failed), not a subjective score.

## ISA Mapping

The first implementation uses path convention mapping only.

For `box64-2-sw64`, the default mapping rules are:

```text
src/dynarec/rv64/                  -> src/dynarec/sw64_core3/
dynarec_rv64_<suffix>.c            -> dynarec_sw64_<suffix>.c
dynarec_rv64_<suffix>.h            -> dynarec_sw64_<suffix>.h
rv64_<suffix>.c/.h/.S              -> sw64_<suffix>.c/.h/.S
```

Missing mapped files are handled by the ISA translation agent (which can create
new files). The isa_mapper records `missing_target` status, and the gate routes
to `needs_semantic_port` regardless.

## Git Is A First-Class Engine

VPA uses Git primitives directly from the orchestrator, not through an LLM tool loop.

### Orchestrator-Only Operations (via GitEngine)

These Git write operations are called only by the `PromotionOrchestrator` through
`GitEngine` methods. The `RepairEngine` has no command execution capability --
its output is always text (resolved file content, structured ChangeSet).

| Operation | GitEngine method | Called by |
|---|---|---|
| cherry-pick | `cherry_pick_from(repo, sha)` | orchestrator.`_apply_mechanical_commit` |
| continue cherry-pick | `commit_cherry_pick(message, author)` | orchestrator after conflict resolution |
| apply patch (3-way) | `apply_patch_3way(patch)` | orchestrator (path-limited or semantic) |
| fetch | `_run_result(["fetch", ...])` | orchestrator via cherry-pick |
| show file | `show_file(path, sha)` | orchestrator/preprocessor hook |
| checkpoint | `checkpoint()` (git rev-parse HEAD) | orchestrator before each commit |
| reset hard | `reset_to_checkpoint(sha)` | orchestrator rollback |
| add | `_run_result(["add", path])` | orchestrator after agent loop returns `patched_files` |
| commit | `_run_result(["commit", ...])` | orchestrator after apply/semantic port |

### RepairEngine Boundary

The `RepairEngine` does not call Git. It has two output modes:

1. **Conflict resolution**: returns resolved file content. The orchestrator
   writes it to disk and calls `git add`.
2. **ISA translation**: returns a structured ChangeSet. The orchestrator
   applies edits to files in memory, verifies integrity, writes to disk via
   temp file + atomic rename, and calls `git add`.

The agent loop inside `RepairEngine` has tool access for reading files and
inspecting content, but it cannot write to disk or call Git. All write
operations go through the orchestrator.

### Bash Tool Restrictions

Phase 1 introduces a `bash` tool in the agent loop. Its restrictions:

- Only read-only commands: `git show :1:<path>`, `git show :2:<path>`,
  `git show :3:<path>`, `grep`, `cat`, `test`, `diff`
- `git add` is NOT in the whitelist. After the agent loop completes, the
  orchestrator iterates `AgentLoopResult.patched_files` and runs `git add`
  for each resolved file.
- The whitelist is enforced by the tool handler, not by convention.

### Checkpoint and Rollback

The orchestrator checkpoint granularity is one checkpoint per upstream commit.
Engines may create internal temporary checkpoints, but those are not part of the
orchestrator contract. From the orchestrator's perspective, rollback means
returning to the state before the current upstream commit began.

Rollback always uses `git reset --hard <checkpoint>`. There is no per-file
recovery mechanism. The ledger records each file's state for audit, but the
rollback mechanism is always at checkpoint granularity.

When a cherry-pick produces conflicts that include `exhausted` files (agent loop
could not resolve), the entire upstream commit is rolled back to the pre-commit
checkpoint. The ledger retains the conflict records so that subsequent runs can
inspect them.

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
- map reference ISA behavior to target ISA code via structured ChangeSet
- resolve merge conflicts (with full 3-way content: base/ours/theirs)
- repair targeted build/test failures

The LLM output for ISA translation is a structured, anchored ChangeSet, not a
raw unified diff. The harness parses the LLM output, pre-applies edits in
memory, verifies baseline hashes, and only then writes to disk.

The LLM output for conflict resolution is complete resolved file content, not
patch fragments. The harness writes the resolved content and verifies no
conflict markers remain.

## Ledger

The ledger is a result log for downstream agents, not a human-facing task list.
It does not contain `manual_item` fields or human instructions.

### Record Types

- `merge`: records the entire merge operation, conflict count, per-file
  resolution decisions, and integrity/validation status
- `commit`: records per-commit ISA translation results with three-axis state

Each record uses three independent status axes:
- `apply`: committed | rolled_back | exhausted
- `integrity`: passed | failed
- `validation`: passed | failed | not_configured

### Key Differences From Previous Design

| Removed | Replaced By |
|---|---|
| `manual_item: str` | `status_reason: str` (for downstream agents) |
| `NEEDS_MANUAL_REVIEW` gate | `needs_semantic_port` + agent loop + ledger state |
| `confidence: 0.0-1.0` | integrity check binary result |
| `threshold` | max_retries for agent loop |
| `PromotionMethod.MANUAL` | method stays same, state reflects failure |
| `PromotionMethod.SEMANTIC_PORT_PENDING` | method stays `SEMANTIC_PORT`, state reflects result |

## Validation

Validation is the main correctness gate.

For `box64-2-sw64`, useful validation layers are:

- compile the SW64 target
- run available box64 test sets
- run smoke tests for binaries that exercise translated instructions

Validation should be configuration-driven:

- configured build command
- configured smoke test commands

Validation is the third axis in the three-axis ledger state. It runs after
apply and integrity checks pass. Validation failure does not trigger automatic
rollback (unlike apply/integrity failure). It is recorded in the ledger for
the downstream agent to inspect and repair.

## Report

The final report should answer:

- which upstream commits were mechanically applied
- which commits required reference-ISA semantic porting
- which target files changed
- which validations passed or failed
- which commits are in `exhausted` state
- what upstream range is now covered by the target ISA

The report should not expose internal agent turn bookkeeping. It should expose
promotion facts useful to a maintainer or downstream automation.

## Implementation Phases

See [PHASES.md](PHASES.md) for the detailed implementation plan.

## Package Structure

```text
vpa/
  main.py
  orchestrator/
    promotion.py       Commit loop, routing, safe points, LLM gate, merge flow
    models.py          Shared workflow records, enums, gate policy
    llm_gate.py        Pure gate decision logic (no LLM calls, no repo mutation)
  engines/
    git.py             Git operations: merge, cherry-pick, apply, checkpoint
    repair.py          LLM agent loop: tool definitions, function calling driver
    validation.py      Build/test execution and result parsing
  analysis/
    classifier.py      Commit and file classification
    change_analyzer.py Lightweight diff semantics and LLM gate signals
    isa_mapper.py      Reference ISA -> target ISA file/symbol mapping
    preprocessor.py    Preprocessor conditional hook for shared files
  ledger/
    store.py           Append-only result records (merge, commit, exhausted)
    report.py          Human and machine-readable summaries
```
