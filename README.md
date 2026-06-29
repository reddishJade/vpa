# VPA

VPA is an architecture-port promotion tool. It keeps an unsupported target ISA
in sync with upstream changes by cherry-picking commits and deferring
architecture-specific porting to human review.

Motivating path: `box64 RISC-V/rv64 → SW64 local port`.

See [DESIGN.md](DESIGN.md) for architecture, [LEDGER.md](LEDGER.md) for ledger
schema.

## Quick Start

```bash
# Plan (dry-run) a range of upstream commits
uv run vpa promote \
  --config vpa.toml \
  --rev-range upstream/main~10..upstream/main \
  --dry-run

# Execute cherry-pick for eligible commits
uv run vpa promote \
  --config vpa.toml \
  --rev-range origin/main..upstream/main \
  --execute \
  --ledger-path ledger.jsonl
```

## Commands

### `vpa promote`

Plan or execute promotion of an upstream commit range.

| Argument | Description |
|---|---|
| `--config <path>` | TOML config file (default: `vpa.toml`) |
| `--upstream-repo <path>` | Upstream Git repository |
| `--local-repo <path>` | Local target Git repository |
| `--rev-range <range>` | Git revision range (e.g. `HEAD~10..HEAD`) |
| `--target-isa-path <path>` | Target ISA path in local repo |
| `--reference-isa-path <path>` | Primary reference ISA path in upstream |
| `--fallback-reference-isa-path <path>` | Fallback reference ISA path (repeatable) |
| `--build-cmd <cmd>` | Build command for validation |
| `--smoke-test <cmd>` | Smoke test command (repeatable) |
| `--ledger-path <path>` | Output path for ledger JSONL file |
| `--report-path <path>` | Output path for plan report |
| `--dry-run` | Plan without mutating repos |
| `--execute` | Run mechanical Git operations |
| `--risk-preference <level>` | `conservative`, `balanced`, or `aggressive` |
| `--llm-temperature <float>` | LLM temperature for semantic porting |
| `--llm-max-context-chars <int>` | Max prompt characters before truncation |

Without `--execute`, `vpa promote` runs in plan-only mode: it classifies each
commit, runs change analysis, and prints the gate decision without touching
any repository.

With `--execute`, VPA cherry-picks each eligible upstream commit onto the local
repo. Conflicts are classified and either auto-resolved or recorded as pending
human review in the ledger.

### `vpa inspect`

Read a ledger JSONL and print a human-readable report.

```bash
# Print to terminal
uv run vpa inspect --ledger-path ledger.jsonl

# Write to file (useful when output is large)
uv run vpa inspect --ledger-path ledger.jsonl -o report.txt
```

| Argument | Description |
|---|---|
| `--ledger-path <path>` | Path to ledger JSONL file (required) |
| `--output, -o <path>` | Write report to file instead of stdout |

The report lists:
- Summary stats (total commits, committed, rolled back)
- All pending human-review files with commit SHA and subject, grouped by
  category (`ISA_BACKEND` / `SOURCE`)
- Sample committed and rolled-back commits

## Configuration

VPA reads `vpa.toml` from the current directory. CLI flags override
corresponding config values.

```toml
[promotion]
upstream_repo = "/path/to/upstream"
local_repo = "/path/to/local"

[isa]
reference_isa_path = "src/dynarec/rv64"
target_isa_path = "src/dynarec/sw64_core3"
fallback_reference_isa_paths = ["src/dynarec/arm64", "src/dynarec/la64"]

[validation]
build_command = "cmake --build build"
smoke_commands = ["ctest --test-dir build"]

[output]
ledger_path = "ledger.jsonl"
report_path = "report.md"

[llm]
model = "your-model"
base_url = "https://your-provider/v1"
api_key_env = "VPA_API_KEY"
```

## Workflow

1. **Classify** — each commit in the range is classified by which files it
   touches (reference ISA, target ISA, shared code, cross-cutting).
2. **Analyze** — diff content is analyzed for change kind (logic, API shape,
   comment-only, etc.).
3. **Gate** — a gate decision routes the commit: `needs_validation_only`,
   `needs_semantic_port`, or `no_target_change`.
4. **Execute** — eligible commits are cherry-picked. Conflicts are resolved
   per strategy:
   - `non_source`: auto `checkout --theirs`
   - `isa_backend` / `source`: recorded as `pending_human_review` in ledger
5. **Inspect** — `vpa inspect` lists all pending files with their source
   commit for manual resolution.
