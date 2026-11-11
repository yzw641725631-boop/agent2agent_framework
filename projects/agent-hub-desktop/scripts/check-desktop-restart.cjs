'use strict';

// Node parent launches separate, hidden Electron processes with a test-only profile.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const net = require('node:net');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { Remote } = require('../core/remote.cjs');
const codec = require('../core/backup-codec.cjs');
const execute = promisify(execFile);
const origin = 'http://127.0.0.1:18768';
const upstream = 'http://127.0.0.1:18769';
const desktop = path.resolve(__dirname, '..');
const workerFile = path.join(desktop, 'tests/desktop-restart.worker.cjs');
const electron = path.join(desktop, 'node_modules/electron/dist/electron.exe');
const bridge = path.join(desktop, 'core/bridge.cjs');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');

class Worker {
  constructor(root, directory, mode) {
    const endpoint = '\\\\.\\pipe\\agent-hub-restart-test-' + crypto.randomUUID();
    const env = { ...process.env, HUB_RESTART_TEST_ROOT: root, AGENT_HUB_TEST_DATA: directory,
      HUB_RESTART_WORKER_MODE: mode, HUB_RESTART_CONTROL_PIPE: endpoint };
    delete env.ELECTRON_RUN_AS_NODE;
    this.pending = new Map(); this.sequence = 0; this.beforeQuit = 0; this.mode = mode;
    this.stderr = '';
    let resolveExit;
    this.exit = new Promise(resolve => { resolveExit = resolve; });
    const receive = socket => {
      this.pipe = socket;
      let buffer = '';
      socket.setEncoding('utf8');
      socket.on('error', () => {});
      socket.on('data', data => {
        buffer += data;
        let end;
        while ((end = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
          const value = JSON.parse(line);
          if (value.event === 'before-quit') { this.beforeQuit++; continue; }
          const pending = this.pending.get(value.id);
          if (pending) {
            this.pending.delete(value.id); clearTimeout(pending.timer);
            if (value.error) pending.reject(Object.assign(new Error(value.error.message), { code: value.error.code }));
            else pending.resolve(value.result);
          }
        }
      });
    };
    const failed = error => {
      for (const item of this.pending.values()) { clearTimeout(item.timer); item.reject(error); }
      this.pending.clear();
    };
    this.ready = new Promise((resolve, reject) => {
      const startup = setTimeout(() => reject(new Error('Electron control connection timed out')), 15000);
      this.server = net.createServer(socket => { clearTimeout(startup); receive(socket); resolve(); });
      this.server.once('error', error => { clearTimeout(startup); reject(error); });
      this.server.listen(endpoint, () => {
        this.child = spawn(electron, [workerFile, '--hidden'], { env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
        this.pid = this.child.pid;
        this.child.stdout.resume();
        this.child.stderr.on('data', data => { this.stderr = (this.stderr + data).slice(-4096); });
        this.child.once('error', error => { clearTimeout(startup); failed(error); reject(error); });
        this.child.once('close', code => {
          clearTimeout(startup);
          this.code = code; resolveExit(code); this.pipe?.destroy(); this.server.close();
          const error = new Error('Restart worker (' + mode + ', action ' + this.lastAction + ') exited unexpectedly: ' + code + '; ' + this.stderr);
          failed(error); reject(error);
        });
      });
    });
    this.ready.catch(() => { this.child?.kill('SIGKILL'); this.server.close(); });
  }
  async request(action, args = {}) {
    this.lastAction = action + (args.name ? ':' + args.name : '');
    await this.ready;
    assert.equal(this.child.exitCode, null, 'Desktop process already exited');
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error('Restart worker response timed out: ' + action)); }, 40000);
      this.pending.set(id, { resolve, reject, timer });
      this.pipe.write(JSON.stringify({ id, action, ...args }) + '\n');
    });
  }
  ui(name, args) { return this.request('ui', { name, args }); }
  async graceful() {
    await this.request('quit');
    assert.equal(await this.waitExit(), 0);
    if (this.mode === 'desktop') assert.ok(this.beforeQuit > 0, 'Normal quit must execute the real before-quit handler');
  }
  async terminate() {
    assert.equal(this.child.exitCode, null);
    assert.equal(this.beforeQuit, 0);
    this.child.kill('SIGKILL'); // Terminate only the process handle created by this driver.
    await this.waitExit();
    assert.equal(this.beforeQuit, 0, 'Forced termination must bypass normal quit handling');
  }
  async waitExit() {
    let timer;
    try { return await Promise.race([this.exit, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Desktop did not exit')), 15000);
    })]); } finally { clearTimeout(timer); }
  }
}

