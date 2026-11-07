# Agent Hub PostgreSQL

数据库运行在指定服务器 `47.109.23.86`。本机开发设备不安装 PostgreSQL 或 Docker。

- 部署目录：`/opt/agent-hub/database`
- Compose：`compose.yaml`
- 容器：`agent-hub-postgres`
- 版本：PostgreSQL 18.6
- 数据库：`agent_hub`；迁移版本：`0005`
- 数据卷：`agent-hub-postgres-data`
- Docker 私有网络：`agent-hub-database`，内部地址 `postgres:5432`
- 端口：未映射到宿主机，不对公网提供数据库访问
- 资源：512 MiB 内存，CPU 上限 1 核，最多 40 连接
- 重启策略：`unless-stopped`；现有 Docker 服务已启用系统启动
- 运行账号：`hub_runtime`；迁移账号：`hub_owner`
- 文档 Worker：`hub_document_worker`，继承 `hub_indexer`；API 不属于索引角色
- 凭证：服务器 `.env`，root 所有、权限 0600；不复制到代码仓库或设备
- 备份：`backups/`；已有首份 custom 格式备份，目录读取检查通过

查看状态：

```sh
cd /opt/agent-hub/database
docker compose ps
docker exec agent-hub-postgres psql -X -U postgres -d agent_hub -c 'SELECT version_num FROM alembic_version;'
```

创建数据库备份（在服务器执行）：

```sh
cd /opt/agent-hub/database
umask 077
docker exec agent-hub-postgres pg_dump -Fc -U postgres -d agent_hub > "backups/agent_hub_$(date -u +%Y%m%dT%H%M%SZ).dump"
```

修改 `.env` 不会自动修改已初始化的数据库账号密码。后续结构升级使用迁移账号执行 Alembic，不删除数据卷，不重复运行首次初始化 SQL。

API 0.3.0、独立文档 Worker 和 IP HTTPS 已部署，QQ SMTP 与超级管理员初始化已完成；正式库 1 用户、0 项目。测试只使用服务器独立 `hub_test` 库。已有服务器升级到 0005 前执行 `provision-document-worker.sh`；首次安装时服务器 `.env` 还需 `HUB_INDEXER_PASSWORD`，不要重跑初始化 SQL。

首轮已完成数据库启动、健康检查、运行账号密码认证、RLS 配置、备份目录读取与容器重启持久化检查。完整备份还原与定时/异地备份尚待配置；数据库备份不包含 `.env`，凭证需另外受控保管。
