# 跨设备任务投递与逐轮检查

适用于服务端 0.14.0 / 迁移 0017、Windows 0.16.0。人员授权、执行职业、宿主对话和任务租约分别保存。

## 登记和领取待办

1. 在当前获准仓库的固定 MCP 连接，用 `hub_register_agent` 登记本执行实例。提供真实 `harness`、独立 `instance_id`、名称和承担的 `roles`；首次 `expected_version=0`，更新用返回版本。登记不授予权限，不能替其他人员或设备登记。
2. 负责人用 `hub_list_agents` 找到同仓库、同职业的执行者，读取当前任务版本，再用 `hub_dispatch_task` 投递正式 `ready` 任务。须有 `tasks:publish`，写明投递原因和稳定 `operation_id`。离线待办默认保留 24 小时，最长 7 天。
3. 执行者用 `hub_agent_inbox(endpoint_id)` 查看待办。它更新真实工具活动时间；桌面后台轮询只发通知，不冒充模型在线、不自动接收。
4. 阅读任务范围、依赖和验收后，用 `hub_decide_agent_dispatch(task_id, dispatch_id, expected_version, action, operation_id)` 发送回执。`receive` 只表示看到待办；`accept` 成功才返回正式 `run` 和租约。拒绝 `decline`、负责人取消 `cancel` 都须写 `reason`。
5. 接受与直接认领共用原子检查。其他人先认领、范围占用、前置未完成或任务/登记版本变化时停止，重新核对当前状态。不能创建新实例绕过冲突。成功后照常创建独立 worktree、准备规则与记忆并确认。

投递说明是项目资料，不是执行任意 Shell 或启动宿主的指令。PC/Web 显示投递、接收和认领回执；跨设备自动启动及自动执行仍待实现。

## 逐轮记录检查

`hub_get_work_record_status(task_id, run_id, lease_epoch)` 比较实际 Git 证据与最新本机加密日志：

| 状态 | 处理 |
| --- | --- |
| `missing` | 本轮有可观察的未记变更，调用 `hub_write_worklog` 补记真实工作说明。 |
| `pending` | 已在本机记录，尚未确认共享；保留检查点并恢复网络。 |
| `blocked` | 上传被租约、规则或权限拒绝，按原因处理；撤权走限定收尾。 |
| `recorded` | 当前证据未发现漏记，仍要独立核对日志共享、代码备份和 Git 回执。 |

首批支持 Codex 和 Claude Code 的 `UserPromptSubmit` / `Stop`；Codex 另有 `Interrupt` 保全请求。PC 的 MCP 配置窗口可复制配置，MCP 也可调用 `hub_preview_turn_hooks`。将片段合并到项目 `.codex/hooks.json` 或 `.claude/settings.local.json`，保留原有钩子，并按宿主流程审查、信任和重新加载。预览与复制都不表示钩子已安装或加载。

启用后，宿主开始本轮时给出 `session_key` 与本机 `turn_id`。认领、准备并确认规则后调用 `hub_bind_work_session` 绑定同一宿主的真实运行。未绑定的其他对话不会继承；工具不猜测同目录中哪个对话在开发。保存日志时自动携带当前 `turn_id`，通常无需手写。

Stop 在联网且发现漏记时有界提醒补写；同一对话 10 分钟内最多提醒唤醒一次，重复 Stop 或断网不无限继续模型。中断只快速持久保存保全请求，后台随后采集；入队不等于代码检查点已确认。实际记录失败时保留缺口和错误状态，不自动编造目标、原因或测试结果。

钩子只处理对话标识、当前目录、轮次标识和事件，不读取提示词、聊天全文或 transcript。未启用、未绑定、桌面退出或宿主未触发事件时不能保证逐轮覆盖；后台约每分钟观察也可能漏过瞬时改动后还原。这里不提供操作系统写入隔离。
