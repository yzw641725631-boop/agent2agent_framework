# Agent Hub：身份与项目基础服务

> 2026-11-08｜0.5.0 / 迁移 0008：版本化项目规则、开工上下文确认、代码任务文件清单与摘要日志索引，配套 Windows 0.7.0。验证与边界见[本轮整改](../../设计文档/22_规则执行与本机检查点整改.md)。

本服务为多项目 Agent 框架提供统一身份、设备审批、项目授权和协作任务。当前已实现 REST API、中文管理后台、PostgreSQL 迁移、初始化及恢复 CLI、设备签名测试客户端、项目进度与 SSE、版本化 Markdown 索引，以及任务租约和不可覆盖的工作日志。通过 [Electron 桌面端](../agent-hub-desktop/README.md) 提供本机 stdio MCP。桌面有本机加密检查点；远程代码快照、自动记忆和撤权上传卸载仍待接入。文档接入步骤见 [Git 文档索引](../../设计文档/11_Git文档索引与MCP查询.md)。

## 已实现

- 只有超级管理员可以创建管理员、创建项目、分配项目管理员和批准授权。
- 管理员必须有验证邮箱；成员创建时可以选择邮箱。超管的新设备通过密码与本设备邮箱验证码自助确认；其他带邮箱的新设备先验证邮箱，再由超管审批。
- 项目管理员可以邀请本项目普通成员、申请成员授权，不能代替超管批准。
- 授权绑定用户、设备、项目、能力、有效期及撤销代次；审批校验请求版本和内容哈希。
- 设备使用 P-256 密钥签名。访问令牌、刷新令牌和设备登记凭证均须附带设备证明；复制 Token 不能换设备使用。
- 短期访问会话、刷新轮换及重放检测；续期不扩大范围、不延长会话家族的绝对有效期。
- 每次访问和续期重查当前账号、设备和项目资格；恢复账号或成员状态不会复活旧授权。
- 项目元数据与审计查询有 PostgreSQL RLS；API 拒绝使用表所有者、超级用户及 BYPASSRLS 数据库角色启动。
- 管理写操作支持幂等键；审计不保存原始密码、验证码、Token 或私钥。
- 首次初始化用服务器一次性码保护；实际验证超管邮箱后才建立账号，随后关闭初始化接口。
- 超管可在后台配置 SMTP、发送测试邮件、保存拟用 HTTPS 地址；SMTP 密码加密存储并且不回显。
- 管理查询、人员与设备状态、审批、项目成员、授权、审计和本人邮箱绑定/更换均有页面入口。
- 浏览器用 WebCrypto 签名，设备私钥以不可导出方式保存在 IndexedDB；会话凭证只存内存。
- 项目进度追加保存，幂等上报与游标分页；SSE 每两秒重查授权、十秒心跳，撤权或过期后结束流。
- 同一设备可创建互不撤销的项目会话家族，桌面端可同时使用多个已审批项目。
- 明确范围的 GitHub Markdown 索引、加密仓库凭证、独立 Worker、提交快照、中文文字片段搜索和全文读取；管理员 `documents:manage`、成员 `documents:read` 均按设备审批。

管理员资格与项目管理员角色分别记录：超管先创建具备管理员资格的账号，待其验证邮箱，再分配项目。分配角色后，设备的项目 grant 仍需签发。

## 当前边界

这套内部 REST 登录**尚不是标准 OAuth 授权服务器**。授权码/PKCE、正式 MCP 授权入口、密码重置和成员自主领取邀请仍待实现。账号目前由获授权管理员在创建时设置初始密码。后台保存的服务地址需要部署端配合才实际切换；当前 IP HTTPS 已发布，QQ 验证码投递与正式超管初始化已完成。

撤销现在会立即阻止普通操作，并写入待处理 outbox。外部 Git 撤权、独立收尾身份、快照上传回执和完整框架卸载尚未实现，返回 `closeout_available: false`；当前版本不执行本地删除。不要把这个原型当作已完成七种 harness 的接入发行版。

运行角色对认证内部表有服务所需的读写权；项目元数据、审计及文档表已启用 RLS，任务、规则和上下文表已增加项目策略，未来制品表仍须分别增加策略。RLS 上下文只能由服务端根据已验证身份设置，客户端不能传入数据库身份。短身份事务使用数据库协调锁；文档网络拉取发生在事务外。

### 0.3.1 并发与邮件

普通事务按排序锁定相关人员、资源及项目；只读事务使用共享锁，初始化/本机超管恢复保留独占恢复屏障。鉴权重查发生在取得锁之后，撤权与提交按短事务的先后顺序生效。普通业务连接池与设备证明、限流、拒绝审计连接池分开，等待连接/数据库锁有时限，繁忙返回 `503 SERVICE_BUSY`。单个 API 进程最多并行两次 Argon2 密码处理，当前部署为一个进程。

