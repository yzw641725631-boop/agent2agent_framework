# 项目组、仓库与代码交付

适用于提供仓库工具的桌面端 0.8.0、服务端 0.6.0 / 迁移 0009 及后续兼容版本。旧版连接没有这些工具时先按版本检查结果处理，不用 Shell 绕过授权。框架 Skill 和业务代码继续分仓库。

## 获准项目组怎样组成

1. 使用安装连接列出获准项目组。只有超管能创建项目组；同次创建重试保留 `operation_id`。
2. 用户选择项目组本机根目录及开发工具，用 `hub_connect_project` 建立项目组连接，重新加载连接。
3. 在项目组连接调用 `hub_list_repositories`。管理员可按用户要求用 `hub_register_repository` 登记公共只读仓库或已配置 GitHub App 的仓库；专用 PAT 和 App 私钥直接填后台，不传给 Agent。
4. 超管按成员、设备审批仓库权限。管理员只能提交申请。`project:read` 不开放任何仓库；`code:write` 要同时具有 `code:read`。读资料需要仓库 `documents:read` 和项目 `documents:read`，读同事任务日志还需要相应仓库代码读取权限及 `logs:read`。
5. 调用 `hub_assemble_project`，传入明确获准的 `repository_ids`、`harness`（`codex`、`claude` 或 `cursor`）及新 `operation_id`。每个仓库拉到根目录下独立的 slug 子目录，生成固定仓库连接。不覆盖已有同名目录，不拉没有代码读取权限的资料仓库。
6. 根据结果在具体仓库目录打开工具、重新加载并完成信任确认。工具配置若已受 Git 管理，会返回 `manual-tracked-configuration`；保留原配置，从该仓库连接复制并合并本机 MCP，不把本机配置提交到业务 Git。

已有代码目录可从安装连接用 `hub_connect_project`，传入项目组 ID、`repository_id` 和目录。首次准备任务会核对 Git origin，来源须为登记地址或平台该仓库的代理地址。没有代码权限时不能认领开发。

组装重试用同一个 `operation_id`、仓库列表及开发工具。完整且未改动的中断目录可接着配置；空的受管目录可重新拉取。有用户改动或不完整内容时保留目录并停止自动恢复；不执行 reset、clean 或递归删除。已停用的子连接不会因重试自动恢复，需重新接入已有目录。

## 任务、获取更新与推送

- 新任务显式绑定 `repository_id`，`scope.repository` 用仓库 slug，`scope.platform` 与登记目标端一致，`scope.paths` 指定允许修改的范围。认领与开工流程见[任务协作](task-workflow.md)。
- `hub_fetch_repository` 只取得登记分支到 `FETCH_HEAD`，不切分支、不合并、不改工作文件。是否合并及解决冲突按项目流程处理。
- 完成本轮代码后，按任务要求创建本机 Git 提交。保存含当前提交号的最新 checkpoint 日志，确认日志共享，再调用 `hub_push_task(task_id, run_id, lease_epoch)`。工作区须干净、认领须有效；代码任务的规则确认也须仍有效。
- 推送仅允许 `refs/heads/hub/{task_id}/{run_id}` 和日志记录的那个提交。不能推主分支、他人任务分支、一次推多分支或删除分支。网络失败保留本机代码，先查询或重试核验，不猜测成功。
- `remote_verified: true` 表示桌面用远程查询核对了提交号；`merged: false` / `deployed: false` 表示评审、合并、CI 和发布还没有完成。须在 `hub_submit_task` 结束租约之前完成需要的任务推送。

服务器只发两分钟以内的平台 Git 票据，每次 HTTP 请求重新检查身份、设备、授权、仓库和写入租约。GitHub App/PAT 保留在服务器。持有其他 GitHub 凭证的人仍可能在平台外访问；本平台不会收回这些凭证或擦除私自复制的代码。

当前仅支持 GitHub HTTPS、普通 Git 对象和单分支拉取，上传请求最大 64 MiB；不自动处理 LFS、submodule、PR、合并或部署。服务器核对提交引用和日志，不独立审计 pack 内所有代码的业务合规性。未提交修改没有远端代码备份，强制隔离、长期记忆和撤权卸载仍待接入。
