# VPA TODO

## Done

### Post-Phase-3 Redesign

- Preprocessor conditional hook (`vpa/analysis/preprocessor.py`)
- Cherry-pick based execution (replace bulk `git merge`)
- v1.0.0 ISA conflict policy: `PendingConflictRecord`, defer to human
- Generated/vendor path filtering
- Disable LLM for semantic port and conflict resolution (v1.0.0)
- `--author` position fix in `commit_cherry_pick`
- Remove pending-file blocking in execute loop
- Unconditional `cherry-pick --abort` on rollback
- `vpa inspect` CLI subcommand for human-readable ledger report
- `LEDGER.md` documenting ledger schema

## Phase 4: Range Recovery And Operational MVP

**Goal**: persist progress, resume interrupted runs, detect worktree drift.

- [ ] Persist per-commit progress in ledger or run-state file
- [ ] Resume partially completed run from last successful checkpoint
- [ ] Detect worktree mismatch with recorded safe point
- [ ] Record enough metadata to explain and recover failed runs

## Post-MVP

- [ ] Optional AST analyzer (libclang/tree-sitter when available)
- [ ] Symbol-name mapping beyond path convention
- [ ] Fallback reference triangulation (la64, arm64)
- [ ] Validation-repair retry policy
- [ ] Conflict-resolution repair path
- [ ] Integration tests against reduced fixture

## Definition Of Done

- [ ] `uv run ruff check .`
- [ ] `uv run pyright`
- [ ] `uv run pytest`
- [ ] `git diff --check`
- [ ] Only intended files are staged
- [ ] `box64-2-sw64/` and `box64_2_sw64.tar.gz` remain untracked unless explicitly requested
