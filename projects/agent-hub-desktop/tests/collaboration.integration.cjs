'use strict';

// Explicitly opt-in: real Windows desktop code and isolated HTTP/PostgreSQL.
// Lifecycle payloads are injected by this driver; no host or model is launched.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { Controller } = require('../core/controller.cjs');
const { Remote } = require('../core/remote.cjs');
const { Vault } = require('../core/vault.cjs');
const observer = require('../core/git-observer.cjs');
const execute = promisify(execFile);
const origin = 'http://127.0.0.1:18768';
const bridge = path.resolve(__dirname, '../core/bridge.cjs');

class McpClient {
  constructor(connection) {
    this.pending = new Map();
    this.sequence = 0;
    this.child = spawn(connection.command, connection.args, {
      env: { ...process.env, ...connection.env }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.exit = new Promise(resolve => this.child.once('close', resolve));
    this.stderr = '';
    this.child.stderr.on('data', bytes => { this.stderr = (this.stderr + bytes).slice(-4096); });
    let buffered = '';
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', chunk => {
      buffered += chunk;
      let end;
      while ((end = buffered.indexOf('\n')) >= 0) {
        const line = buffered.slice(0, end); buffered = buffered.slice(end + 1);
        if (!line.trim()) continue;
        let response;
        try { response = JSON.parse(line); } catch { this.fail(new Error('Invalid MCP JSON')); continue; }
        const waiting = this.pending.get(response.id);
        if (waiting) { this.pending.delete(response.id); clearTimeout(waiting.timer); waiting.resolve(response); }
      }
    });
    this.child.on('error', error => this.fail(error));
    this.child.on('close', () => this.fail(new Error('MCP bridge exited')));
  }
  fail(error) {
    for (const row of this.pending.values()) { clearTimeout(row.timer); row.reject(error); }
    this.pending.clear();
  }
  request(method, params) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('MCP response timed out')); }, 30000);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }
  async invoke(name, args = {}) {
    const response = await this.request('tools/call', { name, arguments: args });
    if (response.error) throw Object.assign(new Error(response.error.message), { code: 'MCP_PROTOCOL_ERROR' });
    if (response.result.isError) {
      const details = JSON.parse(response.result.content[0].text);
      throw Object.assign(new Error(details.message), { code: details.code });
    }
    return response.result.structuredContent || JSON.parse(response.result.content[0].text);
  }
  async close() {
    this.child.stdin.end();
    let timer;
    try { await Promise.race([this.exit, new Promise(resolve => { timer = setTimeout(() => { this.child.kill(); resolve(); }, 3000); })]); }
    finally { clearTimeout(timer); }
  }
}