发信意图与挑战在业务事务中一起提交；收件人与验证码只以加密正文进入 outbox。提交并释放连接后发 SMTP，结束后清除正文。API 每 5 秒检查一次中断的投递，最多尝试 3 次，运行租约 3 分钟；SMTP 无法保证恰好投递一次。明确失败允许重发，已消费的验证码不会被失败回调删除。普通发信请求仍同步返回投递结果，崩溃时由持久意图补偿。

登录先限制来源 IP、验证设备证明，再为真实账号计数；过期桶每次清理最多 100 条。不为随机登录名建立账号桶，计数封顶。待审批设备的过期申请可用原设备密钥重申，拒绝状态不能自行重置。列表过滤过期记录并分页。

## 目录

| 路径 | 内容 |
|---|---|
| `src/agent_hub/api.py` | API 工厂、事务边界和权限入口 |
| `src/agent_hub/service.py` | 身份、审批、签发和撤销业务 |
| `src/agent_hub/security.py` | 密码、设备证明、重放防护和限流 |
| `src/agent_hub/administration.py` | 首次邮箱初始化、邮箱绑定与更换 |
| `src/agent_hub/site.py` | 加密站点设置及版本检查 |
| `src/agent_hub/static/` | 中文管理后台，无前端编译步骤 |
| `src/agent_hub/client.py` | 内部 REST 测试客户端，不持久化 Token |
| `src/agent_hub/cli.py` | 初始化、设备密钥和本地恢复命令 |
| `src/agent_hub/progress.py` | 持久进度与游标查询 |
| `src/agent_hub/documents.py` | 文档配置、权限、快照引用及查询 |
| `src/agent_hub/document_worker.py` | GitHub 读取、范围校验和作业领取/发布 |
| `migrations/versions/` | 六份可执行数据库迁移 |
| `tests/` | 真实 PostgreSQL 上的权限与会话测试 |
| `deploy/compose.server-db.yaml` | 已在指定服务器运行的数据库配置 |
| `deploy/compose.server-app.yaml` | 已部署的应用配置，复用服务器数据库 |
| `deploy/compose.server-edge.yaml` | 独立 IP HTTPS 入口，容器内 8443、宿主机 18180 |
| `deploy/renew-ip-tls.sh` | IP 短期证书检查、部署和入口重载 |
| `deploy/export_schema.py` | 离线导出首次初始化 SQL，不连接数据库 |

## 运行位置与约束

**Docker、PostgreSQL 和数据库测试统一在 `47.109.23.86` 服务器运行。本机 PostgreSQL 已删除。** 本机保留源码、虚拟环境及设备客户端开发工具；不再提供本地数据库初始化脚本。

| 项目 | 当前服务器位置 |
|---|---|
| 部署目录 | `/opt/agent-hub/database` |
| 容器 | `agent-hub-postgres`，PostgreSQL 18.6 |
| 数据库 | `agent_hub`，已迁移至 `0006` |
| 数据卷 | `agent-hub-postgres-data` |
| 私有网络 | `agent-hub-database`，内部主机名 `postgres:5432` |
| 运行/迁移角色 | `hub_runtime` / `hub_owner` |
| 文档 Worker | `hub_document_worker` 登录，继承 `hub_indexer`；与 API 角色分开 |
| 凭证 | 服务器 `.env`，root 所有，权限 `0600` |
| 对外端口 | 数据库不发布宿主机或公网端口 |
| 应用目录 | `/opt/agent-hub/application/current`，指向 `releases/0.3.2` |
| 应用容器 | `agent-hub-app-api-1`，仅发布服务器 `127.0.0.1:8765` |
| 公网入口 | `https://47.109.23.86:18180`，容器 `agent-hub-edge` |
| 入口与证书状态 | `/opt/agent-hub/edge`；每六小时检查证书续期 |
| 应用凭证 | `/opt/agent-hub/application/.env`，初始化码副本 `setup-token`，均为 `0600` |

实际版本、路径、备份与验证范围见[服务器部署记录](../../设计文档/07_服务器部署.md)。应用容器已加入现有私有网络，并有单独网络供 SMTP 出网；不额外创建数据库容器，也不把数据库账号分发给设备。

PostgreSQL 初始化脚本只在空数据卷执行；后续变更通过 owner 运行 Alembic。导出的首次安装 SQL 不能作为增量升级重复执行。API 长期使用 `hub_runtime`，迁移容器单独使用 `hub_owner`。

## 超管初始化与设备密钥

首选浏览器初始化：读取服务器一次性码，在页面保存 SMTP，填写超管账号和邮箱，发送并验证邮件后建立唯一超管与首台可信设备。无需把邮箱、SMTP 密码发到聊天里。

也保留 CLI：`agent-hub bootstrap <登录名> <邮箱> <公开JWK文件> --os windows`，macOS 设备用 `--os macos`。这条命令必须在部署主机、使用数据库 owner 配置执行，并真实验证邮箱。

