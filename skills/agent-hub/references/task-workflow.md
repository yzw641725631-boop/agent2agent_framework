# 任务、角色与流程

项目连接始终固定项目，所有工具参数中的 `task_id`、`run_id`、`lease_epoch` 均显式传入。不要在共享 `main.md` 中维护全员共用的“当前任务”或“当前模式”。

## 拆解与准入

有 `tasks:publish` 的管理员或超管通过 `hub_create_task` 创建任务。填写需求、验收标准与 `scope: {repository, platform, paths}`，子任务使用同项目 `parent_id`。仓库标识与路径只声明本任务焦点，目前不产生 Git 凭证，也不执行文件系统隔离。范围不符合用户要求时先调整任务设计，不额外开发其他端。

通过 `hub_decide_task` 顺序记录 `requirements`、`design`、`feasibility`；每次先读最新 `task.version`，在 `expected_version` 传入。依据必须是真实的需求确认、设计分析或验证结果，不能为了走流程自动填“通过”。三项通过后状态为 `ready`。这只是 G4 的任务准入部分，尚不包含自动上下文加载回执。

## 认领和逐轮记录

`hub_claim_task` 要求 `tasks:claim` 与 `tasks:write`。每个实际执行者使用自己的实例 UUID 和 harness 标识，支持 `human`、`claude-code`、`codex`、`cursor`、`qoder`、`workbuddy`、`codebuddy`、`trae`。这些标识记录执行来源，不意味着七种工具都已有自动安装适配器。

成功后记录返回的 `run.id`、`run.lease_epoch`、`task.version`、`task.lease_until`。10 分钟租约期间约每 2 分钟发送心跳。每次代码变更的轮次使用 `hub_write_worklog` 记录，最多 64 KiB。停工或交接先保存当前日志，再 `hub_release_task`。发生 `TASK_LEASE_STALE` / `TASK_LEASE_EXPIRED` 时重新读任务，不能继续使用旧 epoch。

重试同一次变更请求保留相同 UUID `operation_id`，重新发起一次操作则生成新 ID。幂等回放返回原响应：原认领响应里的租约可能已经到期，不能把回放当成新一次续租。

## 提交、评审、测试和完成

开发提交使用 `hub_submit_task`，绑定当前运行中最新一条 `outcome: checkpoint` 日志。提交后结束开发租约，正式进入 `review`。

| 关口 | 要求的能力 | 通过后 |
| --- | --- | --- |
| review | tasks:review + logs:read | testing |
| test | tasks:test + logs:read | release_ready |
| release | tasks:publish + logs:read | completed |

三项都必须传当前 `submission_id` 作为 `worklog_id` 和最新 `expected_version`，附上真实评审或测试证据。评审、测试执行会留下独立的角色运行记录。作者不能批准本人的评审或测试；超管单人开发可以明确填写至少 10 字符的 `override_reason`，该例外保留在历史里，不能称为独立审查。

退回后任务重回 `ready`，修复者重新认领、记录、提交，重新经过评审与测试。存在未完成子任务时，父任务不能完成。`release` 是平台发布确认；实际 Git 提交、push、PR 合并、部署与回滚仍由现有项目流程执行，当前没有自动 Git 证据采集器。

## 查看与边界

PC“任务与日志”和 Web 同名页面读取同一组服务器记录；PC 的任务列表每 5 秒刷新，进度通知继续使用已有持久游标 SSE。Web 提供人工认领、手动续租、日志填写和流程决定，网页不会持续替离开的执行者续租。

服务端保存日志原文和 SHA256 回执。向量检索、增量蒸馏、自动上下文记忆加载、离线队列、代码快照恢复、角色自动调度、撤权后的专用上传及卸载仍是后续能力。
