# VPA Implementation Phases

## Orchestrator Execution Loop (Affects All Phases)

All three phases converge on the orchestrator's `execute()` method, which
groups commits by consecutive same-classification and processes each group
as one unit:

```python
def execute(self) -> PromotionRun:
    dirty check
    plan = self.plan()                  # per-commit classification + gating
    groups = self._group_commits(plan)  # adjacent same classification → groups
    ledger = ...
    executed_merge = None

    for group in groups:
        if group.kind == CommitClass.SHARED_CODE:
            # one `git merge upstream/main` for the entire group
            # (not per-commit cherry-pick)
            executed_merge = self._execute_merge(group.commits)
            # validation runs once at group level
        elif group.kind == CommitClass.REFERENCE_ISA_CHANGE:
            for planned in group.commits:
                result = self._execute_isa_translate(planned)
                executed.append(result)
                ledger.append(_ledger_record(result))
            # validation runs once at group level
        elif group.kind == CommitClass.TARGET_ISA_DIRECT:
            for planned in group.commits:
                result = self._execute_path_limited(planned)
                executed.append(result)
                ledger.append(_ledger_record(result))
        else:
            skip

    return PromotionRun(plan=plan, merge=executed_merge, executed=executed)
```

Rollback granularity matches group type:
- `SHARED_CODE` group: one checkpoint for the whole group rollback
- `REFERENCE_ISA_CHANGE` group: per-commit checkpoint inside the group
- validation failure on any group → only that group's changes are in play

This structure replaces the previous `for commit in plan.commits` loop that
didn't group by type.

## Phase 1: Core Infrastructure Cleanup + Agent Loop

**Goal**: remove the broken `_fallback_to_ours`, `manual_item`, `confidence`/`threshold`
machinery; replace with the agent loop (function calling) and five tools; update
ledger format to three-axis state.

### P1.1: Delete `_fallback_to_ours` (`engines/repair.py`)

- Remove the `_fallback_to_ours()` function entirely
- Remove the call to it in `resolve_merge_conflicts()`

### P1.2: Delete all confidence/threshold/manual_item/MANUAL fields

Files to modify:

| File | What to remove |
|---|---|
| `models.py` | `GatePolicy.manual_confidence_threshold`, `LedgerRecord.manual_item`, `SemanticPortResult.manual_item`, `ExecutedCommit.manual_item`, `ExecutedMerge.manual_item`, `GateDecisionKind.NEEDS_MANUAL_REVIEW`, `PromotionMethod.MANUAL`, `PromotionMethod.SEMANTIC_PORT_PENDING` |
| `llm_gate.py` | `NEEDS_MANUAL_REVIEW` return path (the `if mapped: return NEEDS_MANUAL_REVIEW` block) |
| `config.py` | `manual_confidence_threshold` from `VPASettings` |
| `main.py` | `--manual-confidence-threshold` CLI arg and related code |
| `promotion.py` | All `manual_item` field references, `NEEDS_MANUAL_REVIEW` gate handler |
| `repair.py` | `threshold` param, `confidence`/`confidences` fields, `manual_item` references |

### P1.3: Add `AgentLoopResult` + `FailureCode` (`models.py`)

```python
class FailureCode(StrEnum):
    MAX_RETRIES = "max_retries"
    INTEGRITY_FAIL = "integrity_fail"
    LLM_ERROR = "llm_error"
    NO_LLM_CONFIGURED = "no_llm_configured"

@dataclass(frozen=True)
class AgentLoopResult:
    success: bool
    failure_code: FailureCode | None = None
    status_reason: str | None = None
    patched_files: list[Path] = field(default_factory=list)
```

### P1.4: Implement `_run_tool_loop()` (`engines/repair.py`)

Core loop:

