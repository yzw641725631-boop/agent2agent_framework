'use strict';

const crypto = require('node:crypto');
const path = require('node:path');
const { HubError } = require('./remote.cjs');
const { encodedCommand } = require('./extract-win.cjs');

const common = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionEnd'];
const cursorEvents = { sessionStart: 'SessionStart', beforeSubmitPrompt: 'UserPromptSubmit',
  preToolUse: 'PreToolUse', beforeMCPExecution: 'BeforeMCPExecution', postToolUse: 'PostToolUse', stop: 'Stop', sessionEnd: 'SessionEnd' };
const catalog = {
  codex: { file: ['.codex', 'hooks.json'], events: [...common, 'Interrupt'], source: 'https://learn.chatgpt.com/docs/hooks' },
  'claude-code': { file: ['.claude', 'settings.local.json'], events: common, source: 'https://code.claude.com/docs/en/hooks' },
  cursor: { file: ['.cursor', 'hooks.json'], events: Object.keys(cursorEvents), source: 'https://cursor.com/docs/hooks' },
  qoder: { file: ['.qoder', 'settings.local.json'], events: common, source: 'https://docs.qoder.com/cli/hooks' },
  codebuddy: { file: ['.codebuddy', 'settings.local.json'], events: common, source: 'https://www.codebuddy.cn/docs/cli/hooks' },
  workbuddy: { file: null, events: [], source: 'https://open.workbuddy.cn/docs/connector' },
  trae: { file: null, events: [], source: 'https://docs.trae.ai/ide/hook-configuration-reference' },
};
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
function spec(harness) {
  const value = catalog[harness];
  if (!value?.file) throw new HubError('HOST_HOOKS_UNAVAILABLE', '该工具的生命周期接口尚未核验，不能标记为已启用。');
  return value;
}
const psQuote = value => "'" + value.replaceAll("'", "''") + "'";
const shQuote = value => "'" + value.replaceAll("'", "'\\''") + "'";
function configuration(connection, harness, installationId, platform = process.platform) {
  const adapter = spec(harness);
  if (!['win32', 'darwin', 'linux'].includes(platform)) throw new HubError('UNSUPPORTED_PLATFORM');
  const hooks = {};
  for (const event of adapter.events) {
    const argv = [connection.command, ...connection.args, '--lifecycle-hook', event, '--harness', harness,
      '--hook-installation', installationId];
    const command = platform === 'win32'
      ? 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + encodedCommand(
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n$env:ELECTRON_RUN_AS_NODE = '1'\n& " + argv.map(psQuote).join(' ') +
        '\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }')
      : 'env ELECTRON_RUN_AS_NODE=1 ' + argv.map(shQuote).join(' ');
    if (harness === 'cursor') hooks[event] = [{ command, timeout: 12,
      ...(['preToolUse', 'beforeMCPExecution'].includes(event) ? { failClosed: true } : {}), ...(event === 'stop' ? { loop_limit: 1 } : {}) }];
    else hooks[event] = [{ ...(event === 'PreToolUse' || event === 'PostToolUse' ? { matcher: '.*' } : {}),
      hooks: [{ type: 'command', command, timeout: event === 'Interrupt' || event === 'SessionEnd' ? 3 : 12,
        ...(harness === 'claude-code' && event === 'PreToolUse' ? { onFailure: 'block' } : {}) }] }];
  }
  return { ...(harness === 'cursor' ? { version: 1 } : {}), hooks };
}
function eventName(harness, event) {
  if (!spec(harness).events.includes(event)) throw new HubError('INVALID_HOOK_INPUT');
  return harness === 'cursor' ? cursorEvents[event] : event;
}
function operation(input) {
  const name = input.tool_name;
  if (typeof name !== 'string' || !name || name.length > 200) return { kind: 'unknown' };
  const data = input.tool_input;
  const result = { name, kind: 'unknown' };
  if (['Read', 'Glob', 'Grep', 'read_file', 'view_image'].includes(name)) return { ...result, kind: 'read' };
  if (name.startsWith('mcp__') || name.startsWith('MCP:')) return { ...result, kind: 'mcp' };
  if (['Bash', 'Shell', 'exec_command', 'run_terminal_command'].includes(name)) {
    const command = data?.command ?? data?.cmd;
    // Deliberately narrow: arbitrary scripts cannot be proven to respect protected paths.
    const safe = typeof command === 'string' && /^(?:pwd|Get-Location|git (?:status(?: --short)?|diff(?: --stat| --name-only)?|rev-parse HEAD|log --oneline -n [1-9][0-9]?))$/.test(command.trim());
    return { ...result, kind: 'shell', safe_read: safe,
      ...(typeof command === 'string' ? { command_hash: hash(command) } : {}) };
  }
  let files;
  if (['Write', 'Edit', 'Delete', 'MultiEdit', 'write_file', 'edit_file'].includes(name)) {
    files = data?.file_path ? [data.file_path] : data?.path ? [data.path] : null;
    if (name === 'MultiEdit' && Array.isArray(data?.edits)) files = data.edits.map(edit => edit.file_path || data.file_path);
  } else if (name === 'apply_patch' || name.endsWith('.apply_patch')) {
    const patch = typeof data === 'string' ? data : data?.command ?? data?.patch ?? data?.input;
    if (typeof patch === 'string' && patch.length <= 1024 * 1024 && patch.startsWith('*** Begin Patch')) {
      files = [...patch.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm)].map(match => match[1]);
    }
  }
  if (files?.length && files.length <= 100 && files.every(file => typeof file === 'string' && file.length <= 4096 && !/[\x00-\x1f]/.test(file)))
    return { ...result, kind: 'edit', paths: [...new Set(files)] };
  return result;
}
function normalize(input, event, harness, installationId) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new HubError('INVALID_HOOK_INPUT');
  const canonical = eventName(harness, event);
  const session = harness === 'cursor' ? input.conversation_id ?? input.session_id : input.session_id;
  const cwd = input.cwd ?? (harness === 'cursor' && input.workspace_roots?.length === 1 ? input.workspace_roots[0] : undefined);
  const turn = harness === 'cursor' ? input.generation_id : input.turn_id;
  if (!input || typeof input !== 'object' || Array.isArray(input) ||
    input.hook_event_name !== undefined && input.hook_event_name !== event ||
    typeof session !== 'string' || !session || session.length > 200 || /[\x00-\x1f]/.test(session) ||
    typeof cwd !== 'string' || cwd.length > 4096 || !path.isAbsolute(cwd) ||
    turn !== undefined && (typeof turn !== 'string' || turn.length > 200) || !/^[a-f0-9]{64}$/.test(installationId || ''))
    throw new HubError('INVALID_HOOK_INPUT');
  return { harness, event: canonical, installation_id: installationId, session_id: session, cwd,
    ...(turn ? { turn_ref: turn } : {}), stop_hook_active: input.stop_hook_active === true || input.loop_count > 0,
    ...(canonical === 'PreToolUse' ? { operation: operation(input) } : {}),
    ...(canonical === 'BeforeMCPExecution' ? { operation: { kind: 'mcp',
      name: typeof input.tool_name === 'string' && input.tool_name.length <= 200 ? input.tool_name : '',
      server: typeof input.mcp_server_name === 'string' && input.mcp_server_name.length <= 200 ? input.mcp_server_name : '' } } : {}) };
}
function output(value, harness, event) {
  if (harness !== 'cursor') return value;
  const canonical = eventName(harness, event);
  if (['PreToolUse', 'BeforeMCPExecution'].includes(canonical)) return value.hookSpecificOutput?.permissionDecision === 'deny'
    ? { permission: 'deny', user_message: value.hookSpecificOutput.permissionDecisionReason,
      agent_message: value.hookSpecificOutput.permissionDecisionReason } : { permission: 'allow' };
  if (canonical === 'Stop') return value.decision === 'block' ? { followup_message: value.reason } : {};
  const context = value.hookSpecificOutput?.additionalContext || value.systemMessage;
  if (canonical === 'UserPromptSubmit') return { continue: true };
  return ['SessionStart', 'PostToolUse'].includes(canonical) && context ? { additional_context: context } : {};
}
module.exports = { catalog, spec, configuration, normalize, operation, eventName, output, hash };
