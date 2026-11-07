'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');
const toml = require('smol-toml');
const jsonc = require('jsonc-parser');
const { HubError } = require('./remote.cjs');

const hosts = ['codex', 'claude', 'cursor'];
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const same = (left, right) => left === null && right === null || Buffer.isBuffer(left) && Buffer.isBuffer(right) && left.equals(right);
function plain(value) {
  if (Array.isArray(value)) return value.map(plain);
  if (value && typeof value === 'object' && [null, Object.prototype].includes(Object.getPrototypeOf(value))) {
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, plain(child)]));
  }
  return value;
}
const equivalent = (left, right) => isDeepStrictEqual(plain(left), plain(right));
const conflict = () => new HubError('CONFIGURATION_CONFLICT',
  '现有同名配置、Skill 或配置格式无法安全合并，原文件已保留。请核对后重试。');

async function safePath(file) {
  for (let current = path.resolve(file); ; current = path.dirname(current)) {
    try {
      const info = await fs.lstat(current);
      if (info.isSymbolicLink()) throw conflict();
      if (current !== path.resolve(file) && !info.isDirectory()) throw conflict();
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (path.dirname(current) === current) break;
  }
}

async function snapshot(file) {
  await safePath(file);
  try {
    const info = await fs.lstat(file);
    if (!info.isFile() || info.size > 2 * 1024 * 1024) throw conflict();
    return await fs.readFile(file);
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function decode(bytes) {
  try { return bytes ? new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes) : ''; }
  catch { throw conflict(); }
}

function uniqueJson(node) {
  if (node?.type === 'object') {
    const names = new Set();
    for (const property of node.children || []) {
      const name = property.children[0].value;
      if (names.has(name)) throw conflict();
      names.add(name); uniqueJson(property.children[1]);
    }
  } else for (const child of node?.children || []) uniqueJson(child);
}

function merge(text, host, name, rawEntry, receipt = {}) {
  const bom = text.startsWith('\ufeff') ? '\ufeff' : '';
  const original = text.slice(bom.length);
  const entry = host === 'codex' ? { command: rawEntry.command, args: rawEntry.args, env: rawEntry.env } : rawEntry;
  let data, next, block = null;
  try {
    if (host === 'codex') data = original.trim() ? toml.parse(original) : {};
    else {
      const errors = [];
      const tree = jsonc.parseTree(original.trim() ? original : '{}', errors, { allowTrailingComma: true });
      if (errors.length || tree?.type !== 'object') throw conflict();
      uniqueJson(tree); data = jsonc.getNodeValue(tree);
    }
  } catch { throw conflict(); }
  const key = host === 'codex' ? 'mcp_servers' : 'mcpServers';
  if (data[key] !== undefined && (!data[key] || typeof data[key] !== 'object' || Array.isArray(data[key]))) throw conflict();
  const current = data[key]?.[name];
  if (current !== undefined && equivalent(current, entry)) {
    return { next: text, entry, block: receipt.block || null };
  }
  if (current !== undefined && !equivalent(current, receipt.entry)) throw conflict();
  if (host === 'codex') {
    block = `# Agent Hub managed: ${name} BEGIN\n` +
      toml.stringify({ mcp_servers: { [name]: entry } }) + `# Agent Hub managed: ${name} END\n`;
    if (current !== undefined && receipt.block && original.split(receipt.block).length === 2) {
      next = original.replace(receipt.block, block);
    } else if (current === undefined) next = original + (original && !original.endsWith('\n') ? '\n' : '') + '\n' + block;
    try {
      const expected = { ...data, [key]: { ...data[key], [name]: entry } };
      let valid = false;
      if (next !== undefined) {
        try { valid = equivalent(toml.parse(next), expected); } catch { /* Inline tables cannot be extended. */ }
      }
      if (!valid) {
        const remaining = { ...data, [key]: { ...data[key] } }; delete remaining[key][name];
        next = toml.stringify(remaining) + '\n' + block;
        if (!equivalent(toml.parse(next), expected)) throw conflict();
      }
    } catch { throw conflict(); }
  } else {
    const source = original.trim() ? original : '{}\n';
    const options = { formattingOptions: { insertSpaces: true, tabSize: 2, eol: source.includes('\r\n') ? '\r\n' : '\n' } };
    next = jsonc.applyEdits(source, jsonc.modify(source, [key, name], entry, options));
    const errors = [], parsed = jsonc.parse(next, errors, { allowTrailingComma: true });
    if (errors.length || !equivalent(parsed, { ...data, [key]: { ...data[key], [name]: entry } })) throw conflict();
  }
  return { next: bom + next, entry, block };
}

function targetFor(host, { home, codexHome, workspace }) {
  if (!hosts.includes(host)) throw new HubError('UNSUPPORTED_HARNESS');
  if (workspace) return path.join(workspace, ...({ codex: ['.codex', 'config.toml'],
    claude: ['.mcp.json'], cursor: ['.cursor', 'mcp.json'] })[host]);
  return host === 'codex' ? path.join(codexHome || path.join(home, '.codex'), 'config.toml') :
    host === 'claude' ? path.join(home, '.claude.json') : path.join(home, '.cursor', 'mcp.json');
}

async function readReceipt(file) {
  const previous = await snapshot(file);
  if (!previous) return { previous, data: {} };
  try {
    const data = JSON.parse(decode(previous));
    if (data.version !== 1 || !data.files || typeof data.files !== 'object') throw conflict();
    return { previous, data };
  } catch { throw conflict(); }
}

async function skillFiles(directory) {
  const files = [];
  async function walk(current, relative = '') {
    await safePath(current);
    for (const item of await fs.readdir(current, { withFileTypes: true })) {
      const name = relative + item.name, file = path.join(current, item.name);
      if (item.isDirectory()) await walk(file, name + '/');
      else {
        if (!item.isFile() || !/\.(md|yaml)$/.test(item.name)) throw conflict();
        const bytes = await snapshot(file);
        files.push({ name, bytes });
      }
    }
  }
  await walk(directory);
  if (!files.some(file => file.name === 'SKILL.md') || files.reduce((sum, file) => sum + file.bytes.length, 0) > 256 * 1024) throw conflict();
  return files;
}

async function planHost({ host, home, codexHome, workspace, receiptDirectory, skillDirectory, name, entry }) {
  if (!/^[a-z][a-z0-9-]{2,79}$/.test(name) || !path.isAbsolute(receiptDirectory)) throw conflict();
  const target = targetFor(host, { home, codexHome, workspace });
  if (!path.isAbsolute(target)) throw conflict();
  const receiptFile = path.join(receiptDirectory, host + '.json');
  const receipt = await readReceipt(receiptFile);
  if (receipt.data.target && receipt.data.target !== target) throw conflict();
  const previous = await snapshot(target);
  const merged = merge(decode(previous), host, name, entry, receipt.data);
  const changes = [{ file: target, previous, next: Buffer.from(merged.next) }];
  const files = {};
  let installedSkill = null;
  if (skillDirectory) {
    installedSkill = host === 'codex' ? path.join(codexHome || path.join(home, '.agents'), 'skills', 'agent-hub') :
      path.join(home, host === 'claude' ? '.claude' : '.cursor', 'skills', 'agent-hub');
    for (const source of await skillFiles(skillDirectory)) {
      const file = path.join(installedSkill, ...source.name.split('/'));
      const before = await snapshot(file);
      if (before && !before.equals(source.bytes) && digest(before) !== receipt.data.files[file]) throw conflict();
      changes.push({ file, previous: before, next: source.bytes });
      files[file] = digest(source.bytes);
    }
  }
  const record = { version: 1, host, target, name, entry: merged.entry, block: merged.block,
    files, skill: installedSkill };
  changes.push({ file: receiptFile, previous: receipt.previous, next: Buffer.from(JSON.stringify(record, null, 2) + '\n') });
  return { target, skill: installedSkill, changes };
}

async function replace(file, bytes, expected) {
  await safePath(file); await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 }); await safePath(file);
  const temporary = file + '.' + crypto.randomUUID() + '.tmp';
  try {
    await fs.writeFile(temporary, bytes, { flag: 'wx', mode: 0o600 });
    await safePath(file);
    if (expected !== undefined && !same(await snapshot(file), expected)) throw new HubError('CONFIGURATION_CHANGED');
    await fs.rename(temporary, file);
  } finally { await fs.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; }); }
}

