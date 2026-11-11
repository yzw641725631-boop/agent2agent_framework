# 隔离验收夹具

普通部署只执行主 README 的 `init/up/check/backup/drill`。本目录的脚本会创建合成超管和业务，仅用于本次独占的新测试栈，不允许对正式业务运行。

在目标 Linux 服务器按主 README 初始化测试栈，名字须以 `hub-portable-qa-` 开头，同时指定 `--tls local-test --test-mode --bind 127.0.0.1`。HTTPS 主机须为 `127.0.0.1`。脚本再次核对私有配置；已有用户、项目、日志、快照或上传文件时拒绝 seed，不清空数据库。

启动全部容器后，在部署目录执行：

```bash
docker compose --profile tools run --rm --no-deps -e HUB_PORTABLE_ACCEPTANCE=isolated-only -v "$PWD/tests/acceptance.py:/acceptance.py:ro" -v "$PWD/../agent-hub/tests:/backend-tests:ro" --entrypoint python operations /acceptance.py seed
python3 deploy.py backup
python3 deploy.py drill
```

seed 仅经 owner 连接建立明确的合成初始身份；后续使用真实设备签名和 HTTPS 网关登录、创建项目和任务、上传随机256 KiB二进制备份，并让独立document/memory/embedding Worker处理作业。文档来源为公开仓库的固定提交，编码使用真实512维模型；未配置付费生成提供方，不声称验证外部模型质量。

`/staging/acceptance.json` 为该次随机数据的私有核验基准，含应用密钥摘要，不可作为日常业务记录提交Git。随后可验证停机/重建后的数据：

```bash
docker compose --profile tools run --rm --no-deps -e HUB_PORTABLE_ACCEPTANCE=isolated-only -v "$PWD/tests/acceptance.py:/acceptance.py:ro" -v "$PWD/../agent-hub/tests:/backend-tests:ro" --entrypoint python operations /acceptance.py verify
```

第二套独立测试栈完成主 README 的 `restore` 后，将源测试的私有 `acceptance.json` 安全复制到目标的 `staging` 卷，再运行相同 `verify`。比对源应用密钥、加密文件、解密后的随机二进制字节、数据库角色与RLS。第二栈配置必须仍满足上述测试边界。

本轮结果与范围见[验收报告](../../../设计文档/45_完整Docker部署与恢复验收.md)。测试结束用 `deploy.py down` 停止；任何后续测试卷清理都必须核对Compose项目标签，仅操作本次独占资源。
