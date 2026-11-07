'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { test } = require('node:test');
const { Updater } = require('../core/updater.cjs');
const { Controller } = require('../core/controller.cjs');
const { compareVersions, hashFile, readJson, writeJson, verifiedRelease, verifyFreshness } =
  require('../core/update-integrity.cjs');

const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const next = '0.10.0';
const archive = Buffer.from('release fixture bytes');
const files = {
  'Agent Hub.exe': 'fixture',
  'resources/app/package.json': JSON.stringify({ name: 'agent-hub-desktop', version: next, main: 'main.cjs' }),
  'resources/app/main.cjs': 'main',
  'resources/app/core/bridge.cjs': 'bridge',
};
const index = JSON.stringify(Object.entries(files).map(([name, content]) => ({ path: name, sha256: digest(content) })));

function signed(key, changes) {
  const payload = Buffer.from(JSON.stringify({ schema: 1, channel: 'stable', platform: 'win-x64',
    metadata_version: 1, issued_at: new Date(Date.now() - 60000).toISOString(),
    expires_at: new Date(Date.now() + 86400000).toISOString(),
    version: next, bytes: archive.length, sha256: digest(archive), files_sha256: digest(index), ...changes }));
  return { payload: payload.toString('base64url'), signature: crypto.sign(null, payload, key).toString('base64url') };
}

async function setup(t, options) {
  const temp = await fs.realpath(os.tmpdir());
  const directory = await fs.mkdtemp(path.join(temp, 'agent-hub-update-test-'));
  const keys = crypto.generateKeyPairSync('ed25519');
  const channel = { url: 'https://release.example/desktop/', publicKey: keys.publicKey };
  const updater = new Updater({ directory, version: '0.9.0', channel, restart: async () => {},
    ...options(keys.privateKey) });
  await updater.open();
  t.after(async () => {
    await updater.close();
    const real = await fs.realpath(directory);
    assert.equal(path.dirname(real), temp);
    assert.match(path.basename(real), /^agent-hub-update-test-/);
    await fs.rm(real, { recursive: true, force: true });
  });
  return updater;
}

test('a signed release activates after startup and still boots when its metadata later expires', async t => {
  let checks = 0, downloads = 0, staged, signingKey;
  const updater = await setup(t, key => ({
    transport: async url => {
      signingKey = key;
      if (url.pathname.endsWith('stable.json')) {
        checks++;
        return Response.json(signed(key));
      }
      downloads++;
      assert.equal(url.pathname, '/desktop/AgentHub-0.10.0-win-x64.zip');
      return new Response(archive);
    },
    extract: async (download, folder) => {
      assert.equal(await hashFile(download), digest(archive));
      for (const [name, content] of Object.entries(files)) {
        const target = path.join(folder, name);
        await fs.mkdir(path.dirname(target), { recursive: true });
        await fs.writeFile(target, content);
      }
      await fs.writeFile(path.join(folder, 'files.sha256.json'), index);
    },
    restart: async release => { staged = release; },
  }));
  assert.equal(compareVersions('0.10.0', '0.9.0'), 1);
  const results = await Promise.all(Array.from({ length: 10 }, () => updater.check({ fresh: true })));
  assert.ok(results.every(status => status.code === 'CLIENT_UPDATE_REQUIRED' && !status.can_continue));
  await updater.installing;
  assert.equal(checks, 1);
  assert.equal(downloads, 1);
  assert.equal(staged.version, next);
  assert.deepEqual(await updater.boot('unused', ''), {});
  const pendingRecord = await readJson(updater.file('pending.json'));
  const expiredEnvelope = signed(signingKey, {
    issued_at: new Date(Date.now() - 172800000).toISOString(),
    expires_at: new Date(Date.now() - 86400000).toISOString(),
  });
  assert.throws(() => verifyFreshness(verifiedRelease(expiredEnvelope, updater.channel.publicKey)),
    { code: 'UPDATE_METADATA_EXPIRED' });
  await writeJson(updater.file('pending.json'), { ...pendingRecord, envelope: expiredEnvelope });
  const candidate = new Updater({ directory: updater.directory, version: next, channel: updater.channel });
  await candidate.open();
  const { pending } = await candidate.boot(staged.executable, staged.id);
  await candidate.activate(pending);
  assert.equal((await updater.boot('unused', '')).target, staged.executable);
  assert.equal((await readJson(updater.file('active.json'))).id, staged.id);
  await fs.appendFile(staged.executable, 'altered bytes');
  assert.deepEqual(await updater.boot('unused', ''), {});
});

