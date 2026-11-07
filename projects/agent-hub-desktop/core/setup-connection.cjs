'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const { HubError } = require('./remote.cjs');
const { equalSecret } = require('./broker.cjs');
const { dispatch } = require('./mcp.cjs');
const { directory } = require('./workspaces.cjs');
const hostInstall = require('./host-install.cjs');
const { version } = require('../package.json');
const uuid = value => typeof value === 'string' && /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);

class SetupConnection {
  constructor(controller) { this.controller = controller; this.operations = Promise.resolve(); }
  get binding() { return this.controller.vault.data.setupConnection; }
  get file() { return path.join(this.controller.directory, 'setup-mcp.json'); }
  owns(id) { return this.binding?.id === id; }
  matches(binding, profile = this.controller.profile) {
    return this.binding === binding && binding.owner && profile?.user?.id === binding.owner.userId &&
      profile.deviceId === binding.owner.deviceId && profile.origin === binding.owner.origin;
  }
  async enable() {
    const c = this.controller;
    if (!this.binding) {
      c.vault.data.setupConnection = { kind: 'setup', id: crypto.randomUUID(),
        secret: crypto.randomBytes(32).toString('base64url'), owner: null };
      await c.persist();
    }
    await this.claim(); await this.restore(); c.emitState();
    return { enabled: true, connection_id: this.binding.id };
  }
  async claim() {
    const c = this.controller, binding = this.binding, p = c.profile;
    if (binding && !binding.owner && c.connection === 'active' && p?.sessions.platform && p.user?.id && p.deviceId) {
      binding.owner = { userId: p.user.id, deviceId: p.deviceId, origin: p.origin };
      await c.persist();
    }
  }
  async restore() {
    if (!this.binding) return;
    const c = this.controller, binding = this.binding;
    await hostInstall.replace(this.file, Buffer.from(JSON.stringify({ version: 1, protocol: 'desktop-mcp-v1',
      endpoint: c.broker.endpoint, binding: binding.id, secret: binding.secret, scope: 'setup' })));
  }
  async disable() {
    delete this.controller.vault.data.setupConnection;
    await this.controller.persist();
    await fs.unlink(this.file).catch(error => { if (error.code !== 'ENOENT') throw error; });
    this.controller.emitState(); return { disabled: true };
  }
  async consumeRequest() {
    const requestFile = path.join(this.controller.directory, 'setup-request.json');
    const bytes = await hostInstall.snapshot(requestFile);
    if (!bytes) return false;
    const request = JSON.parse(bytes.toString('utf8').replace(/^\ufeff/, ''));
    if (request.version !== 1 || !uuid(request.request_id)) throw new HubError('INVALID_SETUP_REQUEST');
    await this.enable();
    const current = await hostInstall.snapshot(requestFile);
    if (current?.equals(bytes)) await fs.unlink(requestFile);
    return true;
  }
  async identity(binding) {
    const c = this.controller, profile = c.profile, remote = c.remote;
    if (c.closed || !profile?.sessions.platform || !remote || !['active', 'offline'].includes(c.connection)) throw new HubError('LOGIN_REQUIRED', '请在 Agent Hub 桌面端登录并完成设备验证。');
    if (!this.matches(binding, profile)) throw new HubError('CONNECTION_ACCOUNT_MISMATCH', '此安装连接属于另一账号或设备。请在桌面设置重新授权，然后重新运行安装配置。');
    if (c.connection === 'offline') throw new HubError('NETWORK_UNAVAILABLE');
    try {
      const me = await remote.request('GET', '/api/me', undefined, { slot: 'platform' });
      c.ensureCurrent(profile, remote);
      if (!this.matches(binding, profile) || me.user.id !== binding.owner.userId || me.device_id !== binding.owner.deviceId) throw new HubError('LOGIN_REQUIRED');
      profile.user = me.user;
      return { profile, remote, me };
    } catch (error) { await c.failure(error, { profile, remote, platform: true }); throw error; }
  }
  async status(binding) {
    const c = this.controller;
    const result = { desktop_version: version, connection: c.connection, ready: false };
    try {
      const { me, profile } = await this.identity(binding);
      return { ...result, ready: true, code: 'CONNECTED', login: profile.login, role: me.user.platform_role,
        message: '安装连接已就绪。可列出授权项目，超管可创建项目；开发前用 hub_connect_project 建立固定项目连接。' };
    } catch (error) { return { ...result, code: error.code || 'LOGIN_REQUIRED', message: error.message }; }
  }
  async dispatch(request) {
    const binding = this.binding, c = this.controller;
    if (!binding || !equalSecret(request.secret, binding.secret)) throw new HubError('BRIDGE_AUTHENTICATION_FAILED');
    const check = c.updater ? () => c.updater.check({ fresh: true }) : undefined;
    if (request.action === 'mcp') return dispatch(request.args.request,
      (action, args) => this.invoke(binding, action, args), check, { scope: 'setup', omitToolText: true });
    if (request.action === 'hub_check_version' && check && !Object.keys(request.args).length) return check();
    return this.invoke(binding, request.action, request.args);
  }
  async invoke(binding, action, args) {
    if (action === 'metadata') return { scope: 'setup', capabilities: ['setup'] };
    if (action === 'hub_connection_status') {
      if (Object.keys(args).length) throw new HubError('INVALID_REQUEST');
      return this.status(binding);
    }
    const c = this.controller;
    if (c.draining) throw new HubError('CLIENT_UPDATE_REQUIRED');
    if (c.updater) {
      const update = await c.updater.check();
      if (!update.can_continue) throw new HubError(update.code, update.message, { update });
    }
    if (c.draining) throw new HubError('CLIENT_UPDATE_REQUIRED');
    const operation = this.request(binding, action, args);
    c.projectOperations.add(operation);
    try { return await operation; } finally { c.projectOperations.delete(operation); }
  }
  async list(binding, identity) {
    const c = this.controller, { profile, remote, me } = identity;
    let items, currentGrants;
    try {
      if (me.user.platform_role === 'superadmin') items = (await remote.request('GET', '/api/projects', undefined, { slot: 'platform' })).items;
      else {
        const grants = (await remote.request('GET', '/api/me/grants', undefined, { slot: 'platform' })).items;
        currentGrants = grants;
        items = [...new Map(grants.map(grant => [grant.project_id,
          { id: grant.project_id, name: grant.project_name, slug: grant.project_slug }])).values()];
      }
      c.ensureCurrent(profile, remote);
      if (!this.matches(binding, profile)) throw new HubError('LOGIN_REQUIRED');
      if (currentGrants) c.grants = currentGrants;
      c.projects = items; c.emitState(); return { items };
    } catch (error) { await c.failure(error, { profile, remote, platform: true }); throw error; }
  }
  async request(binding, action, args) {
    const identity = await this.identity(binding), c = this.controller;
    if (action === 'hub_list_projects') {
      if (Object.keys(args).length) throw new HubError('INVALID_REQUEST');
      return this.list(binding, identity);
    }
    if (action === 'hub_create_project') {
      if (identity.me.user.platform_role !== 'superadmin') throw new HubError('SUPERADMIN_REQUIRED');
      await c.management.configuration();
      if (!this.matches(binding, identity.profile)) throw new HubError('LOGIN_REQUIRED');
      return c.management.invoke(c.management.binding, action, args);
    }
    if (action !== 'hub_connect_project') throw new HubError('UNKNOWN_BRIDGE_ACTION');
    if (Object.keys(args).some(key => !['project_id', 'workspace', 'harness'].includes(key)) ||
        !uuid(args.project_id) || typeof args.workspace !== 'string' || !hostInstall.hosts.includes(args.harness)) throw new HubError('INVALID_REQUEST');
    const operation = this.operations.then(async () => {
      await this.identity(binding); await this.list(binding, identity);
      const workspace = await directory(args.workspace);
      let project = identity.profile.bindings.find(item => item.workspace === workspace), created = false;
      if (project && project.projectId !== args.project_id) throw new HubError('WORKSPACE_ALREADY_BOUND');
      if (!project) {
        await c.bind({ projectId: args.project_id, workspace }); created = true;
        project = identity.profile.bindings.find(item => item.workspace === workspace);
      }
      try {
        await c.context(args.project_id); c.ensureCurrent(identity.profile, identity.remote);
        if (!this.matches(binding, identity.profile) || c.binding(project.id) !== project) throw new HubError('LOGIN_REQUIRED');
        const config = c.configuration(project).mcpServers, name = Object.keys(config)[0];
        const installed = await hostInstall.install({ hosts: [args.harness], workspace,
          receiptDirectory: path.join(c.directory, 'setup-projects', project.id), name, entry: config[name],
          guard: () => {
            c.ensureCurrent(identity.profile, identity.remote);
            if (!this.matches(binding, identity.profile) || c.connection !== 'active' || c.binding(project.id) !== project) throw new HubError('LOGIN_REQUIRED');
          } });
        return { project_id: project.projectId, binding_id: project.id, workspace, ...installed,
          message: '项目连接已写入当前工程的 MCP 配置。请重新加载开发工具并完成其项目信任确认，然后使用该项目连接读取文档和上报进度。' };
      } catch (error) {
        if (created && c.profile === identity.profile) await c.disconnect(project.id).catch(() => {});
        throw error;
      }
    });
    this.operations = operation.catch(() => {}); return operation;
  }
}

module.exports = { SetupConnection };
