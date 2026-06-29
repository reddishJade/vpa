# VPA 演示文档

## 环境准备

```bash
# 测试目录结构
ls /tmp/vpa_test_real/
# test.toml          # 配置文件
# upstream_full/     # box64 上游完整仓库（8418 commits）
# local/             # sw64_core3 下游仓库
# ledger_upstream.jsonl  # 旧帐本

# 配置文件内容
cat /tmp/vpa_test_real/test.toml
# [promotion]
# upstream_repo = "/tmp/vpa_test_real/upstream_full"
# local_repo = "/tmp/vpa_test_real/local"
#
# [isa]
# reference_isa_path = "src/dynarec/rv64"
# target_isa_path = "src/dynarec/sw64_core3"
#
# [validation]
# verify_command = "cmake --build build && ctest --output-on-failure"
```

## 重置测试环境

```bash
# 将 local repo 还原到初始 commit（清空上次测试的 cherry-pick 结果）
git -C /tmp/vpa_test_real/local reset --hard 80a8bf0ce
# HEAD is now at 80a8bf0ce Initial commit

# 确认上游仓库状态
git -C /tmp/vpa_test_real/upstream_full rev-list --count HEAD
# 8418

# 验证两个仓库的初始 commit SHA 一致
git -C /tmp/vpa_test_real/upstream_full rev-parse 80a8bf0ce
git -C /tmp/vpa_test_real/local rev-parse 80a8bf0ce
# 80a8bf0ce1d73d1f8b3f8d1aebd60106a1c39351
```

## 第一次运行（10 个真实 upstream commits）

```bash
# 选取范围：从第 5 到第 15 个 upstream commit
FIRST=$(git -C /tmp/vpa_test_real/upstream_full rev-list --max-parents=0 HEAD)
COMMITS=($(git -C /tmp/vpa_test_real/upstream_full rev-list --reverse "$FIRST..HEAD"))
uv run vpa promote \
  --config /tmp/vpa_test_real/test.toml \
  --rev-range "${COMMITS[4]}..${COMMITS[14]}" \
  --execute \
  --ledger-path /tmp/vpa_test_real/demo_ledger.jsonl
```

**执行结果（摘自输出）：**

```
VPA promotion plan

- 311842a43aa3 Read elf header of launched executable
  classification: unknown
  change: mixed
  gate: needs_semantic_port
- a8637ca5d63d Added main elf to context
  classification: shared_code
  change: logic_change
  gate: needs_validation_only
- d7f8625e63fe Future custommem helper init'd and fini'd
  classification: shared_code
  change: unknown
  gate: needs_validation_only
- 4079491d5e00 Main elf memory allocated
  classification: shared_code
  change: logic_change
  gate: needs_validation_only
- a2a78a4edc2e Load elf in memory
  classification: shared_code
  change: logic_change
  gate: needs_validation_only
- 542a2a0775e5 Detecting tcmalloc now
  classification: shared_code
  change: logic_change
  gate: needs_validation_only
- 26201d7e7057 More elf loader and parsing and stack preparing
  classification: unknown
  change: mixed
  gate: needs_semantic_port
- be92787329b5 Preparing auxval handling
  classification: unknown
  change: mixed
  gate: needs_semantic_port
- 997b5c6b50b9 Added some x86_64 regs and emu infrastructure
  classification: unknown
  change: mixed
  gate: needs_semantic_port
- 9ae5d6121295 Initializing x64emu structure
  classification: shared_code
  change: logic_change
  gate: needs_validation_only

VPA promotion execution

- 311842a43aa3 Read elf header of launched executable
  method: cherry_pick
  apply: rolled_back
  validation: not_run
- a8637ca5d63d Added main elf to context
  method: cherry_pick
  apply: rolled_back
  ...（全部 10 个 commit 因冲突 rolled_back）
```

## 查看帐本

```bash
# 查看完整报告
uv run vpa inspect --ledger-path /tmp/vpa_test_real/demo_ledger.jsonl

# 输出概要：
#   10 commits: 0 committed, 10 rolled_back, 0 skipped
#   22 pending human-review files（均为 SOURCE 类冲突）
#   涉及文件：src/include/box64context.h, src/main.c,
#             src/box64context.c, src/elfs/elfloader.c,
#             src/include/elfloader.h, src/include/x64emu.h
```

## 断点恢复测试

```bash
# 第二次运行同一范围——所有 commit 已存在帐本中，自动跳过
uv run vpa promote \
  --config /tmp/vpa_test_real/test.toml \
  --rev-range "${COMMITS[4]}..${COMMITS[14]}" \
  --execute \
  --ledger-path /tmp/vpa_test_real/demo_ledger.jsonl

# 输出：全部 10 个显示 "method: skip, apply: skipped"
# 帐本行数不变（未重复写入）
```

## 历史大数据量测试

```bash
# local 基线：SW64-0903 分支（7426 commits），已包含历史 cherry-pick
# 全范围 1121 commits（从 c674c1311 到 HEAD）
uv run vpa promote \
  --config /tmp/vpa_test_real/test.toml \
  --rev-range c674c1311..HEAD \
  --execute \
  --ledger-path /tmp/vpa_test_real/ledger_upstream.jsonl

# 结果（从真实基线重测）：
#   1121 commits 处理
#   210 committed（cherry-pick 成功，local 新增 210 commits 至 7636）
#   911 rolled_back（因冲突回滚，记录到 pending）
#   1397 pending 文件（ISA_BACKEND 337 + SOURCE 1060）
#   0 个 --author 错误
```

## 核心流程总结

```
upstream commit
     │
     ▼
  plan() ─── 分类器分析 diff
     │
     ├── generated/vendor ──→ SKIP
     │
     └── 正常 commit
             │
             ▼
         execute()
             │
             ▼
      git cherry-pick -x
             │
      ┌──────┼──────┐
      ▼      ▼      ▼
   成功   SOURCE  NON_SOURCE
          冲突    冲突
           │      │
           ▼      ▼
       记录     checkout
       pending  --theirs
       +        + 继续
       rollback
             │
             ▼
    verify_command (可选，全部完成后)
     ── cmake --build build
     ── ctest --output-on-failure
```
