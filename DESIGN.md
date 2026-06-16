# DESIGN.md

## Architecture

**Single agent + dual-trigger restart.** One agent instance processes commits sequentially. Restart triggers:

1. Processed N commits (configurable)
2. Context usage exceeds 65% (char-count proxy: `sum(len(content)) / model_limit_chars`)

Restart passes to the new instance:

- Fixed system prompt
- Ledger snapshot (commit-level only, hunks not expanded)
- Current commit context (diff + local file current content)

All other conversation history is discarded.

## Progress Source

**Ledger drives, Git verifies.** The ledger is the authoritative progress source. When agent claims a commit is "ported", harness runs `git diff HEAD` to check that local changes actually landed. Ported-in-ledger but no-change-in-git is treated as a warning signal.

## Slicing Strategy

**Commit as primary unit.** When a commit exceeds threshold (>300 lines or >8 files), fall back to **file granularity**. When a single file exceeds >200 lines, fall back to **hunk granularity** with full file snapshot as background.

### File ordering at file-level fallback

Harness performs dependency analysis on affected files (regex-based import/include parsing), limited to the commit's affected file subset. Topologically sorts files by dependency. Fallback heuristic when parsing fails:

1. `.h` / `.proto` / interface definitions
2. Utility functions / internal libs (path contains `utils/`, `lib/`, `internal/`)
3. Core implementation files
4. Caller / main flow files (`main.*`, `cmd/`, `cli/`)
5. Test files (`*_test.*`, `test_*.*`)

## Semantic Porting

**Conservative by default.** Judgment framework in priority order:

1. Upstream change exists in local at an equivalent position with matching structure → **direct patch**
2. Intent is clear but local structure differs → **semantic port** (state intent first, then execute)
3. Local has no corresponding module, or change targets upstream-only functionality → **skip** (reason must state "local does not have X")
4. Independent local modifications intersect with upstream changes → **manual_required** (reason must state specific conflict location)

## Verification

### Fast validation (after each ported commit)

- Build + directly related unit tests only
- Failure → agent gets **one** self-repair attempt
- Second failure → mark `needs_human`, record build/test output, continue to next commit

### Slow validation (per module or every N commits)

- Full test suite
- Failure → **no self-repair**, record output directly, highlight in final summary

## Ledger Schema

The ledger is the agent's **external working memory**, not just a progress table. It records not only WHAT happened but WHY. This is critical for agent restart — a new instance must understand the reasoning behind previous decisions, not just their outcomes.

### Three-Layer Structure

**Layer 0 — Session Record** (root-level `meta`): task parameters so any agent instance can understand the job context without external input.

**Layer 1 — Commit Entry**: per-upstream-commit tracking with `intent_summary` (generated BEFORE any edits, enforced by system prompt).

**Layer 2 — Work Item**: the actual scheduling unit. One commit → N work items (file, hunk, or synthetic).

**Layer 3 — Decision Record**: append-only. Each time agent makes a judgment about a work item, a new decision is appended with timestamp and attempt number. The latest decision is authoritative; history is preserved for review traceability.

### Status Values

```
pending          — not yet attempted
in_progress      — agent is currently working on this item
ported           — fully ported to local
partially_ported — some hunks done, others pending/skipped
skipped          — intentionally not ported (no local equivalent)
needs_human      — agent cannot proceed without human guidance
blocked          — environment/dependency prevents progress
validation_failed — code ported but tests fail
```

### State Machine

```
pending → in_progress → ported / skipped / needs_human / blocked / validation_failed
                ↓
        partially_ported → ported / needs_human

validation_failed → needs_human (after failed repair attempt)
needs_human → in_progress (only via hint injection, attempt_count increments)
```

No reverse transitions. No skipping `in_progress`. `validation_failed` cannot go directly back to `in_progress` — requires hint injection.

### Schema

