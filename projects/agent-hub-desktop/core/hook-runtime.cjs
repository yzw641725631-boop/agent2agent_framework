'use strict';

const path = require('node:path');
const { HubError } = require('./remote.cjs');
const hooks = require('./host-hooks.cjs');
const installation = require('./hook-install.cjs');
const { digest } = require('./git-observer.cjs');
const samePath = (a, b) => process.platform === 'win32' ? path.resolve(a).toLowerCase() === path.resolve(b).toLowerCase() : path.resolve(a) === path.resolve(b);
const deny = reason => ({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: reason } });
class HookRuntime {
  constructor(controller) { this.c = controller; this.operations = new Map(); this.sessions = new Map(); }
  options(binding, harness) {
    return { workspace: binding.workspace, directory: path.join(this.c.directory, 'hook-installations', binding.id), harness,
      bindingId: binding.id, connection: Object.values(this.c.configuration(binding).mcpServers)[0] };
  }
  guard(binding, profile, remote) {
    this.c.ensureCurrent(profile, remote);
    if (this.c.closed || this.c.draining || this.c.connection !== 'active' || binding.status !== 'active' || binding.closeout_id)
      throw new HubError('PROJECT_ACCESS_REVOKED');
  }
  invoke(binding, action, args) {
    const key = binding.id + ':' + args.harness;
    const job = (this.operations.get(key) || Promise.resolve()).catch(() => {}).then(async () => {
      const p = this.c.profile, remote = this.c.remote, guard = () => this.guard(binding, p, remote);
      guard(); await this.c.invokeProjectRemote(binding, 'hub_get_project', {}); guard();
      const options = { ...this.options(binding, args.harness), guard };
      if (action === 'hub_install_hooks') {
        const result = await installation.install(options, args.preview_hash);
        if (p.hookActivity?.[binding.id]) delete p.hookActivity[binding.id][args.harness];
        await this.c.persist(); this.c.emitState(); return result;
      }
      if (action === 'hub_uninstall_hooks') {
        if (args.preview_hash !== hooks.hash('remove:' + binding.id + ':' + args.harness)) throw new HubError('HOOK_PREVIEW_STALE');
        return installation.uninstall({ workspace: binding.workspace, hookDirectory: options.directory, harness: args.harness, guard });
      }
      if (action === 'hub_get_hook_status') {
        const result = await installation.status(options);
        const activity = p.hookActivity?.[binding.id]?.[args.harness];
        return { ...result, state: result.configuration_matches && activity?.installation_id === result.installation_id ? 'observed' : result.state,
          activity: result.configuration_matches ? activity || null : null,
          uninstall_hash: hooks.hash('remove:' + binding.id + ':' + args.harness),
          system_write_isolation: false };
      }
      const plan = await installation.plan(options);
      return { preview_hash: plan.preview_hash, target: plan.target, configuration: plan.configuration,
        requires_host_trust: true, installation_is_activation: false, guard_mode: 'edits-and-restricted-commands',
        supported_events: hooks.spec(args.harness).events, source: hooks.spec(args.harness).source };
    });
    this.operations.set(key, job);
    return job.finally(() => { if (this.operations.get(key) === job) this.operations.delete(key); });
  }
  event(binding, input) {
    const key = digest({ binding_id: binding.id, harness: input?.harness, session_id: input?.session_id });
    const job = (this.sessions.get(key) || Promise.resolve()).catch(() => {}).then(() => this.processEvent(binding, input));
    this.sessions.set(key, job);
    return job.finally(() => { if (this.sessions.get(key) === job) this.sessions.delete(key); });
  }
  async processEvent(binding, input) {
    const c = this.c, p = c.profile, remote = c.remote;
    const allowed = ['harness', 'event', 'installation_id', 'session_id', 'cwd', 'turn_ref', 'stop_hook_active', 'operation'];
    if (!input || Object.keys(input).some(key => !allowed.includes(key)) ||
      !['SessionStart','SessionEnd','UserPromptSubmit','PreToolUse','BeforeMCPExecution','PostToolUse','Stop','Interrupt'].includes(input.event) ||
      input.event === 'BeforeMCPExecution' && input.harness !== 'cursor' ||
      typeof input.session_id !== 'string' || !input.session_id || input.session_id.length > 200 ||
      typeof input.cwd !== 'string' || !samePath(input.cwd, binding.workspace)) throw new HubError('INVALID_HOOK_INPUT');
    if (!p || c.connection === 'signed_out') throw new HubError('LOGIN_REQUIRED');
    const options = this.options(binding, input.harness), state = await installation.status(options);
    if (!state.configuration_matches || state.installation_id !== input.installation_id) throw new HubError('HOOK_CONFIGURATION_CHANGED');
    const byBinding = (p.hookActivity ??= {})[binding.id] ??= {};
    const activity = byBinding[input.harness] ??= { installation_id: input.installation_id, events: {} };
    activity.installation_id = input.installation_id;
    activity.events[input.event] = new Date().toISOString();
    await c.persist(); c.ensureCurrent(p, remote);
    const key = digest({ binding_id: binding.id, harness: input.harness, session_id: input.session_id });
    if (input.event === 'SessionStart') {
      const version = await c.updater?.check({ fresh: true });
      const existing = p.workSessions?.[key];
      const start = existing?.turn_id ? {hookSpecificOutput:{additionalContext:
        `本对话 session_key=${key}，turn_id=${existing.turn_id}。准备并确认任务后用 hub_bind_work_session 绑定。`}} :
        await c.turns.hook(binding, { event: 'UserPromptSubmit', harness: input.harness,
          session_id: input.session_id, cwd: input.cwd, stop_hook_active: false });
      return { hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext:
        version?.can_continue === false ? 'Agent Hub 版本尚未核验通过，暂停受管开发。' :
          'Agent Hub 钩子已实际触发。' + start.hookSpecificOutput.additionalContext } };
    }
    const session = p.workSessions?.[key];
    if (['PreToolUse', 'BeforeMCPExecution'].includes(input.event)) {
      try {
        this.guard(binding, p, remote);
        const op = input.operation;
        if (!op || !['read', 'mcp', 'edit', 'shell', 'unknown'].includes(op.kind)) throw new HubError('INVALID_HOOK_INPUT');
        const name = Object.keys(c.configuration(binding).mcpServers)[0];
        const ownPrefixes = [name, name.replaceAll('-', '_')].map(server => 'mcp__' + server + '__hub_');
        if (input.event === 'BeforeMCPExecution') return op.kind === 'mcp' &&
          [name, name.replaceAll('-', '_')].includes(op.server) &&
          (/^hub_[a-z_]+$/.test(op.name || '') || ownPrefixes.some(prefix => op.name?.startsWith(prefix))) ? {} :
          deny('该 MCP 调用不是当前项目的受管连接，无法核验写入范围。');
        // Cursor's general event omits the server identity. The separately installed,
        // fail-closed beforeMCPExecution event validates it for every MCP invocation.
        if (input.harness === 'cursor' && op.kind === 'mcp' && op.name?.startsWith('MCP:')) return {};
        if (op.kind === 'mcp' && ownPrefixes.some(prefix => op.name?.startsWith(prefix))) return {};
        if (op.kind === 'read' || op.kind === 'shell' && op.safe_read === true) return {};
        if (!session?.run_id) return deny(`本对话尚未准备并绑定任务（session_key=${key}，turn_id=${session?.turn_id || '尚未收到轮次事件'}）。请完成 hub_prepare_work、hub_acknowledge_work 和 hub_bind_work_session。`);
        const run = p.workRuns?.[session.run_id];
        if (!run || run.stopped || session.phase !== 'active') return deny('当前执行已停止，请核对任务、授权和租约。');
        if (op.kind !== 'edit') return deny('该调用无法证明符合禁改范围，已停止。请使用可核验路径的编辑工具或登记的受管执行入口。');
        if (!Array.isArray(op.paths) || !op.paths.length || op.paths.length > 100) throw new HubError('INVALID_HOOK_INPUT');
        const proposed_paths = [];
        for (const file of op.paths) {
          if (typeof file !== 'string' || file.length > 4096 || /[\x00-\x1f]/.test(file)) throw new HubError('INVALID_HOOK_INPUT');
          const target = path.resolve(binding.workspace, file), relative = path.relative(binding.workspace, target);
          if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) throw new HubError('OUTSIDE_FOCUS_SCOPE');
          if (['.git','.codex','.claude','.cursor','.qoder','.codebuddy','.agent-hub'].includes(relative.split(path.sep)[0].toLowerCase()))
            throw new HubError('FRAMEWORK_PATH_PROTECTED');
          await require('./host-install.cjs').safePath(target);
          proposed_paths.push(relative.split(path.sep).join('/'));
        }
        const version = await c.updater?.check({ fresh: true });
        if (version?.can_continue === false) throw new HubError('CLIENT_UPDATE_REQUIRED');
        await c.work.check(binding, { task_id: run.task_id, run_id: run.run_id, lease_epoch: run.lease_epoch, proposed_paths });
        return {};
      } catch (error) { return deny('Agent Hub 修改前检查未通过：' + (error.code || 'GUARD_UNAVAILABLE') + '。保留现有工作，处理后再继续。'); }
    }
    if (input.event === 'PostToolUse' || input.event === 'SessionEnd') {
      if (session?.run_id) {
        session.pending_interrupt = true;
        if (input.event === 'SessionEnd') session.phase = 'interrupted';
        await c.persist(); c.turns.start();
      }
      return input.event === 'PostToolUse' && session?.turn_id ? { hookSpecificOutput: {
        hookEventName: 'PostToolUse', additionalContext: `Agent Hub 本对话 session_key=${key}，本轮 turn_id=${session.turn_id}。` } } : {};
    }
    return c.turns.hook(binding, { event: input.event, harness: input.harness, session_id: input.session_id,
      cwd: input.cwd, ...(input.turn_ref ? { turn_ref: input.turn_ref } : {}), stop_hook_active: input.stop_hook_active });
  }
}
module.exports = { HookRuntime, deny };
