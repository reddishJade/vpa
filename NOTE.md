# Version Promotion Agent 设计笔记

目标：把上游版本区间内的变更，按可验证、可恢复、可审计的方式移植到本地分叉代码库。

核心原则：

- **Ledger 驱动推进**：ledger 是权威进度源，记录 upstream commit/file/hunk 到本地修改的 N:M 映射。
- **Git 校验完成性**：Git 状态不是进度源，但每次标记完成前都要用 `git diff`、build/test 结果校验 ledger 声明是否可信。
- **默认保守语义移植**：不确定时显式标记人工确认，并记录简短理由。
- **阶段性压缩上下文**：长任务不能依赖单个无限上下文，必须可总结、重启、继续。

## 1. Agent 架构

采用 **单 Agent 循环为主 + 关键节点重启**。

版本推进天然有顺序依赖：commit B 可能依赖 commit A 引入的新函数、类型或行为变化。因此默认执行模型应是一个 agent 顺序处理 upstream commit 范围，而不是从一开始拆成多个并行 agent。

上下文膨胀通过重启解决。重启触发条件：

- 已处理 commit 数达到配置阈值，例如 `max_commits_per_session`
- 估算上下文利用率超过 60% 到 70%
- 完成一个大 commit 或一个模块级批次后
- agent 连续遇到多次语义不确定，需要压缩已有判断

重启时只传递：

- ledger 当前 JSON
- 当前待处理 commit 的 diff 和必要本地文件上下文
- 固定任务说明 prompt
- 最近一次验证结果和未解决风险

其他对话历史应丢弃，避免后期漂移。

## 2. 进度源

采用 **Ledger 驱动，Git 做校验层**。

Ledger 负责表达 Git 难以表达的映射：

- 一个 upstream commit 拆成多个本地 edit
- 多个 upstream commit 合并成一个本地 edit
- 一个 upstream hunk 被跳过、语义移植或标记人工确认

Git 校验规则：

- agent 标记 `ported` 前必须检查 `git diff HEAD`
- ledger 中 `local_files_modified` 必须能在 git diff 中找到对应改动，除非该 commit 被明确标记为 `skipped`
- 如果 ledger 声称已 ported 但 git 没有相关改动，记录 warning，commit 不能进入最终完成态
- 每次验证命令的退出码和摘要都写入 ledger

## 3. 切片粒度

默认 **Commit 粒度**，大 commit 自动降级到 **File 粒度**。

Commit 粒度优先，因为它保留 upstream 的原始意图和依赖关系。降级阈值建议：

- diff 超过 300 行
- 或触及超过 8 个文件
- 或包含生成文件、大型格式化变更、跨模块重构

File 粒度 fallback 的处理顺序不能随机，应尽量按依赖顺序：

1. 收集大 commit 修改的文件列表
2. 粗略解析 import/include/引用关系
3. 被依赖文件优先处理
4. 调用方、测试文件、文档最后处理

如果无法可靠建立拓扑顺序，则按“核心库代码 -> 适配层 -> 调用点 -> 测试”的稳定规则处理，并在 ledger 中记录排序依据。

Hunk 粒度只作为 file 内部状态记录，不作为默认调度单元。它太细，会增加管理成本并丢失 commit 意图。

## 4. 语义移植策略

默认策略：**保守**。

处理一个 upstream diff 时，agent 先判断落地方式：

- `direct_patch`：本地结构与 upstream 足够接近，可直接套用或等价编辑
- `semantic_port`：结构不同，但 upstream 意图能明确翻译到本地结构
- `skipped`：本地没有对应模块，或该变更对本地分叉不适用
- `manual_required`：存在语义不确定、冲突或高风险猜测

保守不是停止推进，而是让不确定性可见。每个 `manual_required` 必须包含简短理由，例如：

- 本地函数签名与 upstream 差异较大，无法确认参数语义
- upstream 新增模块在本地不存在，无法判断是否需要创建
- 本地同一区域已有独立改动，可能与 upstream 行为冲突
- 测试失败但错误与当前 commit 的关联不明确

agent 可以在用户提供 hint 后重试 `manual_required` 项，但不应在无新信息时反复猜测。

