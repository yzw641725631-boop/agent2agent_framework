'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const { HubError } = require('./remote.cjs');
const { dispatch } = require('./mcp.cjs');
const { equalSecret } = require('./broker.cjs');
const uuid = value => typeof value === 'string' && /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);

class Management {
  constructor(controller) { this.controller = controller; }
  get binding() { return this.controller.profile?.management; }
  owns(id) { return this.binding?.id === id; }

  async identity() {
    const c = this.controller, profile = c.profile, remote = c.remote;
    if (c.closed || !profile?.sessions.platform || !remote || !['active', 'offline'].includes(c.connection)) {
      throw new HubError('LOGIN_REQUIRED');
    }
    if (c.connection === 'offline') throw new HubError('NETWORK_UNAVAILABLE');
    try {
      const me = await remote.request('GET', '/api/me', undefined, { slot: 'platform' });
      c.ensureCurrent(profile, remote);
      if (me.user?.platform_role !== 'superadmin') throw new HubError('SUPERADMIN_REQUIRED', '只有超级管理员可以使用项目管理 MCP。');
      if (me.user.id !== profile.user?.id || me.device_id !== profile.deviceId) throw new HubError('LOGIN_REQUIRED');
      return { profile, remote, me };
    } catch (error) { await c.failure(error, { profile, remote, platform: true }); throw error; }
  }

  matches(binding, identity) {
    return this.binding === binding && binding.userId === identity.me.user.id &&
      binding.deviceId === identity.me.device_id;
  }

  async configuration() {
    const identity = await this.identity(), c = this.controller;
    let binding = this.binding;
    if (!binding || !this.matches(binding, identity)) {
      if (binding) await fs.unlink(c.descriptorPath(binding)).catch(error => { if (error.code !== 'ENOENT') throw error; });
      binding = { kind: 'management', id: crypto.randomUUID(), secret: crypto.randomBytes(32).toString('base64url'),
        userId: identity.me.user.id, deviceId: identity.me.device_id };
      identity.profile.management = binding;
      await c.persist();
    }
    c.ensureCurrent(identity.profile, identity.remote);
    await c.writeDescriptor(binding);
    c.emitState();
    return c.configuration(binding);
  }

  async restore() {
    const c = this.controller, profile = c.profile, binding = this.binding;
    if (!binding) return;
    if (profile.user?.platform_role === 'superadmin' && profile.user.id === binding.userId && profile.deviceId === binding.deviceId) {
      await c.writeDescriptor(binding);
    } else await fs.unlink(c.descriptorPath(binding)).catch(error => { if (error.code !== 'ENOENT') throw error; });
  }

  async disable() {
    const c = this.controller, binding = this.binding;
    if (binding) {
      delete c.profile.management;
      await c.persist();
      await fs.unlink(c.descriptorPath(binding)).catch(error => { if (error.code !== 'ENOENT') throw error; });
    }
    c.emitState(); return { disabled: true };
  }

  async dispatch(request) {
    const c = this.controller, binding = this.binding;
    if (!binding || !equalSecret(request.secret, binding.secret)) throw new HubError('BRIDGE_AUTHENTICATION_FAILED');
    const check = c.updater ? () => c.updater.check({ fresh: true }) : undefined;
    if (request.action === 'mcp') return dispatch(request.args.request,
      (action, args) => this.invoke(binding, action, args), check, { scope: 'management', omitToolText: true });
    if (request.action === 'hub_check_version' && check && !Object.keys(request.args).length) return check();
    return this.invoke(binding, request.action, request.args);
  }

  async invoke(binding, action, args) {
    const c = this.controller;
    if (c.draining) throw new HubError('CLIENT_UPDATE_REQUIRED');
    if (action !== 'metadata' && c.updater) {
      const state = await c.updater.check();
      if (!state.can_continue) throw new HubError(state.code, state.message, { update: state });
    }
    if (c.draining) throw new HubError('CLIENT_UPDATE_REQUIRED');
    const operation = this.request(binding, action, args);
    c.projectOperations.add(operation);
    try { return await operation; } finally { c.projectOperations.delete(operation); }
  }

  async request(binding, action, args) {
    const c = this.controller, identity = await this.identity();
    if (!this.matches(binding, identity)) throw new HubError('LOGIN_REQUIRED');
    const { profile, remote } = identity;
    try {
      if (action === 'metadata') return { scope: 'management', capabilities: ['projects:list', 'projects:create'] };
      if (action === 'hub_list_projects') {
        if (Object.keys(args).length) throw new HubError('INVALID_REQUEST');
        const result = await remote.request('GET', '/api/projects', undefined, { slot: 'platform' });
        c.ensureCurrent(profile, remote);
        if (!this.matches(binding, identity)) throw new HubError('LOGIN_REQUIRED');
        c.projects = result.items; c.emitState(); return result;
      }
      if (action !== 'hub_create_project') throw new HubError('UNKNOWN_BRIDGE_ACTION');
      if (Object.keys(args).some(key => !['slug', 'name', 'operation_id'].includes(key)) ||
          !uuid(args.operation_id) || typeof args.slug !== 'string' || !/^[a-z][a-z0-9-]{1,79}$/.test(args.slug) ||
          typeof args.name !== 'string' || !args.name.trim() || args.name.length > 200) throw new HubError('INVALID_REQUEST');
      const project = await remote.request('POST', '/api/admin/projects', { slug: args.slug, name: args.name.trim() },
        { slot: 'platform', idempotency: args.operation_id });
      c.ensureCurrent(profile, remote);
      if (!this.matches(binding, identity)) throw new HubError('LOGIN_REQUIRED');
      c.projects = [...c.projects.filter(item => item.id !== project.id), project].sort((a, b) => a.slug.localeCompare(b.slug));
      c.emitState(); return project;
    } catch (error) { await c.failure(error, { profile, remote, platform: true }); throw error; }
  }
}

module.exports = { Management };