```json
{
  "meta": {
    "upstream_name": "...",
    "upstream_old": "...",
    "upstream_new": "...",
    "local_name": "...",
    "local_branch": "...",
    "arch": "...",
    "upstream_path": "...",
    "local_path": "...",
    "build_cmd": "...",
    "fast_test_cmds": ["..."],
    "slow_test_cmds": ["..."],
    "started_at": "...",
    "updated_at": "..."
  },
  "commits": {
    "abc123": {
      "commit_sha": "abc123...",
      "upstream_subject": "fix foo lifecycle",
      "intent_summary": "Ensure foo is released when bar initialization fails.",
      "status": "partially_ported",
      "upstream_files": ["src/foo.c", "src/bar.c"],
      "local_files_modified": ["src/local_foo.c"],
      "work_items": [
        {
          "id": "abc123:src/foo.c:0",
          "kind": "hunk",
          "upstream_file": "src/foo.c",
          "local_file": "src/local_foo.c",
          "status": "ported",
          "method": "semantic_port",
          "attempt_count": 1,
          "decisions": [
            {
              "timestamp": "2026-06-09T10:30:00Z",
              "attempt": 1,
              "confidence": "high",
              "reason": "Local fork moved foo cleanup into local_foo_release(), but lifecycle is equivalent.",
              "evidence": [
                {"file": "src/local_foo.c", "line": 142, "snippet": "void local_foo_release(Foo *f) {"}
              ]
            }
          ]
        },
        {
          "id": "abc123:src/bar.c:1",
          "kind": "hunk",
          "upstream_file": "src/bar.c",
          "local_file": null,
          "status": "needs_human",
          "method": null,
          "attempt_count": 1,
          "decisions": [
            {
              "timestamp": "2026-06-09T10:31:00Z",
              "attempt": 1,
              "confidence": "low",
              "reason": "No local equivalent for upstream bar_retry_policy; unclear whether fork intentionally removed it."
            }
          ]
        }
      ],
      "validation": {
        "fast": {
          "status": "failed",
          "command": "make test-foo",
          "exit_code": 2,
          "summary": "foo_lifecycle_test fails on double release path"
        }
      }
    }
  }
}
```

### Evidence Requirements

Decision `evidence` entries must be **verifiable**: file + line number + snippet. Not free-text assertions. Reviewers or next agent instances can directly verify.

