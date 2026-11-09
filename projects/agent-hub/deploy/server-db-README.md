# Agent Hub 服务器数据库

数据库部署在指定服务器 `47.109.23.86`，开发电脑不安装 PostgreSQL 或 Docker。

## 当前配置

- 目录：`/opt/agent-hub/database`；Compose：`compose.yaml`。
- PostgreSQL 18.6，pgvector 0.8.7；应用 0.9.0，Alembic `0012`。
- 镜像：`agent-hub-postgres:18.6-vector0.8.7`，从固定上游源码构建，见 `Dockerfile.postgres-vector`。
- 容器：`agent-hub-postgres`；数据卷：`agent-hub-postgres-data`。
- 数据库：`agent_hub`；私有网络：`agent-hub-database`，服务地址 `postgres:5432`。
- 数据库端口不映射到宿主机；512 MiB 内存、1 核 CPU、最多 40 个连接。
- `hub_owner` 负责迁移，`hub_runtime` 执行业务请求。
- `hub_document_worker` 继承文档索引角色 `hub_indexer`。
- `hub_memory_worker` 继承摘录角色 `hub_memory`；只处理原文与派生卡片，不持有仓库或发信凭证。
- 凭证只保存在服务器 `.env`，root 所有、权限 `0600`，不写入仓库。

`memory_embeddings` 是可重建的空语义索引。扩展已经安装不代表已经配置 Embedding、语义召回或模型蒸馏。

## 查看状态

在服务器执行：

```sh
cd /opt/agent-hub/database
docker compose ps
docker exec agent-hub-postgres psql -X -U postgres -d agent_hub \
  -c "SELECT version_num FROM alembic_version; SELECT extversion FROM pg_extension WHERE extname='vector';"
```

## 升级已有数据卷

1. 先生成并验证备份，保留旧应用、Compose、首次初始化 SQL 和受控环境文件。
2. 先构建并在独立测试库验证固定版本的扩展镜像。升级保留原数据卷与私有网络。
3. 在服务器受控数据库 `.env` 中生成 `HUB_MEMORY_PASSWORD`，不要将密码回传到聊天或日志。
4. 暂停 API 与文档 Worker，切换扩展镜像，确认数据库健康。
5. 用数据库运维身份执行 `provision-memory-worker.sh`，准备独立角色并安装扩展。
6. 使用 `hub_owner` 执行 `0012` 迁移，再启动 API、文档 Worker 与记忆 Worker。
7. API 容器重建后校验并重载网关 Nginx，使其重新解析后端地址。核对版本、强制 RLS、原有人员与项目数量、公网健康检查；随后完成加密备份和独立恢复演练。

修改 `.env` 不会自动更改已有数据库账号密码。已有卷不重新运行首次初始化 SQL，不删除数据卷。迁移后若回退应用，保留带 `vector.so` 的数据库镜像；旧的无扩展镜像不能直接读取带向量列的数据库。

## 备份与恢复

- `/opt/agent-hub/backups` 保存本机加密 restic 仓库；备份包含一致性数据库 dump、不可覆盖的工作代码制品及受控环境文件。
- systemd 定时执行备份、制品校验、恢复演练和运行检查；查看 `agent-hub-backup.timer`、`agent-hub-restore-drill.timer`、`agent-hub-archive-audit.timer`、`agent-hub-ops-check.timer`。
- 恢复演练在临时数据库检查制品解密和共享记忆来源摘要，成功后删除临时库。
- 单独的非空测试恢复覆盖文档原字节、卡片和来源关系；正式库没有业务数据时，不能以空库恢复冒称真实业务恢复已验收。
- 持续异地备份和外部告警仍未配置。同机加密备份不能处理整机丢失。

升级前临时创建 custom 格式 dump：

```sh
cd /opt/agent-hub/database
umask 077
backup="backups/agent_hub_$(date -u +%Y%m%dT%H%M%SZ).dump"
docker exec agent-hub-postgres pg_dump -Fc -U postgres -d agent_hub > "$backup"
docker exec -i agent-hub-postgres pg_restore --list < "$backup" >/dev/null
```

首次安装 SQL 由 `deploy/export_schema.py` 离线生成；先由运维身份安装扩展，再切换迁移角色。不要用首次安装 SQL 覆盖已有数据库。
