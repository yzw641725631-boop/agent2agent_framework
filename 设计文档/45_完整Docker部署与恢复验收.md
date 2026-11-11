# 完整 Docker 部署与恢复验收

日期：2026-11-11。服务端 **0.22.0 / 数据库0032**，Windows **0.23.1**。

## 本轮交付

按用户要求，把原服务器分散的部署内容整理为仓库内的[完整部署工程](../projects/agent-hub-deployment/README.md)。完整克隆仓库后，其他设备上的AI可按其中的指令在目标Linux服务器构建、启动、检查、备份和恢复，无需当前开发电脑的 `.local` 运维文件。

统一Compose包含数据库、owner迁移、API、document/memory/embedding/generation四种Worker、私有编码服务、HTTPS网关、证书续期、备份巡检与恢复演练。后端和模型源码复用相邻的 `projects/agent-hub`、`projects/agent-hub-embedding`，不复制出第二份运行代码。所有业务和运维服务均在Docker容器内运行，宿主使用Python标准库启动器。

从服务器取回了真实公开Nginx配置、镜像摘要和环境变量名清单；实际部署的58个Python源文件与仓库按LF规范化逐一比对，全部一致。来源清单在[server-47.json](../projects/agent-hub-deployment/reference/server-47.json)，没有真实环境变量值。