## 5. 验证策略

“一批”定义为：一个 upstream commit 进入可判定状态后。

即使大 commit 内部按 file 粒度处理，也应在整个 commit 完成 port/skipped/partial 判定后执行验证。

验证分两层：

- **快速验证**：每个 commit 后运行。包括 build、lint 或直接相关单测。目标耗时为几秒到几十秒。
- **慢速验证**：每 N 个 commit、每个模块完成后或 session 重启前运行。包括完整测试套件、集成测试或目标平台 smoke test。

失败处理：

1. 快速验证第一次失败时，agent 可以基于错误输出自修一次。
2. 第二次仍失败，该 commit 标记为 `needs_human` 或 `partial`。
3. ledger 记录失败命令、退出码、关键错误摘要和已尝试修复。
4. agent 继续推进下一个 commit，除非失败导致工作区无法构建到继续分析的程度。

不要允许 agent 在单个 commit 上无限循环。

## 6. Ledger Schema

Partial commit 使用子条目，不拆成多条顶层 commit 记录。

顶层条目以 upstream commit 为单位，子条目记录 file/hunk 级状态。示例：

```json
{
  "commit_sha": "abc123",
  "upstream_subject": "refactor foo handling",
  "status": "partial",
  "porting_method": "semantic_port",
  "files_touched": ["src/foo.c", "src/bar.c"],
  "local_files_modified": ["src/foo.c"],
  "hunks": [
    {
      "file": "src/foo.c",
      "hunk_id": "0",
      "status": "ported",
      "porting_method": "semantic_port",
      "reason": ""
    },
    {
      "file": "src/foo.c",
      "hunk_id": "1",
      "status": "manual_required",
      "porting_method": "manual",
      "reason": "本地 foo_local() 与 upstream new_foo() 签名差异较大，不确定参数语义"
    },
    {
      "file": "src/bar.c",
      "hunk_id": "0",
      "status": "skipped",
      "porting_method": "manual",
      "reason": "bar.c 本地无对应模块"
    }
  ],
  "validation": {
    "fast": {
      "status": "not_run",
      "command": "",
      "exit_code": null,
      "summary": ""
    },
    "slow": {
      "status": "not_run",
      "command": "",
      "exit_code": null,
      "summary": ""
    }
  },
  "warnings": [],
  "notes": ""
}
```

Commit 级 `status` 聚合规则：

- 全部 hunk 为 `ported` -> `ported`
- 全部 hunk 为 `skipped` -> `skipped`
- 任意 hunk 为 `manual_required` 或验证失败 -> `partial` 或 `needs_human`
- 存在未处理 hunk -> `in_progress`

推荐状态集合：

- `pending`
- `in_progress`
- `ported`
- `partial`
- `skipped`
- `needs_human`
- `blocked`

推荐移植方式：

- `direct_patch`
- `semantic_port`
- `manual`
- `not_applicable`

推荐验证状态：

- `passed`
- `failed`
- `not_run`
- `not_applicable`

## MVP 流程

1. 用户提供 upstream repo/path、local repo/path、old revision、new revision、本地分支和测试命令。
2. agent 初始化 ledger：读取 `git log old..new`、`git diff --name-status old..new`，生成 pending commit 列表。
3. agent 按 commit 顺序推进。
4. 小 commit 直接按 commit 粒度处理。
5. 大 commit 降级为 file 粒度，并按依赖顺序处理。
6. 每个 commit 完成后运行快速验证。
7. 每 N 个 commit 或 session 结束前运行慢速验证。
8. 触发上下文阈值后写入压缩总结，重启 agent 继续。
9. 最终输出已移植范围、修改文件、跳过项、人工确认项、测试结果和风险点。

## 待定实现细节

- token 使用率如何估算：由 runner 统计消息 token，或用近似字符数兜底。
- 依赖拓扑如何实现：第一版可用 import/include/grep 规则，后续再接语言专用 parser。
- fast validation 命令如何自动选择：第一版由用户配置，后续按改动文件映射到测试。
- ledger 存储格式：第一版用 JSON，后续可考虑 JSONL 便于增量写入和恢复。
