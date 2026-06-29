# VPA Ledger Format

The ledger is an append-only JSONL file recording every upstream commit processed
by VPA and every pending human-review item. Each line is one JSON object.

## Line Types

### 1. Commit Execution Record

Written for every upstream commit that enters `_execute_commit()`. Key: `"commit"`.

| Field | Type | Meaning |
|---|---|---|
| `commit.sha` | string | Upstream commit SHA |
| `commit.parent_sha` | string | Parent SHA (for ordering) |
| `commit.author` | string | `"Name <email>"` |
| `commit.author_date` | string | ISO 8601 |
| `commit.subject` | string | Commit subject line |
| `classification` | enum | `shared_code`, `reference_isa_change`, `target_isa_direct`, `cross_cutting`, `generated_or_vendor`, `unknown` |
| `gate` | enum | `no_target_change`, `needs_validation_only`, `needs_semantic_port` |
| `changed_files` | string[] | Files touched by this commit |
| `method` | enum | `cherry_pick`, `semantic_port`, `skip` |
| `apply_status` | enum | `committed` — cherry-pick succeeded<br>`rolled_back` — cherry-pick conflicted or failed<br>`skipped` — gate decided not to apply |
| `apply_reason` | string? | Git stderr or skip reason |
| `integrity_status` | enum | `passed`, `failed`, `not_run` |
| `validation_status` | enum | `passed`, `failed`, `not_run` |
| `llm_used` | bool | Whether an LLM call was made |

Example:

```json
{"record": {"commit": {"sha": "fbbf74f6014b", ...}, "classification": "shared_code", "gate": "needs_validation_only", "method": "cherry_pick", "apply_status": "committed", ...}, "timestamp": "2026-06-29T02:41:02.115661+00:00"}
```

### 2. Pending Human-Review Record

Written when a file conflict is deferred to human. No `"commit"` key.

| Field | Type | Meaning |
|---|---|---|
| `commit_sha` | string | The upstream commit that caused the conflict |
| `commit_subject` | string | Its subject line |
| `file_path` | string | Conflicting file path |
| `category` | enum | `isa_backend` — file under rv64/ or with `#if RV64/SW64`<br>`source` — shared code conflict |
| `status` | string | `pending_human_review` |

Example:

```json
{"record": {"category": "isa_backend", "commit_sha": "3872d8df46bf", "file_path": "src/dynarec/dynarec_native_pass.c", "status": "pending_human_review"}, "timestamp": "2026-06-29T02:41:02.168531+00:00"}
```

## Reading the Ledger

Use `vpa inspect --ledger-path <file>` for a human-readable summary:

```
vpa inspect --ledger-path /tmp/vpa_test_real/ledger_upstream.jsonl

========================================================================
VPA Ledger Report
========================================================================

Total commits in ledger: 1121
  committed:   473
  rolled_back: 648

Pending human-review files: 1359
  ISA_BACKEND: 337
  SOURCE:      1022
...
```

The report lists all pending files with their commit SHA and subject, grouped
by category (`[ISA_BACKEND]` / `[SOURCE]`).
