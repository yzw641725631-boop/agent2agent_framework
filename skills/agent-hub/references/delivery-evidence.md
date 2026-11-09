# 外部交付核验

连接提供本页工具时使用。旧版缺少工具须说明缺口；推送、任务完成、合并、部署与服务健康各有不同依据。

## 仓库要求

用 `hub_get_delivery_policy(repository_id)` 查阅当前规则。发布负责人可用 `hub_set_delivery_policy` 设置必需环节、精确工作流文件、环境、有效期及原因。默认没有必需外部环节；不要自行降低项目已有要求。

- 要求 `ci` 时，所有 `workflows` 都必须通过；路径如 `.github/workflows/test.yml`。
- 要求 `deployment` 或 `rollback` 时，所有 `environments` 都必须有成功结果。
- `ci_target`、`deployment_target` 指定核验推送提交 `delivered`，或合并结果 `merged`。后者同时要求 `merge`，适用于 squash 等导致提交号改变的合并。
- 同次发布要求 `deployment` 或 `rollback` 之一。规则修订使旧版凭据失效。

## 读取提供方结果

1. 先完成实际代码提交、检查点日志和 `hub_push_task`，核对服务器 Git 交付回执。
2. 从真实 GitHub 操作取得 PR 编号、Actions 运行 ID 或 Deployment ID。工具本身不创建、合并或部署，也不接受任意 URL、命令和凭证。
3. 调用 `hub_verify_delivery_evidence`，传入 `task_id`、`worklog_id`、当前 `expected_task_version`、`kind`、数字 `provider_id` 和稳定的 `operation_id`。重试同次核验保持原参数；重新观察用新操作 ID。
4. 合并后先核验 `kind: merge`；后续 CI 或部署传入其 `merge_evidence_id`。回执同时保留原工作提交和实际核验的结果提交，不猜测两者等价。
5. 回滚还须填写 `rollback_of_id`（被替换的成功部署）和 `rollback_to_id`（更早的成功恢复目标）。三者必须属于同仓库、同环境，恢复目标的确切提交与本次回滚一致；不能用“部署了另一个提交”替代回滚证据。
6. 用 `hub_list_delivery_evidence(task_id)` 查看 `release.checks`、`fresh`、`observed_at` 和当前规则版本。不同工作流、环境逐项满足后才可发布。旧成功不能覆盖更新的失败；确认过的重试返回原回执，不刷新其有效期。

核验发生在服务器，通过登记仓库的凭证只读访问固定 GitHub API。只核验该提交的最新工作流运行、该环境的最新部署；草稿 PR、未结束测试、排队/失败/停用部署均不算通过。仓库、权限、任务版本或规则中途变化时拒绝保存，刷新后重新操作。

PC 与网页：在任务的“交付核验”查看记录；网页“仓库与资料 → 交付要求”配置规则。`provider_reports_only` 表示提供方报告，部署的 `target_health_verified: false` 明确尚未独立检查线上健康。最后一次观察无法保证之后没有新运行；发布前按项目要求刷新有效回执。

协议依据：[GitHub PR API](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request)、[Workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run)、[Deployment statuses API](https://docs.github.com/en/rest/deployments/statuses#list-deployment-statuses)。历史记录应保留本系统的不可变回执，不能依赖提供方永久保存全部历史状态。
