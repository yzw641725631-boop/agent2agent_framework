# Agent Hub 完整 Docker 部署

这是新服务器的统一部署入口。完整克隆本仓库后，在目标服务器执行本目录的 `deploy.py`，即可构建和启动后端、数据库、四种 Worker、CPU 编码服务、HTTPS 网关、证书续期及备份巡检。服务器无需复制旧机器的私有 `.local` 脚本或 systemd 单元。

当前锁定：服务端 **0.22.0 / 数据库 0032**，PostgreSQL **18.6 + pgvector 0.8.7**，编码模型 **512 维 FP32**，Windows 下载 **0.23.1**。实际运行版本与文件校验见 [assets.lock.json](assets.lock.json)、[模型清单](../agent-hub-embedding/model-manifest.json)。

## 给接手 AI 的指令

> 完整克隆这个仓库，阅读 `projects/agent-hub-deployment/README.md`，在目标 Linux x86_64 服务器使用 `deploy.py init` 和 `deploy.py up` 完整 Docker 部署。使用新的栈名、域名和凭证，完成 `check`、`backup`、`drill`。首次超管账号由本人在 HTTPS 页面初始化。迁移旧业务时，先取得受控的加密备份和独立保管的 restic 密钥，只恢复到全新目标。不要使用本机开发工作站启动 Docker 或数据库。

## 目录关系与运行组件

```text
projects/
  agent-hub/                 # 后端、后台网页、CLI、迁移、角色 SQL、Dockerfile
  agent-hub-embedding/       # 私有 CPU 编码服务、固定模型清单、Dockerfile
  agent-hub-deployment/      # 本目录：完整 Compose、初始化与运维
    compose.yaml
    deploy.py
    scripts/
    templates/
    reference/               # 从服务器取回的公开配置与来源清单
```

| 容器 | 职责与权限 |
| --- | --- |
| `db` | 私有 PostgreSQL + pgvector，独立数据卷，不映射数据库端口 |
| `migrate` | 以 `hub_owner` 执行迁移后退出 |
| `api` | 普通 `hub_runtime`，中文后台、REST、远程 MCP、Git 代理与上传 |
| `document-worker` | 独立文档索引账号；需要 Git/模型接口出网 |
| `memory-worker` | 独立记忆整理账号 |
| `embedding-worker` | 独立编码队列账号；仅调用私有模型服务 |
| `generation-worker` | 独立后台生成账号；真实模型须超管配置并授权 |
| `embedding` | 固定权重 CPU 推理，无数据库密码、无公网端口 |
| `gateway` | 唯一对外入口：HTTP ACME 与跳转、HTTPS API 和 Windows 下载 |
| `scheduler` | 同机加密备份、巡检、归档/记忆检查、精确恢复演练 |
| `certificates` | ACME 模式下自动续期，网关检测证书更新后 reload |
| `setup` / `operations` | 按需运行的资源准备和手动运维容器 |

所有业务服务与运维程序均在容器内运行；宿主运行 Python 标准库启动器、Docker Engine 和 Compose。启动器从公开Release预取锁定资源到私有 `.runtime/assets`，校验长度和SHA-256，再只读挂载给准备容器重新核验；不在宿主运行数据库或模型推理。容器不挂载 Docker socket。备份权限较高的 `scheduler` / `operations` 仅在私有网络运行。

## 1. 新服务器条件