test('invalid signatures, substituted archives, and an older feed cannot enable outdated calls', async t => {
  let checks = 0, downloads = 0, extracted = false;
  const updater = await setup(t, key => ({
    transport: async url => {
      if (!url.pathname.endsWith('stable.json')) {
        downloads++;
        return new Response(Buffer.alloc(archive.length, 0x41));
      }
      checks++;
      if (checks === 1) {
        const envelope = signed(key);
        envelope.signature = Buffer.alloc(64).toString('base64url');
        return Response.json(envelope);
      }
      return Response.json(signed(key, checks >= 3 ? { version: '0.8.0' } : {}));
    },
    extract: async () => { extracted = true; },
  }));
  assert.equal((await updater.check()).code, 'UPDATE_CHECK_UNAVAILABLE');
  assert.equal(downloads, 0);
  assert.equal((await updater.check({ retry: true })).code, 'CLIENT_UPDATE_REQUIRED');
  await updater.installing;
  assert.equal(updater.state.code, 'CLIENT_UPDATE_FAILED');
  assert.equal(downloads, 1);
  assert.equal(extracted, false);
  const afterReplay = await updater.checkFeed(false);
  assert.equal(afterReplay.latest_version, next);
  assert.equal(afterReplay.can_continue, false);
  assert.equal(downloads, 1);
});

test('freshness rejects expired, future, malformed and excessively long signed metadata', async () => {
  const keys = crypto.generateKeyPairSync('ed25519');
  const time = Date.now();
  const verify = changes => verifyFreshness(verifiedRelease(signed(keys.privateKey, changes), keys.publicKey),
    null, { time });
  const cases = [
    [{ expires_at: new Date(time - 1).toISOString() }, 'UPDATE_METADATA_EXPIRED'],
    [{ issued_at: new Date(time + 600000).toISOString() }, 'UPDATE_CLOCK_INVALID'],
    [{ metadata_version: undefined }, 'UPDATE_METADATA_INVALID'],
    [{ metadata_version: 0 }, 'UPDATE_METADATA_INVALID'],
    [{ issued_at: '2026-02-30T00:00:00.000Z' }, 'UPDATE_METADATA_INVALID'],
    [{ expires_at: new Date(time + 32 * 86400000).toISOString() }, 'UPDATE_METADATA_INVALID'],
    [{ expires_at: '2020-01-01T00:00:00.000Z' }, 'UPDATE_METADATA_INVALID'],
  ];
  for (const [changes, code] of cases) assert.throws(() => verify(changes), { code });
});

