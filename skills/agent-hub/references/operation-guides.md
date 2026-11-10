# 功能操作说明

当连接提供说明工具时，在创建任务前读取 `hub_get_collaboration`。`require_guides` 开启的项目，代码任务须安排说明，确实没有对外操作变化时由有权负责人填写至少 10 字的明确原因。

任务的 `documentation.kind` 选择 `user`、`api`、`operations` 或 `none`；需要说明时填写 `module`，不要把默认 `none` 当作自动豁免。只为指定功能生成说明，不扩大开发目标。

## 在原对话接续

1. 正常保存检查点并提交任务。提交结束开发租约，但为每个参与实例的最后冻结检查点建立独立说明待办；原文和实例归属保留。
2. 提交响应有 `guide_generation` 时，先核对待办的 `execution_policy.server_enabled`。已开启服务端生成时查看进度，不重复认领；否则在同一实例继续。使用 `hub_list_guides(instance_id)` 查本人本设备待办；`hub_claim_guide` 使用当前 `expected_version` 和原实例 ID 取得十分钟独立租约。
3. `hub_get_guide_input` 读取冻结任务、来源日志、提交号及输出结构。代码核对须对应该冻结提交或已核验备份；不能用后来改动的工作目录冒充原实现。
4. 用 `hub_complete_guide` 提交：功能用途、产品版本、使用角色、前提、入口、步骤、输入校验、结果、异常恢复、提示、完整示例、变化、限制和真实核验依据。保持当前版本、租约代次与 `source_hash`。说明不能包含凭证或不必要的内部代码。
5. 来源不足、环境不具备或超时，用 `hub_retry_guide` 留下具体原因并释放独立租约；不编造截图或验证成功。断网时待办留在服务器，恢复后重新核对状态。

不要认领其他人的实例或复用失效的开发租约。负责人可用 `hub_reassign_guide` 明确移交给同仓库中仍获准的登记执行者，原来源与旧处理记录保留。

## 审核、阅读与发布

`hub_decide_guide` 的产品与测试两项均针对当前 `draft_version`。产品审核明确正式受众：`repository` 或 `project`。选择 project 前须移除内部资料，此受众允许有 `guides:read` 的项目成员查阅，即使没有代码权。

作者本人不能独立审核；超管例外需明确记录原因。退回保留旧草稿；新稿重新审核。数据库核对状态转换、不可变事件、双重审核以及对应任务的发布状态，不能直接改状态跳过。

所有当前提交要求的说明均通过，才允许任务发布；正式正文随功能发布冻结。`hub_list_guides` 可搜索和分页，`hub_read_guide` 按需展开正文。仅有指南阅读权的人不能读取原工作日志、私有事件或代码引用。

超管可以在仓库服务端 AI 授权中开启后台说明草稿生成。服务器读取冻结来源，记录 `server-background` 回执；原贡献者和实例归属保留，仍须产品和测试核对。不把已排队说成已生成，也不将后台起草当成原开发者已人工核验。

## 发布后的维护

迁移 0026 起，PC/Web“功能操作说明”及新版 MCP 支持以下流程：

- `hub_send_guide_feedback` 反馈当前 `catalog_version` 的问题、建议或过时内容；`hub_list_guide_feedback` 中读者只能查看自己的反馈。拥有来源仓库及发布管理权的人可用 `hub_resolve_guide_feedback` 记录处理；原反馈及处理历史保留。
- 拥有来源仓库写入权的人用 `hub_amend_guide` 提交完整新正文和 `base_version`；`hub_list_guide_amendments` 展开修订。核对期间目录继续显示旧发布正文，只读者看不到未发布修订。
- 用 `hub_decide_guide_amendment` 分别记录产品、测试核对。作者自审规则沿用原流程；退回后创建新修订。目录版本变化使旧修订不能继续核对或发布。
- 两项通过后，发布负责人用 `hub_publish_guide_revision(action: published)` 明确替换。`expected_version` 是目录版本，须传当前修订 ID。受众沿用原说明，不借修订扩大范围。
- 同一工具用 `archived` / `restored` 归档或恢复；归档默认不在目录出现，`include_archived` 可查。归档保留正文和全部历史，不删除原日志或代码。
- `hub_list_guide_publications` 查询历史，`hub_read_guide_publication` 读取指定目录版本；版本 0 是最初随功能发布的正文。修订、发布、归档、恢复及反馈处理形成项目进度记录。

目录限定当前项目及授权仓库，不能通过搜索、历史版本或反馈 ID 读取其他项目或私有代码资料。
