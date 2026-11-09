'use strict';

const path = require('node:path');
const jsonc = require('jsonc-parser');
const { isDeepStrictEqual } = require('node:util');
const host = require('./host-install.cjs');
const hooks = require('./host-hooks.cjs');
const { HubError } = require('./remote.cjs');
const conflict = () => new HubError('HOOK_CONFIGURATION_CONFLICT', '钩子配置或收据已变化，保留现有内容，请预览后处理冲突。');
const plain = value => Array.isArray(value) ? value.map(plain) : value && typeof value === 'object'
  ? Object.fromEntries(Object.entries(value).map(([key, child]) => [key, plain(child)])) : value;
const same = (a,b) => isDeepStrictEqual(plain(a),plain(b));
function parse(bytes) {
  const source = bytes ? new TextDecoder('utf-8', { fatal: true }).decode(bytes) : '{}';
  const errors = [], tree = jsonc.parseTree(source, errors, { allowTrailingComma: true });
  if (errors.length || tree?.type !== 'object') throw conflict();
  function unique(node) {
    if (node.type === 'object') {
      const names = new Set();
      for (const child of node.children || []) {
        const key = child.children[0].value;
        if (names.has(key)) throw conflict();
        names.add(key); unique(child.children[1]);
      }
    } else for (const child of node.children || []) unique(child);
  }
  unique(tree);
  const value = jsonc.getNodeValue(tree);
  if (value.hooks !== undefined && (!value.hooks || typeof value.hooks !== 'object' || Array.isArray(value.hooks))) throw conflict();
  return { source, value };
}
function edit(source, entryPath, value) {
  return jsonc.applyEdits(source, jsonc.modify(source, entryPath, value,
    { formattingOptions: { insertSpaces: true, tabSize: 2, eol: source.includes('\r\n') ? '\r\n' : '\n' } }));
}
function merge(bytes, desired, previous) {
  let { source, value } = parse(bytes);
  const events = new Set([...Object.keys(previous?.entries || {}), ...Object.keys(desired?.hooks || {})]);
  for (const event of events) {
    const existing = value.hooks?.[event] ?? [];
    if (!Array.isArray(existing)) throw conflict();
    let next = [...existing];
    for (const owned of previous?.entries?.[event] || []) {
      const matches = next.map((entry, i) => same(entry, owned) ? i : -1).filter(i => i >= 0);
      if (matches.length !== 1) throw conflict();
      next.splice(matches[0], 1);
    }
    next.push(...desired?.hooks?.[event] || []);
    if (same(existing, next)) continue;
    source = edit(source, ['hooks', event], next.length ? next : undefined);
  }
  if (desired?.version !== undefined) {
    if (value.version !== undefined && value.version !== desired.version) throw conflict();
    source = edit(source, ['version'], desired.version);
  }
  parse(Buffer.from(source));
  return Buffer.from(source);
}
function receiptFile(directory, harness) { hooks.spec(harness); return path.join(directory, harness + '.json'); }
async function readReceipt(directory, harness, workspace) {
  const bytes = await host.snapshot(receiptFile(directory, harness));
  if (!bytes) return { bytes: null, value: null };
  let value;
  try { value = JSON.parse(bytes); } catch { throw conflict(); }
  if (value.schema !== 1 || value.harness !== harness || value.workspace !== workspace ||
    value.target !== path.join(workspace, ...hooks.spec(harness).file) || !/^[a-f0-9]{64}$/.test(value.installation_id) ||
    !value.entries || typeof value.entries !== 'object' || Array.isArray(value.entries) ||
    Object.keys(value.entries).some(event => !hooks.spec(harness).events.includes(event))) throw conflict();
  return { bytes, value };
}
async function plan({ workspace, directory, harness, bindingId, connection, platform, guard = () => {} }) {
  guard();
  if (!path.isAbsolute(workspace) || !path.isAbsolute(directory)) throw conflict();
  const target = path.join(workspace, ...hooks.spec(harness).file);
  const old = await readReceipt(directory, harness, workspace), before = await host.snapshot(target);
  if (old.value && old.value.binding_id !== bindingId) throw conflict();
  const installation_id = hooks.hash('agent-hub-hooks-v1:' + bindingId + ':' + harness);
  const configuration = hooks.configuration(connection, harness, installation_id, platform);
  const next = merge(before, configuration, old.value);
  const receipt = { schema: 1, binding_id: bindingId, workspace, harness, target, installation_id,
    installed_sha256: hooks.hash(next), entries: configuration.hooks };
  const preview_hash = hooks.hash(JSON.stringify({ target, previous: before ? hooks.hash(before) : null, receipt }));
  guard();
  return { target, before, next, old, receipt, preview_hash, configuration };
}
async function install(options, expected) {
  const p = await plan(options);
  if (p.preview_hash !== expected) throw new HubError('HOOK_PREVIEW_STALE');
  const receipt = receiptFile(options.directory, options.harness);
  const result = await host.applyPlans([{ target: p.target, skill: null, changes: [
    { file: p.target, previous: p.before, next: p.next },
    { file: receipt, previous: p.old.bytes, next: Buffer.from(JSON.stringify(p.receipt)) },
  ] }], options.guard, { backupDirectory: path.join(options.directory, 'backups') });
  return { installed: true, changed: result.changed, receipt_recorded: true, target: p.target,
    installation_id: p.receipt.installation_id, state: 'awaiting_host', requires_host_trust: true };
}
async function status({ workspace, directory, harness }) {
  const record = await readReceipt(directory, harness, workspace);
  if (!record.value || record.value.uninstalled) return { harness, installed: false, state: 'not_installed' };
  const before = await host.snapshot(record.value.target);
  try { merge(before, null, record.value); }
  catch { return { harness, installed: true, state: 'configuration_changed', configuration_matches: false }; }
  return { harness, installed: true, state: 'awaiting_host', configuration_matches: true,
    installation_id: record.value.installation_id, target: record.value.target };
}
async function uninstall({ workspace, hookDirectory, harness: selected, guard = () => {} }) {
  const result = { removed_entries: 0, conflicts: [] };
  if (!hookDirectory) return result;
  for (const harness of Object.keys(hooks.catalog).filter(key => hooks.catalog[key].file && (!selected || key === selected))) try {
    const record = await readReceipt(hookDirectory, harness, workspace);
    if (!record.value) continue;
    const before = await host.snapshot(record.value.target);
    if (before) {
      const next = merge(before, null, record.value);
      guard(); await host.replace(record.value.target, next, before);
      result.removed_entries += Object.values(record.value.entries).reduce((n, entries) => n + entries.length, 0);
    }
    // Persist a completed removal so retries are idempotent and cannot reclaim user entries.
    const nextReceipt = { ...record.value, entries: {}, uninstalled: true };
    guard(); await host.replace(receiptFile(hookDirectory, harness), Buffer.from(JSON.stringify(nextReceipt)), record.bytes);
  } catch (error) { result.conflicts.push({ component: 'hooks:' + harness, code: error.code || 'INVALID_INSTALL_RECEIPT' }); }
  return result;
}
async function ownedPaths({ workspace, hookDirectory }) {
  if (!hookDirectory) return [];
  const result = [];
  for (const harness of Object.keys(hooks.catalog).filter(key => hooks.catalog[key].file)) {
    const record = await readReceipt(hookDirectory, harness, workspace);
    if (record.value && !record.value.uninstalled) result.push(path.relative(workspace, record.value.target).split(path.sep).join('/'));
  }
  return result;
}
module.exports = { plan, install, status, uninstall, ownedPaths, merge, readReceipt };
