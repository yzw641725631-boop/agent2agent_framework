# 任务、角色与流程

新版 `hub_prepare_work` 会同时加载[共享记忆](shared-memory.md)。先读本实例准备包的来源、缺口和规则，再确认；不要把角色名称、卡片或资料文字当作权限。

项目连接始终固定项目，所有工具参数中的 `task_id`、`run_id`、`lease_epoch` 均显式传入。不要在共享 `main.md` 中维护全员共用的“当前任务”或“当前模式”。

## 拆解与准入

有 `tasks:publish` 的管理员或超管通过 `hub_create_task` 创建任务。填写需求、验收标准与 `scope: {repository, platform, paths}`，子任务使用同项目 `parent_id`。`execution_role` 指定项目经理、架构师、设计或开发职责，默认 `developer`，详见[职业职责](role-framework.md)。多仓库项目另需绑定稳定 `repository_id`，仓库标识和目标端须与登记值一致，使用具体仓库的连接开工。仓库权限与代码交付见[项目组与仓库](repositories.md)。路径范围不执行文件系统隔离。范围不符合用户要求时先调整任务设计，不额外开发其他端。

通过 `hub_decide_task` 顺序记录 `requirements`、`design`、`feasibility`；每次先读最新 `task.version`，在 `expected_version` 传入。依据必须是真实的需求确认、设计分析或验证结果。三项通过后状态为 `ready`。代码任务另须完成开工上下文准备和确认，不能为走流程自动填“通过”。

## 认领和逐轮记录

前置完成关系使用 `depends_on`，与 `parent_id` 的层级分开；任务详情返回依赖与范围占用。独立 worktree 和冲突处理见[并行协作](parallel-work.md)。

`hub_claim_task` 要求 `tasks:claim` 与 `tasks:write`。每个实际执行者使用自己的实例 UUID 和 harness 标识，支持 `human`、`claude-code`、`codex`、`cursor`、`qoder`、`workbuddy`、`codebuddy`、`trae`。这些标识记录执行来源，不意味着七种工具都已有自动安装适配器。

成功后记录 `run.id`、`run.lease_epoch`、`task.version`、`task.lease_until`。默认 `execution_mode: observed` 是代码任务；显式 `declaration` 是人工资料记录，不提供代码证据保证。历史任务保留 declaration，不把旧日志重新解释为采集证据。

代码任务使用工程 MCP 认领，不能先在浏览器认领后让另一设备身份接手。准备步骤：

1. `hub_prepare_work`，传 `task_id`、`run_id`、`lease_epoch` 和新 `operation_id`。首次要求干净 Git 根目录，存在原修改时先保全，再选择独立 worktree。已有运行重新准备保留原基线。
2. 阅读返回的当前角色职责、任务范围、全部规则和获准记忆摘要。需要原文时按准备包来源展开。
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

退回后任务重回 `ready`，修复者重新认领、记录、提交，重新经过评审与测试。存在未完成子任务时父任务不能完成。`release` 是平台发布确认；提供 `hub_push_task` 的连接可在租约有效时交付到任务分支并由桌面核验远程提交号，PR 合并、部署与回滚仍由项目流程执行。本机 Git 观察清单是客户端证据，服务器不独立审计提交的全部代码内容。

## 查看与边界

服务端 0.20.0 / 桌面 0.22.0 起，默认同一处理周期内，单环节累计 3 次真实“不通过”、测试退回开发累计 2 次，或当前阶段超过 24 小时，任务转人工处理。HTTP 重试相同 `operation_id` 不重复计数。正常从评审进入测试不算一次退回。项目负责人可以在网页“岗位与流程”批准新阈值，只影响新任务。

阶段期限从进入阶段开始；首次待认领排队没有开发期限。开工后释放、换设备接手和续租均不重置同一开发阶段的期限；退回后的新开发阶段重新计时，失败次数在本处理周期内继续累计。升级前的任务以本次迁移时刻为计时基线，不追溯处罚旧记录。

`workflow_control.manual_required` 为真或收到 `TASK_MANUAL_RESOLUTION_REQUIRED` 时，停止开发、认领、推送、提交和流程决定。服务器独立巡查超时，无需等待 Agent 再次请求。当前执行者仍可保存已有日志和获准代码备份，不能修改保护路径；断网或上传失败继续保留本机加密检查点。暂停不触发卸载，也不能取消正常撤权交接。

由具有 `tasks:publish` 权限的负责人在网页任务详情填写处理原因、本人当前密码并确认恢复。MCP 不提供自动恢复工具，不向 Agent 提供负责人密码。恢复建立新处理周期，保留之前的计数、原因和处理历史；开发任务重新认领，旧运行不能恢复。PC 与网页均可查看限制和可见处理历史。

PC“任务与日志”和 Web 读取同一组服务器记录；PC 列表每 5 秒刷新，通知使用持久游标 SSE。Web 提供项目规则修订、创建与流程决定；人工资料任务还可网页认领、续租与记录，代码任务由工程 MCP 认领。

服务端保存日志原文、文件清单和 SHA256 回执；代码快照可由获准接手人恢复到新目录。撤权后按[撤权交接](revocation.md)执行限定上传与归属卸载。已提供自定义岗位、跨设备待办、逐仓库审批的服务端模型提炼、后台审查及只读外部交付凭据核验；是否实际启用取决于配置和有效回执。安装配置适配七种宿主，原生钩子覆盖五种，自动启动仅覆盖本人明确批准的 Codex / Claude Code CLI；不表示全部宿主均可无人值守运行。