模型和Windows大文件归档到[本仓库Release](https://github.com/yzw641725631-boop/agent2agent_framework/releases/tag/deployment-assets-20261111)，锁定字节数、SHA-256及公开API资产ID。模型保留原许可。Git保存源码、Compose、Dockerfile、SQL、运维程序和说明；不保存管理员/SMTP密码、私钥、应用密钥或业务数据库。

## 实际环境

- 所有Docker、PostgreSQL及模型推理均在服务器47执行；本机仅源码检查、公开制品整理、SSH运输和Git交付。
- 第一栈：`hub-portable-qa-20261111`，回环HTTPS端口19443；第二栈：`hub-portable-qa-restore-20261111`，回环HTTPS端口19444。
- 两栈使用不同目录、数据库密码、Docker网络和持久卷，按顺序运行以控制共享服务器资源。未使用正式生产卷 `agent-hub-postgres-data`。
- 第一栈使用只读公开模型/安装包缓存；第二栈从Release独立下载全部7个资源文件，不挂载第一栈或原服务器的模型/下载目录。
- Compose实测版本2.27.1。固定基础镜像和依赖利用服务器已有构建缓存；本轮并非逐层禁用缓存的外部软件源可达性验收。

## 验收结果

原始脱敏回执见[验收JSON](审计数据/2026-11-11_完整Docker部署验收.json)。

| 项目 | 实际结果 |
| --- | --- |
| 源码构建 | 数据库、应用、编码、运维四类镜像从仓库构建成功；修复原后端 `.dockerignore` 误排除构建所需tokenizer脚本的问题 |
| 空库初始化 | 首次SQL、角色和owner迁移成功，版本0032；API与四种Worker的五个运行账号权限符合预期，worklogs强制RLS |
| 完整启动 | API、四种Worker、真实模型、Nginx及scheduler运行；数据库、API、模型、网关健康 |
| HTTPS业务 | 使用合成身份、真实P-256设备签名，经网关登录、创建项目和任务、保存日志并上传256 KiB随机二进制工作树；中文后台HTML可读取 |
| 后台作业 | 独立文档Worker从公开GitHub固定提交取得README；记忆作业完成、512维模型产生语义向量；generation未配置外部提供方，调用数为0 |
| 加密备份 | 同一数据库导出快照生成dump与逐表摘要，restic完整读校验通过；80表、101行 |
| 恢复演练 | 从确定snapshot恢复临时库，80表/101行和结构摘要精确一致；已上传工作树可认证解密，记忆审计健康 |
| 停机重启 | 第一栈 `down` 后沿用原卷 `up --no-build`，原加密文件及256 KiB明文字节逐一一致 |
| 新目标迁移 | 第二栈从源加密仓库只读恢复，80表/101行及结构一致；继承源应用密钥，保留新目标的Postgres、owner、runtime及restic密码 |
| 迁移后复验 | 目标二进制工作树及加密文件摘要与源一致，HTTPS健康；再次加密备份与精确演练通过 |
| 拒绝非空覆盖 | 再次导入已有业务的目标被拒绝，拒绝前后运行容器ID不变，HTTPS仍健康 |
| 安装包下载 | 新网关完整下载ZIP 160,078,932字节、EXE 123,237,765字节，SHA-256均匹配；stable.json原始字节匹配锁定摘要 |
| TLS更新 | 发布新测试证书后网关自动reload；不匹配证书/私钥被拒绝，原活动证书保留 |
| Certbot兼容 | 固定镜像内Certbot 5.9.0，包含IP标识、证书profile、webroot和deploy-hook参数 |
| 容器巡检 | scheduler四类成功回执存在，最终无 `failed-*.json` 标记；没有实际异地备份或外部告警接收端 |
| 正式系统 | 正式API、PostgreSQL和网关仍健康，版本和镜像保持原状；本次测试未重启正式服务 |

复现夹具及命令见[隔离测试说明](../projects/agent-hub-deployment/tests/README.md)。夹具拒绝正式环境、非回环地址和非专用测试栈；seed不清空任何现有数据库。

验收后已按Compose项目标签清理两套专用测试栈、17个测试卷、专用镜像标签和两个隔离目录。正式容器的镜像、启动时间与健康状态清理前后一致，保护生产卷仍存在。最终提交前完成Ruff、Python语法、文档链接、暂存区源码摘要及凭证扫描；15个运行相关文件的Git暂存字节与服务器已验收源码SHA-256一致，签名清单原始字节未被换行转换。

## 联调中修复的问题

1. 后端Docker构建需要的缓存tokenizer脚本被旧 `.dockerignore` 排除，已只开放该明确文件。
2. Windows/Linux换行影响清单摘要：模型改用规范JSON摘要，签名更新外壳在Git中保持原始字节。
3. 准备和恢复容器的文件所有者/权限不足：按实际操作授予明确capability；恢复失败也清理本次随机临时数据库。
4. 编码服务健康响应与API不同，分别校验；运维容器加入私有语义网络。
5. 公开下载子目录受私有umask影响导致404，已设置公开目录遍历权限，并把下载路径/长度和签名清单字节纳入部署成功检查。
6. 服务器网络对部分下载域名和CDN地址超时：资源归档至仓库Release，启动器自动预取、尝试公开API与浏览器入口，并逐个完成DNS地址的正常TLS握手后下载。不修改宿主DNS、不关闭公开来源的证书校验；容器仍逐文件复核SHA-256。
7. 非空恢复在停服务前预检，再停写后复查；只恢复已提交快照所需制品。恢复源应用密钥后强制重建容器，更新单文件Secret挂载。

## 范围与后续

- 本轮验证的是同一物理服务器上的两个独立Docker目标，没有第二物理服务器；公网ACME实签/续期、真正异地备份接收端及外部告警仍待实际资源。
- TLS轮换实测使用明确的自签名回环测试模式，不作为公网受信证书验收。正式部署默认ACME，也支持本人提供有效证书。
- 固定桌面清单保留原签名、版本和2026-12-11到期时间；历史安装包可继续部署，真实客户端在线更新仍严格验证清单新鲜度。现有0.23.1客户端保留原发行方更新频道，独立发行需要另行构建和受控签名。
- 初次超管由本人在HTTPS页配置SMTP、验证邮箱和设置密码；未代本人登录。目标迁移后的服务地址、OAuth回调和第三方授权需要本人核对。
- 本套件验证新安装、同版本重建及同版本业务恢复；跨迁移版本升级继续单独验证非空升级、备份和回退。
- MCP桌面后台本轮离线，未取得项目工作日志共享回执。本仓库文档与验收JSON保存本轮记录，不声称已通过MCP共享。

部署操作详见[完整Docker部署说明](../projects/agent-hub-deployment/README.md)。参考接口：[Compose Secrets](https://docs.docker.com/compose/how-tos/use-secrets/)、[restic恢复](https://restic.readthedocs.io/en/stable/050_restore.html)、[GitHub公开资产下载](https://docs.github.com/en/rest/releases/assets)、[Python HTTPS](https://docs.python.org/3.12/library/http.client.html#http.client.HTTPSConnection)。
