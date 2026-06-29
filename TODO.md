# VPA TODO

## Next

- [ ] **断点恢复** — execute() 前检查 ledger 中已有 SHA，跳过已处理的 commit，
      避免重跑时重复 cherry-pick 已提交的 commit
- [ ] **`box64-2-sw64/` 加入 `.gitignore`** — 防止 `git add -A` 意外捡入

## Later

- [ ] **冲突自动修复路径** (v2) — 调 LLM 自动解决 `isa_backend` / `source` 冲突
- [ ] **验证-修复重试策略** — validation 失败后调 LLM 修一次再验证
- [ ] **精简 fixture 的集成测试** — 端到端测试真实 repo 流程

## Definition Of Done

- [ ] `uv run ruff check .`
- [ ] `uv run pyright`
- [ ] `uv run pytest`
- [ ] `git diff --check`
- [ ] Only intended files are staged
- [ ] `box64-2-sw64/` and `box64_2_sw64.tar.gz` remain untracked unless explicitly requested
