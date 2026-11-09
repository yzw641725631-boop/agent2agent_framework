# 任务依赖与独立目录

## 拆解与阻塞

创建任务可传同项目 `depends_on: [任务 UUID]`，最多 40 个直接前置任务。`parent_id` 表示层级，父任务完成须等待全部子任务。前置任务须走到正式 `completed`，进度文字“完成”不解锁依赖；完成记录也不代表代码已经合并，接手时仍须核对 Git 交付与实际基线。

`hub_get_task` 的 `coordination` 返回前置是否满足、获准读取的前置、是否含不可见依赖及同仓库范围占用。不可见前置不返回 ID、标题或数量；由获权负责人协调。项目组连接可协调跨仓库，具体仓库连接保持本仓库范围。

有 `tasks:publish` 的管理员在首次认领前可用 `hub_set_task_dependencies` 提交完整前置列表、当前 `expected_version`、调整 `reason`。更新后重回草稿并重新准入，旧决定保留。直接/间接循环及“子任务等待父任务”的死锁被拒绝。存在不可见前置时不能用不完整列表覆盖；首次认领后依赖冻结。

代码任务的同仓库范围按不区分大小写的目录前缀检查，例如 `src/web` 与 `src/web/login.ts` 重叠，`src/web` 与 `src/website` 不重叠。有效开发租约、review、testing、release_ready 阶段保留占用；释放、租约到期、退回或完成后释放。不同仓库、同仓库不重叠范围可并行。人工资料任务不占代码路径，项目协调父任务可使用 declaration，不因此获得代码证据保证。

`TASK_DEPENDENCIES_INCOMPLETE` 或 `TASK_SCOPE_CONFLICT` 时停止认领并协调，不能换 run、设备或角色绕过。检查只约束受管协议，没有操作系统写入锁。

## 同一电脑并行

1. 在具体仓库的原连接认领代码任务，保存 `task_id`、`run.id`、`lease_epoch`。
2. 在 `hub_prepare_work` **之前**调用 `hub_create_task_worktree`，传该租约、新 `operation_id`、可选安装目标 `harness: codex | claude | cursor`。未托管阶段仍需保持租约有效。
3. 目录固定在原仓库旁的 `.agent-hub-worktrees/<task_id>/<run_id>/`，分支为 `hub/<task_id>/<run_id>`。从当前已提交 HEAD 开始，原目录未提交工作继续保留，不自动 fetch、合并或转移修改。
4. 在返回的新目录打开开发工具、重新加载并信任工程。使用新 MCP 连接，确认 `hub_get_project` 的目录和仓库，再为同一租约准备、读取角色与记忆、确认规则。
5. 新连接固定该任务和运行，不能用来写其他任务。多个目录不共享全局模式；每轮分别保存日志、代码备份和 Git 回执。

同操作重试保留原 `operation_id`。程序保留中断目录；目录来源/分支不符、链接或未知已有文件时停止并人工检查。不使用 reset、clean 或自动删除来修复。同一运行已在其他目录准备时先保存、释放，再重新认领创建目录。

worktree 共享 Git 对象与仓库配置，只隔离工作文件和分支；同仓库代码保密仍需独立仓库和授权。不会自动启动其他 Agent 或合并结果。撤权后按安装收据卸载该目录的 MCP、职责、脚本和测试；保留业务文件、Git worktree 和加密检查点。完成后清理 worktree 仍需人工核对已有工作。
