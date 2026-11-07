'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const { Controller } = require('../core/controller.cjs');
const { call } = require('../core/broker.cjs');
const { identity, HubError } = require('../core/remote.cjs');
const { dispatch } = require('../core/mcp.cjs');

async function fixture(t) {
  const base = await fs.realpath(os.tmpdir());
  const directory = await fs.mkdtemp(path.join(base, 'agent-hub-management-'));
  const userId = crypto.randomUUID(), deviceId = crypto.randomUUID();
  const profile = { origin: 'https://hub.example', sessions: { platform: { access_token: 'access',
    refresh_token: 'refresh', expires_at: new Date(Date.now() + 300000).toISOString() } },
  identity: identity(), bindings: [], events: [], cursors: {}, deviceId,
  user: { id: userId, platform_role: 'superadmin' } };
  const vault = { data: { current: 'one', profiles: { one: profile } }, save: async () => {}, pending: Promise.resolve() };
  const c = new Controller({ vault, directory, executable: process.execPath,
    bridgeScript: path.resolve(__dirname, '../core/bridge.cjs'),
    transport: async () => Response.json({ logged_out: true }) });
  const f = { c, profile, directory, role: 'superadmin', projects: [], requests: [], operations: new Map() };
  c.connection = 'active';
  c.remote = { canonical: 'https://hub.example', close() {}, async request(method, api, body, options) {
    f.requests.push({ method, api, body, options });
    if (api === '/api/me') return { user: { id: userId, platform_role: f.role }, device_id: deviceId };
    if (api === '/api/projects') return { items: f.projects };
    assert.equal(api, '/api/admin/projects'); assert.equal(method, 'POST');
    if (f.role !== 'superadmin') throw new HubError('SUPERADMIN_REQUIRED');
    const key = options.idempotency;
    assert.match(key, /^[a-f0-9-]{36}$/);
    if (!f.operations.has(key)) {
      const project = { id: crypto.randomUUID(), ...body };
      f.operations.set(key, project); f.projects.push(project);
    }
    return f.operations.get(key);
  } };
  await c.broker.start();
  t.after(async () => {
    await c.close();
    const actual = await fs.realpath(directory);
    assert.equal(path.dirname(actual), base); assert.match(path.basename(actual), /^agent-hub-management-/);
    await fs.rm(actual, { recursive: true, force: true });
  });
  f.enable = async () => {
    const config = await c.management.configuration();
    const entry = Object.values(config.mcpServers)[0];
    f.descriptor = JSON.parse(await fs.readFile(entry.args[2], 'utf8'));
    return config;
  };
  f.rpc = request => call(f.descriptor, { action: 'mcp', args: { request: { jsonrpc: '2.0', id: 1, ...request } } });
  f.create = args => f.rpc({ method: 'tools/call', params: { name: 'hub_create_project', arguments: args } });
  return f;
}

test('management MCP creates the first project with stable retry identity and no workspace binding', async t => {
  const f = await fixture(t), config = await f.enable();
  assert.match(Object.keys(config.mcpServers)[0], /^agent-hub-management-/);
  assert.equal(f.profile.bindings.length, 0);
  assert.equal(f.c.projects.length, 0);
  const listed = await f.rpc({ method: 'tools/list' });
  assert.deepEqual(listed.result.tools.map(tool => tool.name), ['hub_list_projects', 'hub_create_project']);
  const args = { slug: 'kms', name: '知识管理', operation_id: crypto.randomUUID() };
  const first = await f.create(args), retried = await f.create(args);
  assert.equal(first.result.structuredContent.id, retried.result.structuredContent.id);
  assert.equal(f.projects.length, 1); assert.equal(f.c.projects.length, 1);
  const post = f.requests.filter(r => r.method === 'POST');
  assert.equal(post.length, 2);
  assert.equal(post[0].options.idempotency, args.operation_id);
  assert.deepEqual(post[0].body, { slug: 'kms', name: '知识管理' });
  assert.ok(!JSON.stringify(config).includes('access_token'));
});

test('cached root identity does not permit management after the server removes root status', async t => {
  const f = await fixture(t); await f.enable(); f.role = 'member';
  const denied = await f.create({ slug: 'forbidden', name: 'Forbidden', operation_id: crypto.randomUUID() });
  assert.equal(denied.result.isError, true);
  assert.equal(JSON.parse(denied.result.content[0].text).code, 'SUPERADMIN_REQUIRED');
  await assert.rejects(f.c.management.configuration(), { code: 'SUPERADMIN_REQUIRED' });
  assert.equal(f.requests.filter(r => r.method === 'POST').length, 0);
});