```python
def _run_tool_loop(self, context, tools, system_prompt, max_retries=3) -> AgentLoopResult:
    messages = [{"role": "system", "content": system_prompt}, ...]
    for attempt in range(max_retries):
        response = client.chat.completions.create(
            model=...,
            messages=messages,
            tools=[tool_def for tool in tools],
        )
        msg = response.choices[0].message
        if msg.tool_calls:
            for call in msg.tool_calls:
                result = execute_tool(call.function.name, json.loads(call.function.arguments))
                messages.append({"role": "tool", ...})
        else:
            # LLM signals done -- extract result
            integrity = check_integrity(...)
            if integrity.passed:
                return AgentLoopResult(success=True)
            return AgentLoopResult(success=False, failure_code=FailureCode.INTEGRITY_FAIL, ...)
    return AgentLoopResult(success=False, failure_code=FailureCode.MAX_RETRIES, ...)
```

### P1.5: Implement five tools (`engines/repair.py`)

Each tool has a definition (JSON schema for function calling) and a handler:

| Tool | Schema | Handler |
|---|---|---|
| `read` | `{path: str, line_range?: [int, int]}` | `Path.read_text()` with optional slicing |
| `write` | `{path: str, content: str}` | `Path.write_text()` |
| `grep` | `{pattern: str, path: str}` | `re.findall()` or `subprocess.run(["grep", ...])` |
| `bash` | `{cmd: str}` | `subprocess.run(cmd, shell=True, capture_output=True)` |
| `apply_patch` | `{path: str, patch_text: str}` | Parse diff, verify anchor, apply to in-memory copy |

Tool security:
- `bash` is restricted: only `git show`, `grep`, `cat`, `test`, `diff` commands
- `write` warns if path is outside the repo directory
- `grep` is read-only, no side effects

### P1.6: Update `LedgerRecord` three-axis state (`models.py`, `ledger/store.py`)

```python
@dataclass(frozen=True)
class LedgerRecord:
    commit: CommitInfo
    classification: CommitClass
    gate: GateDecisionKind
    changed_files: list[Path]
    method: PromotionMethod = PromotionMethod.SKIP
    apply_status: str = "not_run"         # committed | rolled_back | exhausted
    apply_reason: str | None = None       # status_reason from agent loop
    integrity_status: str = "not_run"     # passed | failed
    validation_status: str = "not_run"    # passed | failed | not_configured
    llm_used: bool = False
```

Add merge record type:

```python
@dataclass(frozen=True)
class MergeLedgerRecord:
    strategy: str
    total_conflicts: int
    by_category: dict[str, int]
    resolutions: list[dict]
    apply_status: str
    integrity_status: str
    validation_status: str
```

### P1.7: Update `PromotionMethod` (`models.py`)

Remove:
- `MANUAL`
- `SEMANTIC_PORT_PENDING`

Keep:
- `SKIP`
- `CHERRY_PICK`
- `PATH_LIMITED_APPLY_3WAY`
- `SEMANTIC_PORT`
- `MERGE`

### P1.8: Update tests

Files to modify:

| File | What to change |
|---|---|
| `test_phase1_workflow.py` | Remove `NEEDS_MANUAL_REVIEW` test/assert; update gate tests |
| `test_phase2_mechanical_git.py` | Remove `manual_item` assertions; update merge result assertions |
| `test_phase3_semantic_port.py` | Update `manual_item` to `status_reason` / `failure_code` |
| `test_ledger_store.py` | Update ledger record format |

### P1.9: Run `ruff` + `pyright` + `pytest`

Gate: all three pass before proceeding to Phase 2.

---

## Phase 2: Merge Conflict Stratified Resolution

**Goal**: replace the current `_execute_merge` which used `_fallback_to_ours`
globally with the per-file stratified strategy.

### P2.1: File category classifier

Add a function in `analysis/classifier.py` or inline in `promotion.py`:

```python
class ConflictCategory(StrEnum):
    ISA_BACKEND = "isa_backend"
    NON_SOURCE = "non_source"
    SOURCE = "source"

def classify_conflict_file(path: Path) -> ConflictCategory:
    posix = path.as_posix()
    if any(posix.startswith(p) for p in ("src/dynarec/rv64/", "src/dynarec/arm64/", "src/dynarec/la64/")):
        return ConflictCategory.ISA_BACKEND
    if path.suffix in {".md", ".yml", ".yaml", ".toml", ".cfg"} or path.name == "CMakeLists.txt":
        return ConflictCategory.NON_SOURCE
    if posix.startswith("src/"):
        return ConflictCategory.SOURCE
    return ConflictCategory.NON_SOURCE
```

