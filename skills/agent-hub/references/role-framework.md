# 职业职责与受管部署

任务负责角色 `execution_role` 可选 `project-manager`、`architect`、`designer`、`developer`，默认开发工程师。创建任务时指定，认领时由服务器绑定，不能在认领参数里临时提高身份。职责不会授予项目、仓库或流程审批权限。

## 按需读取

- `hub_prepare_work` 的 `payload.role` 是当前执行职责，和规则、专注范围及获准记忆一同绑定准备包哈希。阅读完整包后再确认。恢复工作可用 `hub_load_memory` 联网验证后加载该实例加密保存的职责与记忆。
- 职责模板、规则或任务范围改变后旧确认失效，需要重新准备；不能把旧本地 Markdown 当作当前服务器授权。
- 需要拆解或检查交接分工时才调用 `hub_get_roles`，支持 `language: zh|en`。不在每轮全量读取七份职责。
- requirements 对应项目经理，design/feasibility 对应架构师，review/test/release 对应审查、测试、发布负责人；决定会记录真实人员、设备、harness 和角色。评审仍需独立身份，不能靠切换职业名称充当他人审查。

## 部署到工程

用户要求部署框架时，在目标工程的固定项目连接调用 `hub_preview_framework`，可独立指定 `doc_name_lang` 和 `doc_content_lang`，默认均为 `zh`。核对项目、目录和文件清单后，将相同语言参数与返回的 `preview_hash` 交给 `hub_install_framework`。旧预览被拒绝时重新预览，不猜测哈希。

部署内容位于 `.agent-hub/framework/<连接名称>/`：入口 `main.md`、本地 `doc_index.json`、七种职责、工作流程、Node 自检脚本与测试。只写受管文件，不自动执行脚本。用户修改的受管文件发生冲突时保留原文件，先处理冲突。PC“项目与 MCP → 部署职责”提供同一预览和部署入口。

安装收据在桌面数据内，撤权时沿用限定上传与按收据卸载。目录内索引用于本地职责文件，不是远程共享资料索引。部署不更改业务 AGENTS.md，也不启动子 Agent；[完成依赖与独立任务目录](parallel-work.md)由服务端及桌面提供。

## 自定义岗位与流程

新版提供 `hub_get_collaboration` / `hub_configure_collaboration`。有权负责人发布完整岗位列表、是否允许紧凑流程及是否要求操作说明。岗位 `key` 的基础职业在所有历史修订中冻结，删除后重新创建同一 key 也不能换职业；需要新职业时用新 key。

任务可设置 `role_profile`，对应基础职业必须一致。派工目标须自行登记该岗位。修改职责后相关开工包失效，重新准备确认；岗位不授予账号、仓库、日志或流程审批权。

`standard` 保留需求、设计、可行性、独立评审、测试、发布；项目明确开放后可选 `compact`，省略单独设计确认；`strict` 另外要求真实仓库和 Git 交付。非标准流程填写明确原因。执行后只能升级，先交接释放再调整并重新准入。

[本人批准的本机执行](hooks-and-execution.md)与岗位配置分别核验，配置岗位不会自动启动任何工具。
