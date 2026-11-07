# 安装桌面端、Skill 与 MCP

源码与安装入口：<https://github.com/yzw641725631-boop/agent2agent_framework>。EXE 和签名在线更新包由该仓库指定的正式服务器提供。

当前一体安装针对 Windows x64，自动配置 Codex、Claude Code、Cursor。用户已经说明开发工具时直接选择；其他开发工具和 macOS 暂不套用未验证的安装路径。

首次安装无需预先安装本 Skill。Agent 读取框架仓库 README 和安装脚本，从仓库取安装入口及 `releases/windows-x64.json`，在临时安装目录执行。用户的业务代码目录与框架安装源码分开。

从框架仓库工作目录运行以下命令，`codex` 可替换为 `claude` 或 `cursor`：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install-windows.ps1 -Harness codex
```

这里的执行策略参数只作用于本次 PowerShell 进程，不更改系统长期设置。遵守当前宿主对脚本执行、网络与 MCP 使用的权限要求，不修改其审批、信任或沙箱策略。

安装入口负责校验并安装或复用桌面端、安装同一发行版的 Skill、合并开发工具的用户级 MCP 配置。已有其他连接与设置保留；同名未受管配置有冲突时停止并保留原文件。不要自动删除冲突项以绕过检查。重试可以复用已成功安装的程序。

安装后启动 Agent Hub。首次身份验证仍由用户在 PC 端完成，包括邮箱验证及必要的新设备审批。安装包不携带超管 Token，不替用户批准设备，也不自动获得业务仓库权限。

重新加载开发工具后，调用 `hub_connection_status` 和 `hub_check_version` 确认。工具未发现时先检查 MCP 是否已重新加载、是否受宿主策略限制，再检查桌面后台。

登录另一个账号时，原安装连接不会自动获得新账号权限。在 PC 的“桌面与消息推送 → Agent 一键安装连接”重新授权后，再运行安装入口更新本机配置；旧连接 ID 会失效。

安装成功仅表示客户端、Skill 和 MCP 已接入。完整项目组、多仓库组装和原 install 的全部角色文档并未随客户端安装自动部署。