### P2.2: ISA backend → `checkout --theirs` + ledger

```python
def _resolve_isa_conflict(path: Path) -> dict:
    self.local_git._run_result(["checkout", "--theirs", str(path)])
    self.local_git._run_result(["add", str(path)])
    return {"path": path, "action": "theirs", "category": "isa_backend"}
```

### P2.3: Non-source files → agent loop + ledger

```python
def _resolve_non_source_conflict(path: Path) -> dict:
    result = self.repair_engine.agent_loop(file=path, op="resolve", ...)
    if result.success:
        self.local_git._run_result(["add", str(path)])
        return {"path": path, "action": "llm_resolved", "category": "non_source"}
    # fallback to theirs
    self.local_git._run_result(["checkout", "--theirs", str(path)])
    self.local_git._run_result(["add", str(path)])
    return {"path": path, "action": "theirs", "category": "non_source",
            "reason": result.status_reason, "failure_code": result.failure_code}
```

### P2.4: Source files → agent loop + ledger

```python
def _resolve_source_conflict(path: Path) -> dict:
    result = self.repair_engine.agent_loop(file=path, op="resolve", ...)
    if result.success:
        self.local_git._run_result(["add", str(path)])
        return {"path": path, "action": "llm_resolved", "category": "source"}
    return {"path": path, "action": "exhausted", "category": "source",
            "reason": result.status_reason, "failure_code": result.failure_code}
```

### P2.5: Integrate into `_execute_merge`

```python
def _execute_merge(self, plan: PromotionPlan) -> ExecutedMerge | None:
    checkpoint = self.local_git.checkpoint()
    merge_result = self.local_git.merge_from_ref(self.config.merge_source)

    if merge_result.status == GitOperationStatus.CONFLICT:
        resolutions = []
        for conflict_file in merge_result.conflicts:
            category = classify_conflict_file(conflict_file)
            if category == ConflictCategory.ISA_BACKEND:
                resolutions.append(self._resolve_isa_conflict(conflict_file))
            elif category == ConflictCategory.NON_SOURCE:
                resolutions.append(self._resolve_non_source_conflict(conflict_file))
            else:
                resolutions.append(self._resolve_source_conflict(conflict_file))

        exhausted = [r for r in resolutions if r["action"] == "exhausted"]
        if exhausted:
            self.local_git.reset_to_checkpoint(checkpoint)
            return ExecutedMerge(status="exhausted", resolutions=resolutions, ...)

        # Commit the merge
        ...
```

---

## Phase 3: ISA Translation Agent + Per-Commit Ledger — ✅ Complete

**Goal**: replace `_execute_semantic_port` with the agent loop using
`isa_translate` op and structured ChangeSet output. Add per-commit three-axis
ledger records.

### P3.1: `isa_translate` agent loop ✅

`isa_translate()` method added to `RepairEngine`. Builds semantic port context
with real analysis/gate_decision/local_repo, then calls `_run_tool_loop()` with
`op="translate"`.

### P3.2: ChangeSet format ✅

`_apply_changeset()` parses `{changes: [{op, path, edits/content}]}`. Validates
each edit with exact anchor match (single hit required), applies in memory, and
writes to disk. Paths resolved relative to `repo_root`.

### P3.3: `_execute_semantic_port` → `_execute_isa_translate` ✅

`_execute_isa_translate()` in promotion.py replaces `_execute_semantic_port()`.
Checkpoints before agent loop, stages all changes (`git add -A`), commits, runs
validation, rolls back on failure.

### P3.4: Per-commit ledger ✅

`_ledger_record()` now uses refined `apply_status` (committed/skipped/rolled_back)
and includes `apply_reason` from git stderr or skip reason.

### P3.5: `render_run` three-axis display ✅

Merge section shows apply/integrity/validation three-axis. Per-commit section
shows apply/validation. `_apply_status_label()` maps `GitOperationStatus` to
human-readable labels.