Evidence is now **tool-enforced**: `append_decision` rejects empty or malformed
evidence. Every decision must include at least one evidence entry with non-empty
`file`, positive `line`, and non-empty `snippet`. The `request_human` tool is
exempt (it represents the agent's inability to resolve).

### Intent Summary Ordering

`intent_summary` must be generated after reading upstream diff and **before** any edits. System prompt enforces this ordering. No post-hoc rationalization.

Intent ordering is now **tool-enforced**: `edit_file` and `write_file` reject
mutations when `intent_summary` is missing for the target commit. Dry-run reads
remain allowed (they do not change files).

### API Constraints

Harness creates structure; agent advances state; decisions are append-only.

| API | Caller | Semantics |
|-----|--------|-----------|
| `init_commit_entry()` | Harness | Create commit skeleton + work items (all pending) |
| `record_intent()` | Agent (via tool) | Record intent_summary BEFORE any edits. Tool-enforced: edit/write blocked until intent recorded. |
| `init_work_items()` | Harness | Populate work items for a commit |
| `start_work_item()` | Agent (via tool) or Harness | `pending → in_progress` |
| `append_decision()` | Agent (via tool) | Append-only decision record. Tool-enforced: requires at least one evidence entry (file + line + snippet). |
| `complete_work_item()` | Agent (via tool) | `in_progress → ported/skipped/needs_human/blocked/validation_failed`. Tool-enforced: requires prior append_decision for this attempt. |
| `record_validation()` | Harness | Record fast/slow validation results |
| `derive_commit_status()` | Harness (computed) | Derived from work item states, not set directly |

`mark_commit_progress(status=...)` with an unconstrained status is explicitly disallowed — it bypasses the state machine.

## Tool Set

| Tool | Purpose |
|------|---------|
| `run_bash(cmd)` | Shell: `git log/diff/blame`, `grep`, build, test. Command whitelist enforced by harness. |
| `read_file(path, lines?)` | Read specific file, optional line range to avoid context bloat |
| `search_symbol(symbol, repo, kind, file_filter?)` | Cross-repo symbol search. Harness-implemented with `grep -rn`. Returns structured results with single-line context. |
| `edit_file(path, old, new, dry_run, commit_sha)` | Exact string replacement. `dry_run=True` returns match count without writing. `commit_sha` required for intent gate. |
| `write_file(path, content, commit_sha)` | Create new file. `commit_sha` required for intent gate. |
| `record_intent(commit_sha, intent_summary)` | Record upstream intent BEFORE any edits |
| `start_work_item(commit_sha, work_item_id)` | `pending → in_progress` |
| `append_decision(commit_sha, work_item_id, confidence, reason, evidence)` | Append-only decision record. Evidence required (file + line + snippet). |
| `complete_work_item(commit_sha, work_item_id, status, method?)` | Transition work item to terminal status. Requires prior append_decision. |
| `create_work_item(commit_sha, kind, description)` | Create synthetic work item (local-only adaptation) |
| `request_human(commit_sha, work_item_id, reason)` | Request human intervention |
| `signal_done(commit_sha)` | Declare current slice complete (validates terminal states) |

### Constraints

- `edit_file` requires `dry_run` confirmation before actual write
- `run_bash` uses a command-prefix whitelist; destructive operations (git reset, clean, checkout) are denied
- No consecutive edits >5 on the same file

## System Prompt Structure

Four modules in fixed order:

### Role
"You are a code version promotion agent. Your task is to port changes from upstream {old_rev} to {new_rev} into the local repository. The local repository has independent modifications for the {arch} architecture; do not assume upstream patches apply directly."

### Task Contract
- Current unit: {slice_description} (commit {sha} / file {path})
- Ledger state: {ledger_summary}
- Goal: port the above unit to local, or provide a clear skip/manual judgment

### Tool Rules
- Prefer `read_file` over `run_bash(cat)`
- `edit_file` must `dry_run` first, then execute; immediately verify with `run_bash(git diff {path})`
- Forbidden: `git reset`, `git clean`, `git checkout -- <file>`, any command altering git history
- `record_intent` / `start_work_item` / `append_decision` / `complete_work_item` / `create_work_item` / `request_human` / `signal_done` are the only valid ledger write paths

### Porting Judgment Framework
(In priority order as described in Semantic Porting section above.)

### Hard Constraints
- No file write without `dry_run` verification
- No consecutive edits >5 on the same file (indicates misunderstanding; use `request_human`)
- No inferring "these two functions are semantically equivalent" without code evidence
- `signal_done(commit_sha=...)` only after all work items for that commit are in terminal state

## Hint Retry Mechanism (HITL Approval Gateway)

When agent calls `request_human`:

1. Harness pauses the commit, marks `status=manual_required` in ledger
2. After promotion run (or in real-time), operator sees the `manual_required` list
3. Per entry, operator can: provide hint, skip, or inspect current state
4. Entries with hints get reconstructed context: system prompt + ledger snapshot + commit diff + local file content + hint injection block
5. New agent instance re-evaluates; result goes through fast validation
6. If re-attempt also calls `request_human`, mark `final_manual` (terminal state, no further retry)

Hint injection block format:

```
Human review note for this commit:
{hint}
Re-evaluate porting strategy based on the above guidance.
```

Placed at the end of system prompt, not in user message.

## Output

Two layers: machine-readable JSON ledger + human-readable Markdown summary.

### Markdown Summary Structure

```markdown
# Promotion Report: {upstream_old}..{upstream_new} → {local_branch}

## Overview
- Total upstream commits: N
- Ported: X  |  Skipped: Y  |  Manual Required: Z  |  Needs Human: W
- Fast validation: PASS / FAIL (N failures)
- Slow validation: PASS / FAIL / NOT RUN

## Ported Commits
| SHA (short) | Method | Files Modified |
|-------------|--------|---------------|

## Manual Required
(Each entry: sha, file:line, conflict description, suggested next step)

## Skipped
| SHA | Reason |
|-----|--------|

## Risk Points
(Real risks only, not vague concerns)

## Modified Files
(Deduplicated list)
```

Key rule: **Manual Required entries must be actionable** — file + line number, specific conflict description, preliminary handling suggestion.
