'use strict';

const { powershell, encodedCommand } = require('./extract-win.cjs');

const sessionRule = '每次开始或恢复使用 Agent Hub 的项目对话时，先调用该项目的 hub_check_version。' +
  '只有返回 can_continue: true 才开始读取项目文档、上报进度和执行其他受管操作。' +
  '若客户端需要更新，等待桌面端完成自动更新和重启，再检查版本。若检查失败，提示用户查看桌面端状态。';

const quoted = value => "'" + value.replaceAll("'", "''") + "'";

function sessionHook({ command, args }, host) {
  const argumentsList = [command, ...args, '--session-check'].map(quoted).join(' ');
  const script = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n" +
    "$env:ELECTRON_RUN_AS_NODE = '1'\n& " + argumentsList;
  const argumentsForShell = ['-NoProfile', '-NonInteractive', '-EncodedCommand', encodedCommand(script)];
  const handler = host === 'codex' ? {
    type: 'command', command: 'powershell.exe ' + argumentsForShell.join(' '),
    statusMessage: '检查 Agent Hub 客户端版本', timeout: 15,
  } : {
    type: 'command', command: powershell(), args: argumentsForShell, timeout: 15,
  };
  return { hooks: { SessionStart: [{ hooks: [handler] }] } };
}

module.exports = { sessionRule, sessionHook };
