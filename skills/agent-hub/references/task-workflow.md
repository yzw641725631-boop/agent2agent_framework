# 任务、角色与流程

项目连接始终固定项目，所有工具参数中的 `task_id`、`run_id`、`lease_epoch` 均显式传入。不要在共享 `main.md` 中维护全员共用的“当前任务”或“当前模式”。

## 拆解与准入

有 `tasks:publish` 的管理员或超管通过 `hub_create_task` 创建任务。填写需求、验收标准与 `scope: {repository, platform, paths}`，子任务使用同项目 `parent_id`。仓库标识与路径只声明本任务焦点，目前不产生 Git 凭证，也不执行文件系统隔离。范围不符合用户要求时先调整任务设计，不额外开发其他端。

通过 `hub_decide_task` 顺序记录 `requirements`、`design`、`feasibility`；每次先读最新 `task.version`，在 `expected_version` 传入。依据必须是真实的需求确认、设计分析或验证结果。三项通过后状态为 `ready`。代码任务另须完成开工上下文准备和确认，不能为走流程自动填“通过”。

## 认领和逐轮记录

`hub_claim_task` 要求 `tasks:claim` 与 `tasks:write`。每个实际执行者使用自己的实例 UUID 和 harness 标识，支持 `human`、`claude-code`、`codex`、`cursor`、`qoder`、`workbuddy`、`codebuddy`、`trae`。这些标识记录执行来源，不意味着七种工具都已有自动安装适配器。

成功后记录 `run.id`、`run.lease_epoch`、`task.version`、`task.lease_until`。默认 `execution_mode: observed` 是代码任务；显式 `declaration` 是人工资料记录，不提供代码证据保证。历史任务保留 declaration，不把旧日志重新解释为采集证据。

代码任务使用工程 MCP 认领，不能先在浏览器认领后让另一设备身份接手。准备步骤：

1. `hub_prepare_work`，传 `task_id`、`run_id`、`lease_epoch` 和新 `operation_id`。首次要求干净 Git 根目录，存在原修改时先保全，再选择独立 worktree。已有运行重新准备保留原基线。
2. 阅读返回的任务范围、全部规则和获准日志摘要。需要原文时用 `hub_read_worklog` 按 ID 展开。
3. `hub_acknowledge_work`，附上 `context_id`、`content_hash` 和全部 `rule_ids`。规则 revision 改变后旧确认失效；重新准备并确认。
4. 修改前 `hub_check_changes`，传拟修改的 `proposed_paths`。检查当前租约、规则、允许路径及实际变化。禁止功能的文字规则仍需执行者遵守，检查器不理解任意业务语义。

10 分钟租约下，桌面为已准备确认的运行约每 2 分钟自动心跳；10 分钟没有观察到代码或工具活动时停止续租。程序后台观察只存本机代码，不生成已测试、已完成等结论。未进入桌面托管的运行仍需显式心跳。

每轮代码变更用 `hub_write_worklog` 记录，最多 64 KiB。桌面将实际改动清单和上下文回执绑定日志；漏报、保护路径、范围外变化会被拒绝。仅服务器确认才算共享，断网进入本机加密待发。停工或交接先保存当前日志，再 `hub_release_task`。发生 `TASK_LEASE_STALE` / `TASK_LEASE_EXPIRED` 时重新读任务，不能继续使用旧 epoch。

重试同一次变更请求保留相同 UUID `operation_id`，重新发起一次操作则生成新 ID。幂等回放返回原响应：原认领响应里的租约可能已经到期，不能把回放当成新一次续租。

## 提交、评审、测试和完成

开发提交使用 `hub_submit_task`，绑定当前运行中最新一条 `outcome: checkpoint` 日志。代码任务提交前会重新采集；检查点之后有改动、规则变化、确认失效时须先记录新检查点。提交后结束开发租约，进入 `review`。

| 关口 | 要求的能力 | 通过后 |
| --- | --- | --- |
| review | tasks:review + logs:read | testing |
| test | tasks:test + logs:read | release_ready |
| release | tasks:publish + logs:read | completed |

三项都必须传当前 `submission_id` 作为 `worklog_id` 和最新 `expected_version`，附上真实评审或测试证据。评审、测试执行会留下独立的角色运行记录。作者不能批准本人的评审或测试；超管单人开发可以明确填写至少 10 字符的 `override_reason`，该例外保留在历史里，不能称为独立审查。

退回后任务重回 `ready`，修复者重新认领、记录、提交，重新经过评审与测试。存在未完成子任务时父任务不能完成。`release` 是平台发布确认；实际 Git 提交、push、PR 合并、部署与回滚仍由现有项目流程执行。本机 Git 观察清单是客户端证据，服务器没有独立核验代码内容或远程提交。

## 查看与边界

PC“任务与日志”和 Web 读取同一组服务器记录；PC 列表每 5 秒刷新，通知使用持久游标 SSE。Web 提供项目规则修订、创建与流程决定；人工资料任务还可网页认领、续租与记录，代码任务由工程 MCP 认领。

服务端保存日志原文、文件清单和 SHA256 回执。本机检查点可导出；远程代码快照、向量检索、增量蒸馏、自动长期记忆、角色调度、撤权后专用上传与卸载仍是后续能力。