async function applyPlans(plans, guard = () => {}) {
  const changes = plans.flatMap(plan => plan.changes), written = [], backups = [];
  try {
    for (const change of changes) {
      guard();
      const current = await snapshot(change.file);
      if (!same(current, change.previous)) throw new HubError('CONFIGURATION_CHANGED');
      if (current?.equals(change.next)) continue;
      if (current) {
        const backup = change.file + '.agent-hub-' + crypto.randomUUID() + '.bak';
        await fs.writeFile(backup, current, { flag: 'wx', mode: 0o600 }); backups.push(backup);
      }
      guard(); await replace(change.file, change.next, change.previous); written.push(change);
    }
    guard();
  } catch (error) {
    let complete = true;
    for (const change of written.reverse()) {
      try {
        if (!(await snapshot(change.file))?.equals(change.next)) { complete = false; continue; }
        if (change.previous === null) await fs.unlink(change.file);
        else await replace(change.file, change.previous, change.next);
      } catch { complete = false; }
    }
    if (!complete) throw new HubError('CONFIGURATION_RECOVERY_REQUIRED', '部分配置在恢复时发生变化，请检查本机备份后重试。');
    throw error;
  }
  return { targets: plans.map(plan => ({ configuration: plan.target, skill: plan.skill })), backups,
    changed: written.length > 0, reload_required: true };
}

async function install(options) {
  const selected = [...new Set(options.hosts)];
  if (!selected.length || selected.some(host => !hosts.includes(host))) throw new HubError('UNSUPPORTED_HARNESS');
  const plans = [];
  for (const host of selected) plans.push(await planHost({ ...options, host }));
  return applyPlans(plans, options.guard);
}

module.exports = { install, merge, snapshot, replace, hosts };