test('project MCP has no management tool and cannot use its binding secret to create projects', async t => {
  const f = await fixture(t); await f.enable();
  const binding = { id: crypto.randomUUID(), projectId: crypto.randomUUID(), secret: 'project-secret',
    workspace: f.directory, status: 'active' };
  f.profile.bindings.push(binding);
  f.c.context = async () => ({ capabilities: ['project:read', 'projects:create'] });
  const request = { jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name: 'hub_create_project',
    arguments: { slug: 'wrong-connection', name: 'Wrong', operation_id: crypto.randomUUID() } } };
  const result = await dispatch(request, async () => { throw new Error('Should not invoke'); });
  assert.equal(result.error.code, -32602);
  await assert.rejects(call({ binding: binding.id, secret: binding.secret, endpoint: f.c.broker.endpoint }, { action: 'hub_create_project',
    args: request.params.arguments }), { code: 'UNKNOWN_BRIDGE_ACTION' });
  assert.equal(f.requests.filter(r => r.method === 'POST').length, 0);
});

test('logout blocks management and disabling it invalidates its old descriptor and secret', async t => {
  const f = await fixture(t); await f.enable();
  const descriptorFile = f.c.descriptorPath(f.c.management.binding);
  await f.c.logout();
  const denied = await f.create({ slug: 'after-logout', name: 'After', operation_id: crypto.randomUUID() });
  assert.equal(JSON.parse(denied.result.content[0].text).code, 'LOGIN_REQUIRED');
  await assert.rejects(fs.access(descriptorFile), { code: 'ENOENT' });
  await f.c.management.disable();
  await assert.rejects(f.rpc({ method: 'tools/list' }), { code: 'BINDING_NOT_FOUND' });
});

test('management update gate blocks mutations before any server request and rejects extra creation fields', async t => {
  const f = await fixture(t); await f.enable();
  f.c.updater = { check: async () => ({ can_continue: false, code: 'CLIENT_UPDATE_REQUIRED', message: 'updating' }) };
  const before = f.requests.length;
  const denied = await f.create({ slug: 'waiting', name: 'Waiting', operation_id: crypto.randomUUID() });
  assert.equal(JSON.parse(denied.result.content[0].text).code, 'CLIENT_UPDATE_REQUIRED');
  assert.equal(f.requests.length, before);
  f.c.updater = null;
  const invalid = await f.create({ slug: 'bad', name: 'Bad', operation_id: crypto.randomUUID(), user_id: 'other' });
  assert.equal(JSON.parse(invalid.result.content[0].text).code, 'INVALID_REQUEST');
  assert.equal(f.projects.length, 0);
});

test('real stdio management bridge discovers management tools before any project exists', async t => {
  const f = await fixture(t), config = await f.enable(), entry = Object.values(config.mcpServers)[0];
  const child = spawn(process.execPath, entry.args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const chunks = [], errors = [];
  child.stdout.on('data', value => chunks.push(value)); child.stderr.on('data', value => errors.push(value));
  const requests = [
    { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25' } },
    { jsonrpc: '2.0', id: 2, method: 'tools/list' },
    { jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'hub_create_project',
      arguments: { slug: 'first', name: 'First', operation_id: crypto.randomUUID() } } },
  ];
  child.stdin.end(requests.map(value => JSON.stringify(value)).join('\n') + '\n');
  const [code] = await once(child, 'exit');
  assert.equal(code, 0, Buffer.concat(errors).toString());
  const messages = Buffer.concat(chunks).toString().trim().split('\n').map(line => JSON.parse(line));
  assert.match(messages[0].result.instructions, /项目管理连接/);
  assert.deepEqual(messages[1].result.tools.map(tool => tool.name), ['hub_list_projects', 'hub_create_project']);
  assert.equal(messages[2].result.structuredContent.slug, 'first');
  assert.ok(messages[2].result.content[0].text.includes('first'));
});

test('disabling the management binding during identity verification prevents the pending write', async t => {
  const f = await fixture(t); await f.enable();
  const request = f.c.remote.request;
  let resume, entered;
  const started = new Promise(resolve => { entered = resolve; });
  const blocked = new Promise(resolve => { resume = resolve; });
  f.c.remote.request = async (...args) => {
    if (args[1] === '/api/me') { entered(); await blocked; }
    return request(...args);
  };
  const pending = f.create({ slug: 'disabled', name: 'Disabled', operation_id: crypto.randomUUID() });
  await started;
  await f.c.management.disable(); resume();
  const result = await pending;
  assert.equal(JSON.parse(result.result.content[0].text).code, 'LOGIN_REQUIRED');
  assert.equal(f.requests.filter(r => r.method === 'POST').length, 0);
});

test('a management descriptor for one account is unusable after switching to another profile', async t => {
  const f = await fixture(t); await f.enable();
  f.c.vault.data.profiles.two = { sessions: { platform: {} }, user: { id: crypto.randomUUID(), platform_role: 'superadmin' },
    bindings: [], events: [], cursors: {}, deviceId: crypto.randomUUID() };
  f.c.vault.data.current = 'two';
  await assert.rejects(f.rpc({ method: 'tools/list' }), { code: 'BINDING_NOT_FOUND' });
  assert.equal(f.projects.length, 0);
});