async function run() {
  assert.equal(process.platform, 'win32');
  assert.equal(process.env.HUB_TEST_HTTP, 'collaboration-only');
  assert.ok(process.env.HUB_TEST_FIXTURE_KEY?.length >= 40);
  const reportFile = process.env.HUB_DESKTOP_RESTART_REPORT;
  assert.ok(reportFile && path.isAbsolute(reportFile));
  const response = await fetch(upstream + '/__test__/seed', { headers: { 'X-Hub-Fixture-Key': process.env.HUB_TEST_FIXTURE_KEY } });
  assert.equal(response.status, 200);
  const seed = await response.json(); assert.equal(seed.fixture_kind, 'isolated-collaboration-v1');
  const parent = await fs.realpath(os.tmpdir());
  const root = await fs.mkdtemp(path.join(parent, 'hub-desktop-restart-'));
  const workers = [], connections = new Set();
  let gate = 'online', lost = 0;
  const proxy = http.createServer((request, outgoing) => {
    if (gate === 'offline') { request.socket.destroy(); return; }
    const dropLog = gate === 'lose-log-ack' && request.method === 'POST' && /\/worklogs$/.test(request.url);
    const dropCode = gate === 'lose-code-ack' && request.method === 'POST' && /\/worklogs\/[^/]+\/snapshot$/.test(request.url);
    const relay = http.request(upstream + request.url, { method: request.method, headers: request.headers }, result => {
      if ((dropLog || dropCode) && result.statusCode === 200) {
        result.resume(); result.once('end', () => { lost++; gate = 'offline'; outgoing.destroy(); });
      } else { outgoing.writeHead(result.statusCode, result.headers); result.pipe(outgoing); }
    });
    relay.on('error', () => outgoing.destroy());
    outgoing.on('close', () => relay.destroy()); request.pipe(relay);
  });
  proxy.on('connection', socket => { connections.add(socket); socket.on('error', () => {}); socket.on('close', () => connections.delete(socket)); });
  await new Promise((resolve, reject) => { proxy.once('error', reject); proxy.listen(18768, '127.0.0.1', resolve); });
  const rootRemote = new Remote(seed.root, async () => {}, (url, options) => fetch(url.replace(origin, upstream), options));
  const results = [], start = Date.now();
  let runtime;
  const base = `/api/projects/${seed.project_id}`;
  const launch = (directory, mode) => { const w = new Worker(root, directory, mode); workers.push(w); return w; };
  async function mcp(directory, binding, name, args = {}) {
    const child = spawn(electron, [bridge, '--binding', path.join(directory, 'bindings', binding + '.json')], {
      env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    });
    let out = '';
    child.stdout.on('data', bytes => { out += bytes; }); child.stderr.resume();
    const ended = new Promise((resolve, reject) => { child.once('close', resolve); child.once('error', reject); });
    child.stdin.end(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }) + '\n');
    let timer;
    const code = await Promise.race([ended, new Promise((_, reject) => {
      timer = setTimeout(() => { child.kill(); reject(new Error('MCP bridge timed out')); }, 30000);
    })]).finally(() => clearTimeout(timer));
    assert.equal(code, 0);
    const response = JSON.parse(out.trim()); assert.ok(!response.error);
    if (response.result.isError) {
      const error = JSON.parse(response.result.content[0].text);
      throw Object.assign(new Error(error.message), { code: error.code });
    }
    return response.result.structuredContent || JSON.parse(response.result.content[0].text);
  }
  try {
    await rootRemote.init();
    for (const scenario of ['normal-exit-pending', 'forced-exit-pending', 'forced-exit-log-ack-lost', 'forced-exit-code-ack-lost']) {
      console.log('DESKTOP_RESTART_PROGRESS ' + JSON.stringify({ scenario, state: 'starting' }));
      gate = 'online'; lost = 0;
      const scenarioStart = Date.now(), directory = path.join(root, scenario + '-profile'), workspace = path.join(root, scenario + '-repo');
      await fs.mkdir(path.join(workspace, 'src'), { recursive: true });
      await fs.mkdir(path.join(workspace, 'tests'));
      await fs.writeFile(path.join(workspace, 'src/main.ts'), 'baseline\n');
      await fs.writeFile(path.join(workspace, 'src/remove.ts'), 'delete this\n');
      await fs.writeFile(path.join(workspace, 'tests/baseline.txt'), 'unchanged baseline\n');
      const git = args => execute('git', ['-C', workspace, ...args], { windowsHide: true });
      await git(['init', '-q']); await git(['config', 'core.autocrlf', 'false']);
      await git(['remote', 'add', 'origin', seed.repositories.web.repository_url]); await git(['add', '.']);
      await git(['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org', 'commit', '-qm', 'baseline']);
      const binding = crypto.randomUUID(), profile = structuredClone(seed.author);
      profile.cursors = {}; profile.bindings = [{ id: binding, projectId: seed.project_id,
        repositoryId: seed.repositories.web.id, workspace, status: 'active', secret: crypto.randomBytes(32).toString('base64url'),
        initial_commit: (await git(['rev-parse', 'HEAD'])).stdout.trim() }];
      const seeder = launch(directory, 'seed');
      const seeded = await seeder.request('seed', { profile });
      runtime ??= { electron_version: seeded.electron_version, node_version: seeded.node_version, protector: seeded.encryption };
      await seeder.graceful();
      let app = launch(directory, 'desktop');
      assert.equal((await app.ui('state')).connection, 'active');
      let task = await rootRemote.request('POST', base + '/tasks', { title: scenario, description: 'Synthetic desktop restart acceptance',
        acceptance: 'No duplicate log or code receipt, unchanged working files', repository_id: seed.repositories.web.id,
        scope: { repository: 'web', platform: 'web', paths: ['src', 'tests'] }, execution_mode: 'observed' });
      for (const name of ['requirements', 'design', 'feasibility']) task = await rootRemote.request('POST', `${base}/tasks/${task.id}/decisions`, {
        gate: name, approved: true, expected_version: task.version, evidence: 'Synthetic recovery test admission',
        instance_id: crypto.randomUUID(), harness: 'human' });
      const call = (name, args) => mcp(directory, binding, name, args);
      const accepted = await call('hub_claim_task', { task_id: task.id, instance_id: crypto.randomUUID(), harness: 'codex', operation_id: crypto.randomUUID() });
      const lease = { task_id: task.id, run_id: accepted.run.id, lease_epoch: accepted.run.lease_epoch };
      const context = await call('hub_prepare_work', { ...lease, operation_id: crypto.randomUUID() });
      await call('hub_acknowledge_work', { ...lease, context_id: context.id, content_hash: context.content_hash,
        rule_ids: context.payload.policy.rules.map(rule => rule.id), operation_id: crypto.randomUUID() });
      const text = 'uncommitted restart fixture ' + scenario + '\n', binary = Buffer.from([0, 255, 128, 10, 13, 1]);
      await fs.writeFile(path.join(workspace, 'src/main.ts'), text);
      await fs.writeFile(path.join(workspace, 'src/binary.bin'), binary); await fs.unlink(path.join(workspace, 'src/remove.ts'));
      gate = scenario.endsWith('log-ack-lost') ? 'lose-log-ack' : scenario.endsWith('code-ack-lost') ? 'lose-code-ack' : 'offline';
      const saveArgs = { ...lease, summary: '进程重启验收 ' + scenario, notes: 'SYNTHETIC_RESTART_NOTES_' + scenario,
        tests: 'Separate Electron main processes, real main.cjs, stdio MCP, DPAPI and isolated PostgreSQL.',
        next_steps: '从磁盘重启后恢复并核对独立回执。', operation_id: crypto.randomUUID() };
      const pending = await call('hub_write_worklog', saveArgs);
      if (scenario.endsWith('code-ack-lost')) {
        assert.equal(pending.state, 'shared'); assert.equal(pending.server_confirmed, true); assert.equal(pending.server_code_backup, false);
      } else { assert.equal(pending.state, 'pending'); assert.equal(pending.server_confirmed, false); }
      assert.equal(lost, scenario.endsWith('ack-lost') ? 1 : 0);
      const onDisk = await fs.readFile(path.join(directory, 'work-checkpoints', pending.local_checkpoint_id + '.bin'));
      assert.equal(onDisk.includes(Buffer.from(saveArgs.notes)), false);
      const oldPid = app.pid;
      if (scenario === 'normal-exit-pending') await app.graceful(); else await app.terminate();
      const exitedCode = app.code, beforeQuit = app.beforeQuit;
      const exitSignal = app.child.signalCode;
      const prior = await rootRemote.request('GET', `${base}/tasks/${task.id}/worklogs`);
      assert.equal(prior.items.length, scenario.endsWith('ack-lost') ? 1 : 0);
      if (scenario.endsWith('code-ack-lost')) assert.ok(prior.items[0].snapshot);
      app = launch(directory, 'desktop');
      assert.notEqual(app.pid, oldPid);
      assert.equal((await app.ui('state')).connection, 'offline');
      const restored = (await app.ui('localWork')).items.find(row => row.local_checkpoint_id === pending.local_checkpoint_id);
      assert.ok(restored); assert.equal(restored.state, pending.state);
      gate = 'online'; await app.ui('refresh');
      const shared = await app.ui('retryCheckpoint', { id: pending.local_checkpoint_id });
      assert.ok(shared.server_confirmed && shared.server_code_backup, JSON.stringify(shared));
      const repeated = await app.ui('retryCheckpoint', { id: pending.local_checkpoint_id });
      assert.equal(repeated.server_receipt.id, shared.server_receipt.id);
      assert.equal(repeated.code_receipt.receipt_hash, shared.code_receipt.receipt_hash);
      const logs = await rootRemote.request('GET', `${base}/tasks/${task.id}/worklogs`);
      assert.equal(logs.items.length, 1); assert.equal(logs.items[0].id, shared.server_receipt.id);
      if (prior.items.length) assert.equal(prior.items[0].id, shared.server_receipt.id);
      if (prior.items[0]?.snapshot) {
        assert.equal(prior.items[0].snapshot.id, shared.code_receipt.id);
        assert.equal(prior.items[0].snapshot.receipt_hash, shared.code_receipt.receipt_hash);
      }
      const log = await rootRemote.request('GET', `${base}/tasks/${task.id}/worklogs/${shared.server_receipt.id}`);
      const bytes = await rootRemote.request('GET', `${base}/tasks/${task.id}/worklogs/${log.id}/snapshot/content`, undefined, { binary: true });
      const archive = await codec.decodeArchive(bytes, { worklogId: log.id, evidenceHash: log.evidence_hash, manifestHash: log.snapshot.manifest_hash });
      const store = new (require('../core/work-store.cjs').WorkStore)(directory, null);
      const files = store.workingFiles({ snapshot: archive });
      const map = new Map(files.map(file => [file.path, file.data]));
      assert.equal(map.get('src/main.ts').toString(), text); assert.deepEqual(map.get('src/binary.bin'), binary);
      assert.equal(map.has('src/remove.ts'), false); assert.equal(map.get('tests/baseline.txt').toString(), 'unchanged baseline\n');
      assert.equal(await fs.readFile(path.join(workspace, 'src/main.ts'), 'utf8'), text);
      assert.deepEqual(await fs.readFile(path.join(workspace, 'src/binary.bin')), binary);
      await assert.rejects(fs.stat(path.join(workspace, 'src/remove.ts')), { code: 'ENOENT' });
      await call('hub_release_task', { ...lease, reason: '合成进程恢复验收完成，保留原检查点。', operation_id: crypto.randomUUID() });
      await app.graceful();
      results.push({ scenario, passed: true, first_pid: oldPid, restarted_pid: app.pid, distinct_main_processes: true,
        first_exit_code: exitedCode, first_exit_signal: exitSignal, normal_quit_events_before_first_exit: beforeQuit,
        response_losses_injected: lost, local_checkpoint_encrypted: true, disk_checkpoint_recovered_while_offline: true,
        logs_before_recovery: prior.items.length, logs_after_recovery: logs.items.length,
        separate_log_and_code_receipts: true, duplicate_retry_preserves_receipts: true,
        restored_text_binary_deletion_and_baseline: true, original_workspace_preserved: true,
        log_receipt: shared.server_receipt.receipt_hash, code_receipt: shared.code_receipt.receipt_hash,
        elapsed_ms: Date.now() - scenarioStart });
      console.log('DESKTOP_RESTART_PROGRESS ' + JSON.stringify({ scenario, passed: true }));
    }
    const report = { passed: true, started_at: new Date(start).toISOString(), completed_at: new Date().toISOString(),
      elapsed_ms: Date.now() - start, ...runtime, desktop_version: require('../package.json').version, entry: 'main.cjs',
      packaged_application: false, tray_and_updater_active: false, platform: process.platform, physical_computers: 1,
      actual_model_calls: 0, actual_native_host_launches: 0,
      entry_sha256: sha(await fs.readFile(path.join(desktop, 'main.cjs'))),
      source_entry_unmodified: true, operating_system_restart: false, power_loss_or_mid_write_crash: false,
      failure_injection: 'Parent HTTP relay drops connections or committed responses; owned Electron process handle is forcibly terminated.',
      scenarios: results };
    await fs.writeFile(reportFile, JSON.stringify(report, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
    console.log('DESKTOP_RESTART_RESULT ' + JSON.stringify(report));
    return report;
  } finally {
    rootRemote.close();
    for (const w of workers) if (w.child && w.child.exitCode === null && w.child.signalCode === null) { w.child.kill('SIGKILL'); await w.exit; }
    for (const socket of connections) socket.destroy();
    await new Promise(resolve => proxy.close(resolve));
    assert.equal(path.dirname(root), parent); assert.ok(path.basename(root).startsWith('hub-desktop-restart-'));
    await fs.rm(root, { recursive: true, force: true });
  }
}

if (require.main === module) run().catch(error => { console.error(error.code || error.name, error.stack); process.exitCode = 1; });
module.exports = { run };
