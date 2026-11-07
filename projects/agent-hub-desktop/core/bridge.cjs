'use strict';

const fs = require('node:fs/promises');
const { call } = require('./broker.cjs');
const { HubError } = require('./remote.cjs');
const { dispatch, error } = require('./mcp.cjs');
const { version } = require('../package.json');

async function binding(file) {
  try {
    const info = await fs.lstat(file);
    if (!info.isFile() || info.size > 8192) throw new Error('Invalid binding descriptor');
    const descriptor = JSON.parse(await fs.readFile(file, 'utf8'));
    if (descriptor.version !== 1 || typeof descriptor.endpoint !== 'string' ||
        typeof descriptor.binding !== 'string' || typeof descriptor.secret !== 'string') {
      throw new Error('Invalid binding descriptor');
    }
    return descriptor;
  } catch (failure) {
    if (failure.code === 'ENOENT') throw new HubError('LOGIN_REQUIRED', '请在桌面端登录并启用对应的 MCP 连接。');
    throw failure;
  }
}

async function sessionCheck(file) {
  let message;
  try {
    const status = await call(await binding(file), { action: 'hub_check_version', args: {} });
    message = `Agent Hub ${status.installed_version}。${status.message}`;
    if (!status.can_continue) message += ' 版本检查通过前暂停该项目的受管操作，随后再调用 hub_check_version。';
  } catch (failure) {
    message = failure instanceof HubError ? failure.message : '请打开 Agent Hub 检查本机项目连接和版本。';
  }
  process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: message } }) + '\n');
}

async function relay(request, file) {
  if (request?.jsonrpc === '2.0' && typeof request.method === 'string' && !Object.hasOwn(request, 'id')) return null;
  const invoke = async (action, args) => call(await binding(file), { action, args });
  try {
    const descriptor = await binding(file);
    if (descriptor.protocol === 'desktop-mcp-v1') {
      const response = await call(descriptor, { action: 'mcp', args: { request } });
      const result = response?.result;
      if (result?.structuredContent && !result.content) {
        result.content = [{ type: 'text', text: JSON.stringify(result.structuredContent) }];
      }
      return response;
    }
  } catch (failure) {
    // Keep the transport connected while the broker is restarting.
    const check = async () => {
      if (request?.method === 'initialize') return { installed_version: version, message: failure.message };
      throw failure;
    };
    return dispatch(request, async () => { throw failure; }, check);
  }
  return dispatch(request, invoke);
}

async function run(argv = process.argv.slice(2)) {
  const index = argv.indexOf('--binding');
  if (index < 0 || !argv[index + 1]) throw new Error('Missing --binding');
  const file = argv[index + 1];
  if (argv.includes('--session-check')) return sessionCheck(file);
  let buffer = '', queue = Promise.resolve();
  process.stdin.setEncoding('utf8');
  const emit = response => { if (response) process.stdout.write(JSON.stringify(response) + '\n'); };
  process.stdin.on('data', chunk => {
    buffer += chunk;
    if (buffer.length > 128 * 1024) {
      emit(error(null, -32600, 'Message too large'));
      process.stdin.destroy();
      return;
    }
    let end;
    while ((end = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      if (!line.trim()) continue;
      queue = queue.then(async () => {
        let request;
        try { request = JSON.parse(line); }
        catch { emit(error(null, -32700, 'Parse error')); return; }
        try { emit(await relay(request, file)); }
        catch (failure) { emit(error(request?.id ?? null, -32000, failure.code || 'Desktop bridge unavailable')); }
      });
    }
  });
  process.stdin.on('end', () => queue.then(() => { process.exitCode = 0; }));
}

if (require.main === module) run().catch(() => {
  process.stderr.write('Agent Hub bridge could not start; check the desktop connection.\n');
  process.exitCode = 1;
});
module.exports = { run, dispatch };
