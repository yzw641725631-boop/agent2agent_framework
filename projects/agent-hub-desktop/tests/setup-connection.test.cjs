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

async function fixture(t) {
  const base = await fs.realpath(os.tmpdir()), root = await fs.mkdtemp(path.join(base, 'agent-hub-setup-'));
  const directory = path.join(root, 'data'); await fs.mkdir(directory);
  const user = { id: crypto.randomUUID(), platform_role: 'superadmin' }, deviceId = crypto.randomUUID();
  const profile = { origin: 'https://hub.example', login: 'root', user, deviceId, identity: identity(),
    sessions: { platform: { access_token: 'access', refresh_token: 'refresh', expires_at: new Date(Date.now() + 300000).toISOString() } },
    bindings: [], events: [], cursors: {} };
  const vault = { data: { current: 'one', profiles: { one: profile } }, save: async () => {}, pending: Promise.resolve() };
  const c = new Controller({ vault, directory, executable: process.execPath,
    bridgeScript: path.resolve(__dirname, '../core/bridge.cjs'), transport: async () => Response.json({ logged_out: true }) });
  const f = { c, root, directory, profile, user, role: 'superadmin', projects: [], grants: [], requests: [], operations: new Map() };
  c.connection = 'active'; c.watch = () => {};
  c.remote = { canonical: 'https://hub.example', close() {}, async request(method, api, body, options) {
    f.requests.push({ method, api, body, options });
    if (api === '/api/me') return { user: { ...user, platform_role: f.role }, device_id: deviceId };
    if (api === '/api/projects') return { items: f.projects };
    if (api === '/api/me/grants') return { items: f.grants };
    assert.equal(api, '/api/admin/projects');
    if (f.role !== 'superadmin') throw new HubError('SUPERADMIN_REQUIRED');
    if (!f.operations.has(options.idempotency)) {
      const project = { id: crypto.randomUUID(), ...body }; f.operations.set(options.idempotency, project); f.projects.push(project);
    }
    return f.operations.get(options.idempotency);
  } };
  f.remote = c.remote;
  c.context = async projectId => {
    const project = f.projects.find(item => item.id === projectId);
    if (!project) throw new HubError('PROJECT_NOT_ACCESSIBLE');
    return { ...project, capabilities: ['project:read', 'documents:read', 'tasks:write'] };
  };
  await c.broker.start(); await c.setup.enable();
  f.descriptor = JSON.parse(await fs.readFile(c.setup.file, 'utf8'));
  f.rpc = request => call(f.descriptor, { action: 'mcp', args: { request: { jsonrpc: '2.0', id: 1, ...request } } });
  f.tool = (name, args = {}) => f.rpc({ method: 'tools/call', params: { name, arguments: args } });
  t.after(async () => {
    await c.close(); const actual = await fs.realpath(root);
    assert.equal(path.dirname(actual), base); assert.match(path.basename(actual), /^agent-hub-setup-/);
    await fs.rm(actual, { recursive: true, force: true });
  });
  return f;
}

test('setup MCP is discoverable before login and binds to the first verified identity', async t => {
  const f = await fixture(t); await f.c.setup.disable();
  f.profile.sessions = {}; f.c.connection = 'signed_out'; await f.c.setup.enable();
  f.descriptor = JSON.parse(await fs.readFile(f.c.setup.file, 'utf8'));
  assert.equal(f.c.setup.binding.owner, null);
  const listed = await f.rpc({ method: 'tools/list' }); assert.equal(listed.result.tools.length, 4);
  const pending = (await f.tool('hub_connection_status')).result.structuredContent;
  assert.equal(pending.ready, false); assert.equal(pending.code, 'LOGIN_REQUIRED');
  f.profile.sessions.platform = {}; f.c.connection = 'active'; await f.c.setup.claim();
  assert.equal((await f.tool('hub_connection_status')).result.structuredContent.ready, true);
});

test('setup creates the first project with server root verification and idempotent retries', async t => {
  const f = await fixture(t), args = { slug: 'kms', name: 'KMS', operation_id: crypto.randomUUID() };
  const first = await f.tool('hub_create_project', args), second = await f.tool('hub_create_project', args);
  assert.equal(first.result.structuredContent.id, second.result.structuredContent.id);
  assert.equal(f.projects.length, 1);
  f.role = 'member';
  const denied = await f.tool('hub_create_project', { ...args, operation_id: crypto.randomUUID() });
  assert.equal(JSON.parse(denied.result.content[0].text).code, 'SUPERADMIN_REQUIRED');
  assert.equal(f.projects.length, 1);
});