- Linux x86_64、Python 3.9+、Git、Docker Engine、**Compose 2.27+**（`docker compose`，也接受沿用相同接口的 v5）。本轮实测版本为 2.27.1，版本关系见 [Docker 官方说明](https://docs.docker.com/compose/support-and-feedback/faq/)。
- 建议专用机器至少 2 CPU、4 GiB 内存、20 GiB 可用磁盘；业务量、上传配额、保留备份需要额外容量。默认逐容器限制内存和 CPU。
- 域名指向服务器；开放 TCP 80 和配置的 HTTPS 端口。IP 证书也支持，但 IP 必须可从公网通过 80 完成 HTTP-01 验证。
- 现有 80/443 已占用时，先在目标机器规划入口；不能覆盖其他站点。也可使用已取得的证书和自定义端口。
- 构建、首次资源准备需要访问 Docker Hub、Debian/PostgreSQL 软件源、PyPI、GitHub。模型与Windows大文件均已归档到本仓库Release，部署时无需直连Hugging Face。资产均固定版本并校验摘要；没有配置镜像源绕过校验。

不要在 Windows 开发电脑执行部署命令。当前仓库的正式数据库仍在服务器47；新安装不会挂载 `agent-hub-postgres-data`。

## 2. 首次部署：自动 HTTPS

在目标服务器，用有 Docker 权限的运维账号执行。下例完整克隆仓库，不能只下载本目录：

```bash
git clone https://github.com/yzw641725631-boop/agent2agent_framework.git
cd agent2agent_framework/projects/agent-hub-deployment
python3 deploy.py init --name agent-hub-company --origin https://hub.example.com --email owner@example.com
python3 deploy.py up
python3 deploy.py check
python3 deploy.py status
```

把域名和邮箱替换为本人的实际值。`init` 生成 `.env`、随机凭证和 Nginx 配置；已有配置或同名Docker资源时拒绝覆盖。`up` 构建固定版本镜像、自动下载并双重校验资源、初始化空库、迁移、启动全部容器并验证 HTTPS，成功回执在 `.runtime/deployment-receipt.json`。下载优先用锁定的公开GitHub API资产入口，网络失败后尝试浏览器下载入口；两者都须通过相同SHA-256和长度校验。下载逐个尝试DNS返回的地址并保留正常TLS证书与主机名验证，避免个别CDN地址握手超时阻断部署。下载缓存也受 `.gitignore` 排除，可沿用宿主标准的 `HTTPS_PROXY` 设置；构建镜像时的代理由目标Docker安装配置负责。

IP HTTPS 示例：

```bash
python3 deploy.py init --name agent-hub-ip --origin https://203.0.113.10:18180 --email owner@example.com
python3 deploy.py up
```

IP 模式自动使用 Certbot 的 `--ip-address` 与 `shortlived` 配置，仍要求公网 80 端口，不能把 HTTP-01 验证改到 18180。续期容器每六小时检查一次，网关每三十秒检查证书变化。域名模式使用 DNS 标识。

打开配置的 HTTPS 地址，读取服务器上 `.runtime/secrets/bootstrap_token` 的一次性码，由本人配置 SMTP、验证邮箱并设置首个超管。这里没有默认管理员密码。服务端模型、GitHub 凭证与其他第三方授权随后在后台按需配置。

Windows客户端登录时将“服务地址”设为本次部署的HTTPS地址。归档的0.23.1客户端仍使用原发行方的签名更新频道；本目录提供其固定下载副本，不会自动把已有客户端的发行信任切换到新服务器。需要运营独立的桌面更新频道时，另按桌面发行流程配置频道、签名根和新构建。

### 已有证书

```bash
python3 deploy.py init --name agent-hub-company --origin https://hub.example.com:18443 --http-port 18080 --tls existing --cert /secure/fullchain.pem --key /secure/privkey.pem
python3 deploy.py up
```

证书与私钥须匹配，SAN 包含目标域名或 IP，且未过期。该模式不启动 Certbot；证书续期由提供证书的运维系统负责。不要将证书私钥加入 Git。

### 同机隔离验证

明确的测试模式仅接受回环监听和自签名证书，不用于本人真实登录：

```bash
python3 deploy.py init --name agent-hub-test --origin https://127.0.0.1:19443 --http-port 19480 --bind 127.0.0.1 --tls local-test --test-mode
python3 deploy.py up
```

测试模式使用内存邮件，只适合合成身份。若有已经校验过的公开模型/安装包缓存，`init` 可附加 `--model-cache /absolute/model-fp32 --desktop-cache /absolute/windows-x64`；它们只读挂载到准备容器，仍逐文件校验，不读取源服务器数据库或私有配置。

## 3. 日常操作

```bash
python3 deploy.py status
python3 deploy.py check
python3 deploy.py audit
python3 deploy.py backup
python3 deploy.py drill
python3 deploy.py down
python3 deploy.py up --no-build
```

`down` 保留全部持久卷及 `.runtime`，重新启动可沿用原有业务。`up --no-build` 要求镜像已经构建；同一源码版本可重建容器。跨应用/迁移版本升级须单独验证非空数据迁移和回退，不把这套首次部署流程当作任意版本的自动升级保证。

`up` / `check` 同时验证HTTPS健康、版本与迁移、签名清单原始字节、ZIP/EXE公开下载路径及长度；资源准备阶段已经逐文件核验完整SHA-256。

默认巡检每五分钟、加密备份与归档/记忆审计每天、恢复演练每三十天；首次启动立即执行。间隔在 `.env` 中调整，随后重建 `scheduler`。回执保存到本栈的 `backups` 卷，`failed-*.json` 表示对应任务失败，不能用旧成功回执掩盖当前失败。

读取脱敏回执和运行日志：

```bash
docker compose --profile tools run --rm --no-deps --entrypoint cat operations /backups/last-backup.json
docker compose --profile tools run --rm --no-deps --entrypoint cat operations /backups/last-restore.json
docker compose logs --tail 50 scheduler
```

运维命令失败时，宿主诊断在 `.runtime/failed-command.log`；容器内部诊断在本栈 `staging` 卷的 `failed-command.log` / `failed-operation.log`。诊断可能含业务信息，按私有文件处理，不提交 Git、不粘贴聊天。

## 4. 备份与另一台服务器恢复

`backup` 同时产生 PostgreSQL 导出和**同一导出快照**下的逐表行摘要、结构摘要，然后用 restic 加密保存数据库、上传的工作树、模型、安装包和恢复所需配置。完整 `restic check --read-data` 成功后才写成功回执。`drill` 从确定的完整 snapshot ID 恢复到随机临时数据库，精确比较数据和结构，并解密核验上传制品与记忆；最终删除该次临时库。

这仍是同机备份。上线时须把加密仓库复制到独立主机/桶，并实际验证异地副本。restic 密码必须独立受控保管；失去密码就无法解密备份。

### 导出一份静止的加密仓库

完成 `python3 deploy.py backup` 后记录返回的 **64 位 `snapshot_id`**。短暂停止本栈 scheduler，复制完整仓库后恢复它；业务 API 可以继续服务。不要只复制 PostgreSQL 数据目录。

```bash
docker compose stop scheduler
mkdir -m 700 /secure/agent-hub-transfer
docker compose --profile tools run --rm --no-deps --entrypoint tar operations -C /backups -cf - repository > /secure/agent-hub-transfer/repository.tar
cp .runtime/secrets/restic_password /secure/agent-hub-transfer/restic-password
chmod 600 /secure/agent-hub-transfer/repository.tar /secure/agent-hub-transfer/restic-password
docker compose up -d scheduler
```

同时避免其他人工备份/演练占用仓库。将加密包与密码通过不同的受控通道交给目标运维人员；不要将它们推送 GitHub。示例为本地文件交付，不声称已配置异地接收端或外部告警。

### 在全新目标导入

在另一服务器**独立完整克隆仓库**，按前述步骤使用新的栈名、地址、数据库密码完成 `init` 与 `up`。暂不创建用户或业务，再导入同版本备份：

```bash
mkdir -m 700 /secure/import
tar -xf /secure/agent-hub-transfer/repository.tar -C /secure/import
python3 deploy.py restore --repository /secure/import/repository --password-file /secure/agent-hub-transfer/restic-password --snapshot <完整64位snapshot_id>
python3 deploy.py check
python3 deploy.py audit
python3 deploy.py backup
python3 deploy.py drill
```

恢复前先检查目标没有用户、项目、日志、快照和上传文件；非空目标会在停服务前被拒绝。通过后停写，导入数据库、已提交的工作树及原应用加密密钥，保留**目标新建的数据库账号密码和 restic 密码**。临时上传和孤儿文件保留在源加密备份内。源仓库只读挂载，必须是静止的独立副本；恢复精确核验失败时不会重新开放业务服务。

成功后强制重建应用容器，确保单文件 Secret 挂载读取新密钥，再校验 HTTPS。源平台设置、SMTP、加密 Git/模型配置随业务恢复；域名变更后的 OAuth、Web 会话、桌面服务地址及第三方回调须由本人重新核验。需要清空旧数据的迁移应另建目标，不能对原生产卷执行删除。

## 5. 提交范围与资源来源

- Git 保存所有服务端源码、Dockerfile、Compose、SQL、运维程序、公开 Nginx 模板、模型和下载资源的锁定摘要及部署说明。
- `reference/server-47.json` 和 `reference/nginx-server-47.conf` 来自服务器实际部署；清单只保留环境变量名，没有值。实际后端 58 个 Python 文件已按 LF 规范化与仓库源码比对。它是来源证据，不是可复用的旧机器私有配置。
- 模型来源为 Hugging Face 的固定 commit，逐文件 SHA-256 校验，保留 [BGE MIT 许可](reference/BGE-LICENSE)。模型和Windows EXE/ZIP 已归档到[本仓库 GitHub Release](https://github.com/yzw641725631-boop/agent2agent_framework/releases/tag/deployment-assets-20261111)，使用固定下载地址与 SHA-256，不依赖服务器47存活，也不作为 Git 源码 blob 提交。
- 固定的桌面签名清单按签发时间核验历史真实性，原始字节保持不变。**实际客户端在线更新仍核验清单到期时间**，当前清单到期为 2026-12-11。固定安装包可继续部署；续发在线更新清单须由发行人使用受控签名流程生成，私钥不进入仓库。
- `.env`、`.runtime/`、数据库内容、SMTP/管理员密码、应用密钥、证书私钥和 restic 密码均不提交。其他设备生成自己的新凭证；迁移业务须通过上述加密备份。

Compose Secrets 是逐服务单文件挂载；宿主 `.runtime` 与 `secrets` 目录为 `0700`，单个文件为 `0444` 以兼容容器非 root UID。不能把单个秘密文件移到公开目录。主机 root 与 Docker 管理员仍能读取宿主秘密。

## 6. 已验证与边界

验收记录见[完整 Docker 部署与恢复验收](../../设计文档/45_完整Docker部署与恢复验收.md)。本轮在服务器47新建的隔离栈运行，未切换正式系统；公网 ACME 新签、第二物理服务器和真正异地告警/备份接收端不由回环验收替代。

参考：[Docker Compose Secrets](https://docs.docker.com/compose/how-tos/use-secrets/)、[Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order/)、[Certbot IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html)、[restic 恢复](https://restic.readthedocs.io/en/stable/050_restore.html)。