async function run({ protector, protectorName }) {
  const started = Date.now();
  assert.equal(process.platform, 'win32', 'This driver records Windows evidence only');
  assert.equal(process.env.HUB_TEST_HTTP, 'collaboration-only');
  assert.ok(process.env.HUB_TEST_FIXTURE_KEY?.length >= 40);
  assert.ok(await protector.isAsyncEncryptionAvailable());
  assert.equal((await fetch(origin + '/__test__/seed')).status, 404);
  const response = await fetch(origin + '/__test__/seed', {
    headers: { 'X-Hub-Fixture-Key': process.env.HUB_TEST_FIXTURE_KEY },
  });
  assert.equal(response.status, 200);
  const seed = await response.json();
  assert.equal(seed.fixture_kind, 'isolated-collaboration-v1');
  for (const profile of [seed.root, seed.author, seed.colleague, seed.restricted]) assert.equal(profile.origin, origin);
  assert.notEqual(seed.author.user.id, seed.colleague.user.id);
  assert.notEqual(seed.author.deviceId, seed.colleague.deviceId);
  const parent = await fs.realpath(os.tmpdir());
  const temporary = await fs.mkdtemp(path.join(parent, 'hub-collaboration-'));
  const actors = [], root = new Remote(seed.root, async () => {});
  const checkpoints = [];
  const evidence = { passed: false, started_at: new Date(started).toISOString(), fixture: seed.fixture_kind, platform: process.platform,
    desktop_version: require('../package.json').version, electron_version: process.versions.electron || null,
    node_version: process.versions.node, protector: protectorName,
    physical_windows_computers: 1, synthetic_members: 2, synthetic_device_identities: 2,
    repositories: ['web', 'api'], harness_protocols: ['codex', 'claude-code'],
    actual_host_launches: 0, actual_model_calls: 0, injected_lifecycle_payloads: true,
    checks: {}, checkpoints };
  const check = (name, condition = true) => { assert.ok(condition, name); evidence.checks[name] = true; };
  try {
    await root.init();
    const base = `/api/projects/${seed.project_id}`;
    async function actor(profile, name, harness) {
      const directory = path.join(temporary, name + '-desktop');
      const vault = new Vault(directory, protector); await vault.load();
      vault.data = { version: 1, current: 'fixture', profiles: { fixture: profile } };
      for (const slug of ['web', 'api']) {
        const workspace = path.join(temporary, name + '-' + slug);
        await fs.mkdir(path.join(workspace, 'src'), { recursive: true });
        await fs.mkdir(path.join(workspace, 'tests'));
        await fs.writeFile(path.join(workspace, 'src/main.ts'), 'baseline\n');
        await fs.writeFile(path.join(workspace, 'src/remove.ts'), 'remove me\n');
        await fs.writeFile(path.join(workspace, 'tests/baseline.txt'), 'baseline test\n');
        const git = args => execute('git', ['-C', workspace, ...args], { windowsHide: true });
        await git(['init', '-q']); await git(['config', 'core.autocrlf', 'false']);
        await git(['remote', 'add', 'origin', seed.repositories[slug].repository_url]);
        await git(['add', '.']);
        await git(['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org', 'commit', '-qm', 'baseline']);
        profile.bindings.push({ id: crypto.randomUUID(), projectId: seed.project_id,
          repositoryId: seed.repositories[slug].id, workspace, status: 'active',
          secret: crypto.randomBytes(32).toString('base64url'),
          initial_commit: (await observer.initialize(workspace)).base_commit });
      }
      profile.cursors = {};
      await vault.save();
      const value = { directory, harness, clients: new Map(), online: true };
      actors.push(value);
      value.start = async () => {
        const loaded = new Vault(directory, protector); await loaded.load();
        const c = value.c = new Controller({ vault: loaded, directory, executable: process.execPath,
          bridgeScript: bridge, recoveryDelay: 60000 });
        c.remote = new Remote(c.profile, () => c.persist(), (...args) => {
          if (!value.online) return Promise.reject(new Error('Injected offline transport'));
          return fetch(...args);
        });
        await c.remote.init(); c.connection = 'active'; await c.broker.start();
        for (const binding of c.profile.bindings) {
          binding.status = 'active'; await c.writeDescriptor(binding);
          const client = new McpClient(Object.values(c.configuration(binding).mcpServers)[0]);
          value.clients.set(binding.repositoryId, client);
          const initialized = await client.request('initialize', { protocolVersion: '2025-11-25',
            capabilities: {}, clientInfo: { name: 'isolated-collaboration-driver', version: '1' } });
          assert.equal(initialized.result.serverInfo.name, 'agent-hub-desktop');
        }
        await c.persist();
      };
      value.binding = slug => value.c.profile.bindings.find(b => b.repositoryId === seed.repositories[slug].id);
      value.invoke = (slug, tool, args) => value.clients.get(seed.repositories[slug].id).invoke(tool, args);
      value.stop = async () => {
        await Promise.all([...value.clients.values()].map(client => client.close())); value.clients.clear();
        await value.c.close();
      };
      await value.start(); return value;
    }
    const a = await actor(seed.author, 'author', 'codex');
    const b = await actor(seed.colleague, 'colleague', 'claude-code');
    const lease = (task, result) => ({ task_id: task.id, run_id: result.run.id, lease_epoch: result.run.lease_epoch });
    async function endpoint(who, slug) {
      return who.invoke(slug, 'hub_register_agent', { instance_id: crypto.randomUUID(), harness: who.harness,
        name: 'Synthetic integration executor', roles: ['developer'], expected_version: 0,
        operation_id: crypto.randomUUID() });
    }
    async function accept(who, slug, task, registration) {
      const offer = await root.request('POST', base + '/agent-dispatches', { task_id: task.id,
        endpoint_id: registration.id, expected_task_version: task.version,
        expected_endpoint_version: registration.version, note: '合成联调：核对范围后显式接受' });
      const inbox = await who.invoke(slug, 'hub_agent_inbox', { endpoint_id: registration.id });
      assert.ok(inbox.items.some(item => item.id === offer.id));
      const received = await who.invoke(slug, 'hub_decide_agent_dispatch', { task_id: task.id,
        dispatch_id: offer.id, expected_version: offer.version, action: 'receive', operation_id: crypto.randomUUID() });
      assert.equal(received.dispatch.run_id, null);
      assert.equal((await root.request('GET', `${base}/tasks/${task.id}`)).task.status, 'ready');
      const args = { task_id: task.id, dispatch_id: offer.id, expected_version: received.dispatch.version,
        action: 'accept', operation_id: crypto.randomUUID() };
      const accepted = await who.invoke(slug, 'hub_decide_agent_dispatch', args);
      assert.deepEqual(await who.invoke(slug, 'hub_decide_agent_dispatch', args), accepted);
      assert.equal(accepted.run.instance_id, registration.instance_id);
      return accepted;
    }
    async function prepare(who, slug, currentLease) {
      const context = await who.invoke(slug, 'hub_prepare_work', { ...currentLease, operation_id: crypto.randomUUID() });
      await who.invoke(slug, 'hub_acknowledge_work', { ...currentLease, context_id: context.id,
        content_hash: context.content_hash, rule_ids: context.payload.policy.rules.map(rule => rule.id),
        operation_id: crypto.randomUUID() });
      return context;
    }
    async function lifecycle(who, slug, event, session, installation, extra = {}) {
      const binding = who.binding(slug);
      const child = spawn(process.execPath, [bridge, '--binding', who.c.descriptorPath(binding),
        '--lifecycle-hook', event, '--harness', who.harness, '--hook-installation', installation], {
        env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      });
      let out = '', err = '';
      child.stdout.on('data', bytes => { out += bytes; }); child.stderr.on('data', bytes => { err += bytes; });
      const exited = new Promise((resolve, reject) => { child.once('close', resolve); child.once('error', reject); });
      child.stdin.end(JSON.stringify({ session_id: session, cwd: binding.workspace,
        hook_event_name: event, prompt: 'SYNTHETIC_PROMPT_MUST_NOT_BE_PERSISTED', ...extra }));
      let timer;
      const code = await Promise.race([exited, new Promise((_, reject) => {
        timer = setTimeout(() => { child.kill(); reject(new Error('Lifecycle bridge timed out')); }, 15000);
      })]).finally(() => clearTimeout(timer));
      assert.equal(code, 0, 'Lifecycle bridge failed: ' + err);
      return JSON.parse(out);
    }
    async function installAndBind(who, slug, currentLease) {
      const args = { harness: who.harness };
      const preview = await who.invoke(slug, 'hub_preview_hooks', args);
      const installed = await who.invoke(slug, 'hub_install_hooks', { ...args, preview_hash: preview.preview_hash });
      const session = crypto.randomUUID();
      await lifecycle(who, slug, 'SessionStart', session, installed.installation_id);
      const started = await lifecycle(who, slug, 'UserPromptSubmit', session, installed.installation_id);
      const sessionKey = started.hookSpecificOutput.additionalContext.match(/session_key=([a-f0-9]{64})/)[1];
      const hook = (event, extra) => lifecycle(who, slug, event, session, installed.installation_id, extra);
      assert.equal((await hook('PreToolUse', { tool_name: 'Write', tool_input: { file_path: 'src/main.ts' } }))
        .hookSpecificOutput.permissionDecision, 'deny');
      await who.invoke(slug, 'hub_bind_work_session', { ...currentLease, session_key: sessionKey });
      assert.deepEqual(await hook('PreToolUse', { tool_name: 'Write', tool_input: { file_path: 'src/main.ts' } }), {});
      for (const target of ['../outside.txt', '.codex/hooks.json', 'docs/out-of-scope.md']) {
        assert.equal((await hook('PreToolUse', { tool_name: 'Write', tool_input: { file_path: target } }))
          .hookSpecificOutput.permissionDecision, 'deny');
      }
      return hook;
    }
    const webEndpoint = await endpoint(a, 'web'), apiEndpoint = await endpoint(b, 'api');
    await assert.rejects(b.invoke('api', 'hub_claim_task', { task_id: seed.tasks.api.id,
      instance_id: apiEndpoint.instance_id, harness: b.harness, operation_id: crypto.randomUUID() }),
    { code: 'TASK_DEPENDENCIES_INCOMPLETE' });
    check('cross_repository_dependency_blocks_early_claim');
    const webAccepted = await accept(a, 'web', seed.tasks.web, webEndpoint);
    const webLease = lease(seed.tasks.web, webAccepted);
    await assert.rejects(b.invoke('web', 'hub_claim_task', { task_id: seed.tasks.web.id,
      instance_id: crypto.randomUUID(), harness: b.harness, operation_id: crypto.randomUUID() }), { code: 'TASK_ALREADY_CLAIMED' });
    check('explicit_acceptance_and_retry_preserve_one_run');
    await prepare(a, 'web', webLease);
    const webHook = await installAndBind(a, 'web', webLease);
    check('codex_hook_protocol_guards_bound_task_and_paths');
    const webWorkspace = a.binding('web').workspace;
    await fs.writeFile(path.join(webWorkspace, 'src/main.ts'), 'web task uncommitted change\n');
    await fs.unlink(path.join(webWorkspace, 'src/remove.ts'));
    await fs.writeFile(path.join(webWorkspace, 'src/binary.bin'), Buffer.from([0, 255, 128, 13, 10]));
    assert.equal((await webHook('Stop')).decision, 'block');
    const logArgs = { ...webLease, summary: '合成联调 Web 检查点', notes: '验证未提交改动、删除、二进制和断网待传。',
      tests: 'Integration driver assertions; no model or human code review performed.',
      next_steps: '恢复待传并交给另一获准身份核对流程。', operation_id: crypto.randomUUID() };
    a.online = false;
    const pending = await a.invoke('web', 'hub_write_worklog', logArgs);
    assert.equal(pending.state, 'pending'); assert.equal(pending.server_confirmed, false);
    assert.equal(pending.server_code_backup, false);
    assert.ok((await a.invoke('web', 'hub_list_local_work', {})).items.some(row => row.local_checkpoint_id === pending.local_checkpoint_id));
    const encrypted = await fs.readFile(a.c.work.store.file(pending.local_checkpoint_id));
    assert.equal(encrypted.includes(Buffer.from(logArgs.notes)), false);
    check('offline_mcp_preserves_encrypted_pending_without_shared_receipt');
    await a.stop();
    a.online = true; await a.start();
    assert.ok((await a.invoke('web', 'hub_list_local_work', {})).items.some(row => row.local_checkpoint_id === pending.local_checkpoint_id));
    const shared = await a.c.work.retry(pending.local_checkpoint_id);
    assert.ok(shared.server_confirmed && shared.server_code_backup);
    assert.equal(shared.local_checkpoint_id, pending.local_checkpoint_id);
    const repeated = await a.invoke('web', 'hub_write_worklog', logArgs);
    assert.equal(repeated.server_receipt.id, shared.server_receipt.id);
    assert.equal(repeated.code_receipt.receipt_hash, shared.code_receipt.receipt_hash);
    check('desktop_restart_recovers_pending_and_retries_idempotently');
    checkpoints.push({ task: 'web', log_receipt: shared.server_receipt.receipt_hash, code_receipt: shared.code_receipt.receipt_hash });
    const list = await root.request('GET', `${base}/tasks/${seed.tasks.web.id}/worklogs`);
    assert.equal(list.items.filter(row => row.id === shared.server_receipt.id).length, 1);
    const recovery = await b.invoke('web', 'hub_download_work_backup', { task_id: seed.tasks.web.id,
      worklog_id: shared.server_receipt.id, operation_id: crypto.randomUUID() });
    const exported = await b.c.work.export(recovery.local_checkpoint_id, path.join(temporary, 'colleague-recovered-web'));
    assert.equal(await fs.readFile(path.join(exported.working_tree_directory, 'src/main.ts'), 'utf8'), 'web task uncommitted change\n');
    assert.deepEqual(await fs.readFile(path.join(exported.working_tree_directory, 'src/binary.bin')), Buffer.from([0, 255, 128, 13, 10]));
    await assert.rejects(fs.stat(path.join(exported.working_tree_directory, 'src/remove.ts')), { code: 'ENOENT' });
    assert.equal(await fs.readFile(path.join(b.binding('web').workspace, 'src/main.ts'), 'utf8'), 'baseline\n');
    check('other_member_recovers_uncommitted_binary_and_deleted_files_to_new_directory');
    await assert.rejects(a.invoke('api', 'hub_get_task', { task_id: seed.tasks.web.id }), { code: 'TASK_REPOSITORY_MISMATCH' });
    check('fixed_mcp_repository_prevents_cross_repository_task_access');

    async function finish(author, reviewer, slug, currentLease, saved) {
      const latest = await author.invoke(slug, 'hub_get_task', { task_id: currentLease.task_id });
      let task = await author.invoke(slug, 'hub_submit_task', { ...currentLease,
        worklog_id: saved.server_receipt.id, expected_version: latest.task.version, operation_id: crypto.randomUUID() });
      const decision = (gate, who) => ({ task_id: task.id, gate, approved: true,
        expected_version: task.version, worklog_id: task.submission_id,
        evidence: 'Synthetic integration assertion of gate transitions and identities; no substantive code review.',
        instance_id: crypto.randomUUID(), harness: who.harness, operation_id: crypto.randomUUID() });
      await assert.rejects(author.invoke(slug, 'hub_decide_task', decision('review', author)), { code: 'TASK_INDEPENDENT_REVIEW_REQUIRED' });
      for (const gate of ['review', 'test']) task = await reviewer.invoke(slug, 'hub_decide_task', decision(gate, reviewer));
      task = await root.request('POST', `${base}/tasks/${task.id}/decisions`, { gate: 'release', approved: true,
        expected_version: task.version, worklog_id: task.submission_id,
        evidence: 'Synthetic workflow release; no Git merge or production deployment.', instance_id: crypto.randomUUID(), harness: 'human' });
      assert.equal(task.status, 'completed');
      return task;
    }
    await finish(a, b, 'web', webLease, shared);
    check('web_submit_self_review_rejection_and_other_member_gates');
    await assert.rejects(a.invoke('web', 'hub_check_changes', { ...webLease, proposed_paths: ['src/main.ts'] }),
      error => ['TASK_LEASE_STALE', 'TASK_LEASE_NOT_OWNED', 'WORK_CONTEXT_STALE', 'WORK_CONTEXT_REQUIRED'].includes(error.code));
    check('completed_run_cannot_resume_writes');
    async function eventually(condition) {
      const until = Date.now() + 15000;
      while (!condition()) {
        assert.ok(Date.now() < until, 'Timed out waiting for desktop SSE event');
        await new Promise(resolve => setTimeout(resolve, 50));
      }
    }
    b.c.watch(seed.project_id);
    await eventually(() => b.c.profile.events.some(item => item.task_id === seed.tasks.web.id && item.kind === 'task.submitted'));
    b.c.cancelWatch(seed.project_id);
    const cursor = b.c.profile.cursors[seed.project_id];
    const apiAccepted = await accept(b, 'api', seed.tasks.api, apiEndpoint);
    const apiLease = lease(seed.tasks.api, apiAccepted);
    await prepare(b, 'api', apiLease);
    const apiHook = await installAndBind(b, 'api', apiLease);
    check('claude_code_hook_protocol_guards_bound_task_and_paths');
    await fs.writeFile(path.join(b.binding('api').workspace, 'src/main.ts'), 'api follows completed web task\n');
    assert.equal((await apiHook('Stop')).decision, 'block');
    const apiSaved = await b.invoke('api', 'hub_write_worklog', { ...apiLease,
      summary: '合成联调 API 检查点', notes: '前置 Web 任务完成后再开始 API，读取了获准交接。',
      tests: 'MCP, DPoP HTTP, PostgreSQL and native protocol driver assertions.',
      next_steps: '核对跨身份评审、测试与活动记录。', operation_id: crypto.randomUUID() });
    assert.ok(apiSaved.server_confirmed && apiSaved.server_code_backup);
    checkpoints.push({ task: 'api', log_receipt: apiSaved.server_receipt.receipt_hash, code_receipt: apiSaved.code_receipt.receipt_hash });
    await finish(b, a, 'api', apiLease, apiSaved);
    check('cross_repository_dependency_releases_and_api_workflow_completes');
    const missed = await root.request('POST', base + '/progress', { summary: '合成联调断开订阅期间的事件',
      status: 'checkpoint', repository_id: seed.repositories.api.id });
    b.c.watch(seed.project_id);
    assert.match(missed.sequence, /^\d+$/);
    await eventually(() => b.c.profile.events.some(item => item.sequence === missed.sequence));
    assert.ok(BigInt(b.c.profile.cursors[seed.project_id]) > BigInt(cursor));
    const eventIds = b.c.profile.events.map(item => item.sequence);
    assert.equal(new Set(eventIds).size, eventIds.length);
    check('desktop_sse_reconnect_replays_missed_events_without_duplicates');
    const activity = await root.request('GET', base + '/activity?limit=100');
    for (const id of [seed.tasks.web.id, seed.tasks.api.id]) {
      assert.ok(activity.items.some(item => item.task_id === id && item.kind === 'task.submitted'));
    }
    check('both_repository_submissions_have_persistent_activity');
    for (const who of [a, b]) {
      const decrypted = await protector.decryptStringAsync(await fs.readFile(path.join(who.directory, 'vault.bin')));
      assert.equal(decrypted.result.includes('SYNTHETIC_PROMPT_MUST_NOT_BE_PERSISTED'), false);
    }
    check('injected_prompt_text_is_not_saved_in_desktop_vault');
    const restricted = new Remote(seed.restricted, async () => {}); await restricted.init();
    try {
      await assert.rejects(restricted.request('GET', `${base}/tasks/${seed.tasks.web.id}/worklogs`, undefined,
        { slot: seed.project_id }), { code: 'TASK_NOT_FOUND' });
    } finally { restricted.close(); }
    check('unrelated_repository_member_cannot_read_handoff');
    await root.request('POST', `${base}/members/${seed.author.user.id}/revoke`, {});
    await assert.rejects(a.invoke('web', 'hub_get_project'), error =>
      ['PROJECT_ACCESS_REVOKED', 'PROJECT_NOT_ACCESSIBLE', 'SESSION_EXPIRED_OR_REVOKED', 'REFRESH_REPLAY_FAMILY_REVOKED'].includes(error.code));
    const retained = await b.invoke('web', 'hub_read_worklog', { task_id: seed.tasks.web.id,
      worklog_id: shared.server_receipt.id });
    assert.equal(retained.id, shared.server_receipt.id);
    assert.equal(await fs.readFile(path.join(webWorkspace, 'src/main.ts'), 'utf8'), 'web task uncommitted change\n');
    check('author_revocation_blocks_old_mcp_and_preserves_colleague_handoff_and_working_files');
    evidence.passed = true;
    evidence.completed_at = new Date().toISOString();
    evidence.elapsed_ms = Date.now() - started;
    return evidence;
  } finally {
    root.close();
    for (const who of actors) await who.stop();
    assert.equal(path.dirname(temporary), parent);
    assert.ok(path.basename(temporary).startsWith('hub-collaboration-'));
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

module.exports = { run };