test('member discovery derives project visibility from current grants rather than cached root role', async t => {
  const f = await fixture(t); f.role = 'member';
  const project = { project_id: crypto.randomUUID(), project_name: 'Only mine', project_slug: 'mine' };
  f.grants = [project];
  const result = (await f.tool('hub_list_projects')).result.structuredContent;
  assert.deepEqual(result.items, [{ id: project.project_id, name: project.project_name, slug: 'mine' }]);
  assert.equal(f.requests.filter(item => item.api === '/api/projects').length, 0);
});

test('connecting an existing folder writes one fixed project MCP and safely retries', async t => {
  const f = await fixture(t), project = { id: crypto.randomUUID(), name: 'One', slug: 'one' };
  f.projects.push(project);
  const workspace = path.join(f.root, '工程'); await fs.mkdir(workspace);
  const args = { project_id: project.id, workspace, harness: 'codex' };
  const first = (await f.tool('hub_connect_project', args)).result.structuredContent;
  assert.equal(first.project_id, project.id); assert.equal(first.reload_required, true);
  const before = await fs.readFile(path.join(workspace, '.codex/config.toml'), 'utf8');
  const retry = (await f.tool('hub_connect_project', args)).result.structuredContent;
  assert.equal(retry.binding_id, first.binding_id); assert.equal(f.profile.bindings.length, 1);
  assert.equal(await fs.readFile(path.join(workspace, '.codex/config.toml'), 'utf8'), before);
  const other = { id: crypto.randomUUID(), name: 'Two', slug: 'two' }; f.projects.push(other);
  const denied = await f.tool('hub_connect_project', { ...args, project_id: other.id });
  assert.equal(JSON.parse(denied.result.content[0].text).code, 'WORKSPACE_ALREADY_BOUND');
});

test('a local configuration conflict removes a newly created binding and preserves the file', async t => {
  const f = await fixture(t), project = { id: crypto.randomUUID(), name: 'One', slug: 'one' }; f.projects.push(project);
  const workspace = path.join(f.root, 'conflict'); await fs.mkdir(workspace);
  await fs.writeFile(path.join(workspace, '.mcp.json'), '{invalid');
  const result = await f.tool('hub_connect_project', { project_id: project.id, workspace, harness: 'claude' });
  assert.equal(JSON.parse(result.result.content[0].text).code, 'CONFIGURATION_CONFLICT');
  assert.equal(f.profile.bindings.length, 0);
  assert.equal(await fs.readFile(path.join(workspace, '.mcp.json'), 'utf8'), '{invalid');
});

test('logout, another account and disabling cannot reuse setup authorization', async t => {
  const f = await fixture(t); await f.c.logout();
  assert.equal((await f.tool('hub_connection_status')).result.structuredContent.ready, false);
  f.c.vault.data.profiles.two = { ...f.profile, user: { id: crypto.randomUUID() }, sessions: { platform: {} } };
  f.c.vault.data.current = 'two'; f.c.connection = 'active';
  f.c.remote = f.remote;
  assert.equal((await f.tool('hub_connection_status')).result.structuredContent.code, 'CONNECTION_ACCOUNT_MISMATCH');
  await f.c.setup.disable();
  await assert.rejects(f.rpc({ method: 'tools/list' }), { code: 'BINDING_NOT_FOUND' });
});

test('setup mutations wait for signed update approval before contacting the API', async t => {
  const f = await fixture(t), before = f.requests.length;
  f.c.updater = { check: async () => ({ can_continue: false, code: 'CLIENT_UPDATE_REQUIRED', message: 'updating' }) };
  const result = await f.tool('hub_list_projects');
  assert.equal(JSON.parse(result.result.content[0].text).code, 'CLIENT_UPDATE_REQUIRED');
  assert.equal(f.requests.length, before);
});

test('real stdio refuses an old connection ID after setup is reauthorized', async t => {
  const f = await fixture(t), previous = f.c.setup.binding.id;
  await f.c.setup.disable(); await f.c.setup.enable();
  const child = spawn(process.execPath, [f.c.bridgeScript, '--binding', f.c.setup.file, '--setup', '--connection-id', previous],
    { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  const chunks = []; child.stdout.on('data', chunk => chunks.push(chunk));
  const requests = [{ jsonrpc: '2.0', id: 1, method: 'tools/list' }, { jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'hub_connection_status', arguments: {} } }];
  child.stdin.end(requests.map(item => JSON.stringify(item)).join('\n') + '\n');
  const [code] = await once(child, 'exit'); assert.equal(code, 0);
  const results = Buffer.concat(chunks).toString().trim().split('\n').map(line => JSON.parse(line));
  assert.equal(results[0].result.tools.length, 5);
  assert.equal(results[1].result.structuredContent.ready, false);
  assert.equal(results[1].result.structuredContent.code, 'CONNECTION_CHANGED');
});