CLI 设备密钥使用 `agent-hub device-key <设备私有目录>` 生成加密私钥与公开 JWK；浏览器会自行生成设备密钥。私钥留在设备。正式库已建立超管，首次初始化关闭；新 IP 地址的浏览器设备仍需邮箱验证和原可信设备审批。

## 内部 API 契约

以下接口已部署服务器，通过 SSH 转发使用。内部 REST 客户端 `DeviceClient` 负责 P-256 请求签名，公网接入要求 HTTPS。正式 OAuth/MCP 授权仍待实现。新增后台接口见[初始化与设置说明](../../设计文档/08_管理后台与初始化.md)。

| 路径 | 作用 |
|---|---|
| `POST /api/auth/enrollments` | 密码 + 设备证明，获取仅供设备登记的短期凭证 |
| `POST /api/devices/email-challenges` | 向账号绑定邮箱发送该设备验证码 |
| `POST /api/devices/email-verifications` | 单次消费验证码 |
| `GET /api/admin/approvals` | 超管分页查看未过期待审申请，支持 offset/limit |
| `POST /api/admin/approvals/{id}/decision` | 用版本、哈希批准或拒绝 |
| `POST /api/auth/sessions` | 批准设备创建平台会话，或指定自己设备的 grant |
| `POST /api/auth/project-sessions` | 以本人本设备获批 grant 创建独立项目会话，不撤销其他会话 |
| `POST /api/auth/refresh` | 用刷新凭证续期，不接受新权限参数 |
| `POST /api/admin/users/admins` | 超管创建有邮箱的管理员账号 |
| `POST /api/admin/projects` | 超管创建项目 |
| `PUT /api/admin/projects/{id}/administrators` | 超管分配已验证的管理员 |
| `POST /api/projects/{id}/member-invitations` | 在本项目新增普通成员关系 |
| `POST /api/grant-requests` | 请求精确项目/设备/能力授权 |
| `POST /api/admin/grants` | 超管在单事务中直接批准并签发 grant |
| `GET /api/me/grants` | 查看本人本设备获签发的有效 grant |
| `GET /api/projects/{id}/context` | 读取获授权项目元数据 |
| `GET /api/projects/{id}/progress` | 受 `project:read` 保护的进度分页，`after` 游标、`limit` 上限 100 |
| `POST /api/projects/{id}/progress` | 受 `tasks:write` 保护的追加报告，幂等键必需 |
| `GET /api/projects/{id}/progress/stream` | 签名 SSE，进度、追平标志与访问状态事件 |
| `POST /api/projects/{id}/members/{user}/revoke` | 项目停权，普通请求立即失效 |
| `POST /api/admin/projects/{id}/members/{user}/restore` | 超管恢复成员关系，仍须重新签发 grant |
| `POST /api/admin/users/{id}/status` | 超管管理全局人员状态 |
| `POST /api/admin/devices/{id}/revoke` | 超管撤销设备 |
| `GET /api/audit` | 超管全局审计，管理员按获授权项目审计 |

`Authorization: DPoP <opaque-token>` 和 `DPoP: <signed-proof>` 必须同时提供。登记凭证、访问凭证与刷新凭证分别使用；刷新令牌放在该端点的 Authorization 头中。管理修改请求还需 `Idempotency-Key`；重试同一操作必须保留原键。生成凭证的登录请求不缓存原始凭证。

## 超管恢复（部署主机）

`agent-hub recover-root <登录名> <全新公开JWK文件> --os windows` 在部署主机使用 owner 权限执行，验证超管密码及邮箱验证码，增加账号撤销代次并撤销旧设备。项目管理员没有对应 API 权限。命令源码已提供；真实 SMTP 与丢失设备场景尚未联调，离线恢复码尚未实现。

## 测试

本轮 **53 项测试**已在服务器 `hub_test` 独立库及测试容器通过，包含原有身份与进度回归，以及文档权限、RLS、索引一致性、撤权、作业恢复和归档边界。Windows 桌面 23 项测试与连接服务器测试服务的实际浏览器/Electron/MCP 联调通过，详见[文档实施说明](../../设计文档/11_Git文档索引与MCP查询.md)。本机数据库已移除。QQ 当前使用 587/STARTTLS，正式验证码邮件已由用户确认收到。

后续测试继续在服务器独立的 `hub_test` 测试库及 Dockerfile `tests` 目标运行，不在工作站启动数据库。测试入口为 `pytest -q`，由服务器受控注入 `HUB_TEST_OWNER_URL`、`HUB_TEST_RUNTIME_URL`、`HUB_SECRET`、`HUB_ENVIRONMENT=test`、`HUB_MAIL_MODE=memory`。测试会清空指定测试库业务表，两条 URL 的数据库名必须均为 `hub_test`；不要使用正式 `agent_hub` 数据库。

源码依赖固定在 `requirements.lock`，本机源码检查可使用 `ruff check src tests deploy migrations`。完整测试范围与剩余阶段见[实施记录](../../设计文档/06_实施记录.md)。