test('metadata renewal persists its sequence and refuses replay or reused numbers after restart', async t => {
  let feed, signingKey;
  const updater = await setup(t, key => {
    signingKey = key;
    feed = signed(key, { version: next, metadata_version: 5 });
    return { version: next, transport: async () => Response.json(feed) };
  });
  assert.equal((await updater.check()).can_continue, true);
  const record = await readJson(updater.file('known.json'));
  const original = verifiedRelease(record.envelope, updater.channel.publicKey);
  feed = signed(signingKey, { ...original, metadata_version: 6,
    expires_at: new Date(Date.now() + 172800000).toISOString() });
  assert.equal((await updater.checkFeed(false)).can_continue, true);
  const restarted = new Updater({ directory: updater.directory, version: next, channel: updater.channel,
    transport: async () => Response.json(feed) });
  await restarted.open();
  t.after(() => restarted.close());
  assert.equal(restarted.known.release.metadata_version, 6);
  assert.equal((await restarted.check()).can_continue, true);
  feed = record.envelope;
  assert.equal((await restarted.checkFeed(false)).code, 'UPDATE_METADATA_ROLLBACK');
  const renewed = restarted.known.release;
  feed = signed(signingKey, { ...renewed, expires_at: new Date(Date.now() + 259200000).toISOString() });
  assert.equal((await restarted.checkFeed(false)).code, 'UPDATE_METADATA_REUSED');
  assert.throws(() => verifyFreshness({ ...original, metadata_version: 4 }, original),
    { code: 'UPDATE_METADATA_ROLLBACK' });
  assert.throws(() => verifyFreshness({ ...original, expires_at: new Date(Date.now() + 172800000).toISOString() },
    original), { code: 'UPDATE_METADATA_REUSED' });
  assert.doesNotThrow(() => verifyFreshness({ ...original, metadata_version: 6,
    expires_at: new Date(Date.now() + 172800000).toISOString() }, original));
  assert.throws(() => verifyFreshness(original, original, { checkedAt: Date.now() + 600000 }),
    { code: 'UPDATE_CLOCK_INVALID' });
});

test('cached newer artifacts do not download while the current metadata is expired or offline', async t => {
  let downloads = 0;
  const updater = await setup(t, key => ({
    transport: async url => {
      if (!url.pathname.endsWith('stable.json')) { downloads++; return new Response(archive); }
      return Response.json(signed(key, { expires_at: new Date(Date.now() - 1).toISOString() }));
    },
  }));
  assert.equal((await updater.check()).code, 'UPDATE_METADATA_EXPIRED');
  assert.equal(downloads, 0);
  // Historical artifact signatures remain usable for startup; no freshness check here.
  const keys = crypto.generateKeyPairSync('ed25519');
  const envelope = signed(keys.privateKey, { metadata_version: undefined, issued_at: undefined, expires_at: undefined });
  assert.equal(verifiedRelease(envelope, keys.publicKey).version, next);
  updater.channel.publicKey = keys.publicKey;
  await writeJson(updater.file('known.json'), { envelope });
  await updater.open();
  updater.transport = async () => { throw new Error('offline'); };
  assert.equal((await updater.checkFeed(true)).can_continue, false);
  assert.equal(downloads, 0);
});

test('project MCP initializes its release check and blocks operations until it passes', async () => {
  const id = crypto.randomUUID();
  const binding = { id, projectId: id, status: 'active', secret: 'test-binding-secret', workspace: 'fixture' };
  const profile = { bindings: [binding], sessions: { platform: {} } };
  const vault = { data: { current: 'test', profiles: { test: profile } } };
  let status = { installed_version: '0.9.0', latest_version: next, can_continue: false,
    code: 'CLIENT_UPDATE_REQUIRED', message: 'updating' };
  const requests = [];
  const controller = new Controller({ vault, updater: { check: async options => {
    requests.push(options);
    return status;
  } } });
  let executed = false;
  controller.connection = 'active';
  controller.projectRequest = async () => { executed = true; return { id }; };
  const invoke = request => controller.dispatch({
    action: 'mcp', binding: id, secret: binding.secret, args: { request: { jsonrpc: '2.0', id: 1, ...request } },
  });
  const initialized = await invoke({ method: 'initialize' });
  assert.match(initialized.result.instructions, /0\.9\.0.*updating/);
  assert.deepEqual(requests[0], { fresh: true });
  const denied = await invoke({ method: 'tools/call', params: { name: 'hub_get_project' } });
  assert.equal(denied.result.isError, true);
  assert.equal(JSON.parse(denied.result.content[0].text).update.latest_version, next);
  assert.equal(executed, false);
  status = { ...status, can_continue: true };
  const check = await invoke({ method: 'tools/call', params: { name: 'hub_check_version' } });
  assert.equal(check.result.structuredContent.can_continue, true);
  assert.equal((await invoke({ method: 'tools/call', params: { name: 'hub_get_project' } })).result.structuredContent.id, id);
});
