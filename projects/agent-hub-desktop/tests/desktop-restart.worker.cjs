'use strict';

// Test-only process driver. The desktop branch loads the unmodified main.cjs.
const { app, BrowserWindow, safeStorage } = require('electron');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs/promises');
const net = require('node:net');

assert.equal(process.platform, 'win32');
assert.equal(process.env.HUB_TEST_HTTP, 'collaboration-only');
const root = path.resolve(process.env.HUB_RESTART_TEST_ROOT || '');
const directory = path.resolve(process.env.AGENT_HUB_TEST_DATA || '');
assert.ok(path.basename(root).startsWith('hub-desktop-restart-'));
const relative = path.relative(root, directory);
assert.ok(relative && relative !== '..' && !relative.startsWith('..' + path.sep) && !path.isAbsolute(relative));
const mode = process.env.HUB_RESTART_WORKER_MODE;
assert.ok(['seed', 'desktop'].includes(mode));
const endpoint = process.env.HUB_RESTART_CONTROL_PIPE;
assert.ok(endpoint?.startsWith('\\\\.\\pipe\\agent-hub-restart-test-'));
const control = net.connect(endpoint);
const emit = value => { if (!control.destroyed) control.write(JSON.stringify(value) + '\n'); };
app.disableHardwareAcceleration();
app.setPath('userData', directory);
app.on('before-quit', () => emit({ event: 'before-quit', pid: process.pid }));

if (mode === 'desktop') require('../main.cjs');

async function windowReady() {
  const until = Date.now() + 25000;
  while (Date.now() < until) {
    const window = BrowserWindow.getAllWindows()[0];
    if (window && !window.webContents.isLoading() && await window.webContents.executeJavaScript('!!window.hub').catch(() => false)) return window;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('Desktop renderer did not become ready');
}
async function handle(request) {
  assert.ok(Number.isSafeInteger(request.id));
  if (request.action === 'seed') {
    assert.equal(mode, 'seed');
    assert.equal(request.profile.origin, 'http://127.0.0.1:18768');
    await app.whenReady();
    assert.ok(await safeStorage.isAsyncEncryptionAvailable());
    const { Vault } = require('../core/vault.cjs');
    const vault = new Vault(directory, safeStorage); await vault.load();
    assert.equal(vault.data.current, null, 'Never overwrite an existing desktop profile');
    vault.data = { version: 1, current: 'fixture', profiles: { fixture: request.profile } };
    await vault.save();
    return { seeded: true, encryption: 'Electron-Windows-DPAPI', pid: process.pid,
      electron_version: process.versions.electron, node_version: process.versions.node };
  }
  if (request.action === 'ui') {
    assert.equal(mode, 'desktop');
    assert.ok(['state', 'localWork', 'refresh', 'retryCheckpoint'].includes(request.name));
    const window = await windowReady();
    return window.webContents.executeJavaScript(
      'window.hub.invoke(' + JSON.stringify(request.name) + ',' + JSON.stringify(request.args || {}) + ')');
  }
  if (request.action === 'quit') {
    setImmediate(() => app.quit());
    return { quitting: true, pid: process.pid };
  }
  throw new Error('Unknown restart worker request');
}
let buffer = '', queue = Promise.resolve();
control.setEncoding('utf8');
control.on('data', chunk => {
  buffer += chunk;
  if (Buffer.byteLength(buffer) > 256 * 1024) { app.exit(2); return; }
  let end;
  while ((end = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
    if (!line.trim()) continue;
    queue = queue.then(async () => {
      let request;
      try { request = JSON.parse(line); emit({ id: request.id, result: await handle(request) }); }
      catch (error) { emit({ id: request?.id, error: { code: error.code || error.name, message: error.message } }); }
    });
  }
});
control.on('end', () => { queue.finally(() => app.quit()); });
control.on('error', () => app.exit(2));
process.on('uncaughtException', () => app.exit(2));
process.on('unhandledRejection', () => app.exit(2));
