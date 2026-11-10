'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');
const toml = require('smol-toml');
const jsonc = require('jsonc-parser');
const { HubError } = require('./remote.cjs');

const os = require('node:os');
const hosts = ['codex', 'claude', 'cursor', 'qoder', 'workbuddy', 'codebuddy', 'trae'];
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
    claude: ['.mcp.json'], cursor: ['.cursor', 'mcp.json'], qoder: ['.qoder', 'settings.local.json'],
    workbuddy: ['.workbuddy', 'mcp.json'], codebuddy: ['.mcp.json'], trae: ['.trae', 'mcp.json'] })[host]);
  if(host==='codex')return path.join(codexHome || path.join(home, '.codex'), 'config.toml');
  if(host==='claude')return path.join(home,'.claude.json');
  if(host==='qoder')return path.join(home,'.qoder','settings.json');
  if(host==='codebuddy')return path.join(home,'.codebuddy','.mcp.json');
  if(host==='trae') {
    // TRAE's manual editor is authoritative for nonstandard installations. Do not
    // alter any enable/trust flags; the host still asks the user to trust the server.
    const appData=path.resolve(home)===path.resolve(os.homedir()) && process.env.APPDATA ||
      path.join(home,'AppData','Roaming');
    return process.platform==='win32'?path.join(appData,'Trae','User','mcp.json'):
      process.platform==='darwin'?path.join(home,'Library','Application Support','Trae','User','mcp.json'):
      path.join(home,'.config','Trae','User','mcp.json');
  }
  return path.join(home,host==='workbuddy'?'.workbuddy':'.cursor','mcp.json');
}

async function resolvedTarget(host, options) {
  const primary=targetFor(host,options);
  if(host!=='codebuddy')return primary;
  // CodeBuddy reads the first existing config, it does not merge legacy files.
  const candidates=options.workspace?[primary,path.join(options.workspace,'mcp.json')]:
    [primary,path.join(options.home,'.codebuddy','mcp.json'),path.join(options.home,'.codebuddy.json')];
  for(const candidate of candidates)if(await snapshot(candidate))return candidate;
  return primary;
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

async function planHost({ host, home, codexHome, workspace, configFile, receiptDirectory, skillDirectory, additionalSkills=[], name, entry }) {
  if (!/^[a-z][a-z0-9-]{2,79}$/.test(name) || !path.isAbsolute(receiptDirectory)) throw conflict();
  const target = configFile || await resolvedTarget(host, { home, codexHome, workspace });
  if (!path.isAbsolute(target)) throw conflict();
  const receiptFile = path.join(receiptDirectory, host + '.json');
  const receipt = await readReceipt(receiptFile);
  if (receipt.data.target && receipt.data.target !== target) throw conflict();
  const previous = await snapshot(target);
  const merged = merge(decode(previous), host, name, entry, receipt.data);
  const changes = [{ file: target, previous, next: Buffer.from(merged.next) }];
  const files = {};
  let installedSkill = null;
  const skills=[...(skillDirectory?[{name:'agent-hub',directory:skillDirectory}]:[]),...additionalSkills];
  if(new Set(skills.map(s=>s.name)).size!==skills.length)throw conflict();
  for (const skill of skills) {
    if(!/^[a-z][a-z0-9-]{2,63}$/.test(skill.name) || !path.isAbsolute(skill.directory))throw conflict();
    const targetSkill = host === 'codex' ? path.join(codexHome || path.join(home, '.agents'), 'skills', skill.name) :
      path.join(home,({claude:'.claude',cursor:'.cursor',qoder:'.qoder',workbuddy:'.codebuddy',
        codebuddy:'.codebuddy',trae:'.agents'})[host], 'skills', skill.name);
    if(skill.name==='agent-hub')installedSkill=targetSkill;
    for (const source of await skillFiles(skill.directory)) {
      const file = path.join(targetSkill, ...source.name.split('/'));
      const before = await snapshot(file);
      let ownedHash=receipt.data.files?.[file];
      if(!ownedHash && ['codebuddy','workbuddy'].includes(host)) {
        const sibling=await readReceipt(path.join(receiptDirectory,(host==='codebuddy'?'workbuddy':'codebuddy')+'.json'));
        ownedHash=sibling.data.files?.[file];
      }
      if (before && !before.equals(source.bytes) && digest(before) !== ownedHash) throw conflict();
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

async function applyPlans(plans, guard = () => {}, { backupDirectory = null } = {}) {
  const changes = plans.flatMap(plan => plan.changes), written = [], backups = [];
  try {
    for (const change of changes) {
      guard();
      const current = await snapshot(change.file);
      if (!same(current, change.previous)) throw new HubError('CONFIGURATION_CHANGED');
      if (current?.equals(change.next)) continue;
      if (current) {
        const backup = backupDirectory ? path.join(backupDirectory,digest(change.file)+'-'+crypto.randomUUID()+'.bak') :
          change.file + '.agent-hub-' + crypto.randomUUID() + '.bak';
        if(backupDirectory){await safePath(backupDirectory);await fs.mkdir(backupDirectory,{recursive:true,mode:0o700});await safePath(backupDirectory);}
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
  if(options.configFile && (selected.length!==1 || !path.isAbsolute(options.configFile) ||
      path.extname(options.configFile).toLowerCase()!==(selected[0]==='codex'?'.toml':'.json')))throw conflict();
  const plans = [];
  for (const host of selected) plans.push(await planHost({ ...options, host }));
  // Shared Skill directories and project .mcp.json must be written once. Every
  // host keeps its own ownership receipt; conflicting plans abort before mutation.
  const changes=new Map();
  for(const plan of plans)plan.changes=plan.changes.filter(change=>{
    const key=path.resolve(change.file), prior=changes.get(key);
    if(prior) {
      if(!same(prior.previous,change.previous) || !same(prior.next,change.next))throw conflict();
      return false;
    }
    changes.set(key,change);return true;
  });
  return applyPlans(plans, options.guard);
}

function removeEntry(text, host, name, receipt) {
  const bom = text.startsWith('\ufeff') ? '\ufeff' : '', source = text.slice(bom.length);
  let data;
  try {
    if (host === 'codex') data = source.trim() ? toml.parse(source) : {};
    else {
      const errors = [], tree = jsonc.parseTree(source.trim() ? source : '{}', errors, { allowTrailingComma: true });
      if (errors.length || tree?.type !== 'object') throw conflict();
      uniqueJson(tree); data = jsonc.getNodeValue(tree);
    }
  } catch { throw conflict(); }
  const key = host === 'codex' ? 'mcp_servers' : 'mcpServers';
  const current = data[key]?.[name];
  if (current === undefined) return text;
  if (!equivalent(current, receipt.entry)) throw conflict();
  let next;
  if (host === 'codex') {
    // A missing/edited managed block is a conflict; never rewrite unrelated TOML.
    if (!receipt.block || source.split(receipt.block).length !== 2) throw conflict();
    next = source.replace(receipt.block, '');
  } else next = jsonc.applyEdits(source, jsonc.modify(source, [key, name], undefined,
    { formattingOptions: { insertSpaces: true, tabSize: 2, eol: source.includes('\r\n') ? '\r\n' : '\n' } }));
  const expected = plain(data); delete expected[key][name];
  let parsed;
  try { parsed = host === 'codex' ? toml.parse(next) : jsonc.parse(next); } catch { throw conflict(); }
  // Removing the last TOML table also removes its empty parent.
  if (host === 'codex' && !Object.keys(expected[key]).length && parsed[key] === undefined) delete expected[key];
  if (!equivalent(parsed, expected)) throw conflict();
  return bom + next;
}

function inside(root, file) {
  const relative = path.relative(root, file);
  return relative && !relative.startsWith('..' + path.sep) && relative !== '..' && !path.isAbsolute(relative);
}

async function erase(file, expected, guard) {
  guard(); await safePath(file);
  const current = await snapshot(file);
  if (!current) return false;
  if (!same(current, expected)) throw new HubError('CONFIGURATION_CHANGED');
  guard(); await fs.unlink(file); return true;
}

// All project-specific rules, scripts and tests live below one owned directory.
// The receipt stays in desktop data, never under server-supplied paths.
async function deployFramework({ workspace, receiptDirectory, name, files, guard = () => {} }) {
  if (!/^[a-z][a-z0-9-]{2,79}$/.test(name) || !path.isAbsolute(workspace) || !path.isAbsolute(receiptDirectory) || !Array.isArray(files) || files.length > 1000) throw conflict();
  const root = path.join(workspace, '.agent-hub', 'framework', name), file = path.join(receiptDirectory, 'framework.json');
  const before = await snapshot(file), receipt = before ? JSON.parse(decode(before)) : { files: {} };
  if (before && (receipt.version !== 1 || receipt.workspace !== workspace || receipt.name !== name)) throw conflict();
  const changes = [], owned = { ...receipt.files };
  for (const entry of files) {
    if (typeof entry.path !== 'string' || entry.path.includes('\\') || entry.path.split('/').some(p => !p || p === '.' || p === '..') ||
        !Buffer.isBuffer(entry.bytes) || entry.bytes.length > 2 * 1024 * 1024) throw conflict();
    const target = path.join(root, ...entry.path.split('/'));
    if (!inside(root, target)) throw conflict();
    const previous = await snapshot(target);
    if (previous && !previous.equals(entry.bytes) && digest(previous) !== receipt.files[entry.path]) throw conflict();
    changes.push({ file: target, previous, next: entry.bytes }); owned[entry.path] = digest(entry.bytes);
  }
  changes.push({ file, previous: before, next: Buffer.from(JSON.stringify({ version: 1, workspace, name, files: owned })) });
  return applyPlans([{ target: root, skill: null, changes }], guard, {backupDirectory:path.join(receiptDirectory,'backups')});
}

async function uninstallProject({ workspace, receiptDirectories, hookDirectory, name, installed, guard = () => {} }) {
  if (!path.isAbsolute(workspace) || !/^[a-z][a-z0-9-]{2,79}$/.test(name)) throw conflict();
  await safePath(workspace);
  if (await fs.realpath(workspace) !== workspace) throw conflict();
  const result = { removed_files: 0, removed_entries: 0, conflicts: [], retained_shared_components: true };
  const removedHooks = await require('./hook-install.cjs').uninstall({workspace,hookDirectory,guard});
  result.removed_entries += removedHooks.removed_entries;
  result.conflicts.push(...removedHooks.conflicts);
  const receipts = [], framework = [];
  for (const directory of receiptDirectories) {
    if (!path.isAbsolute(directory)) throw conflict();
    for (const host of hosts) {
      try {
        const { data, previous } = await readReceipt(path.join(directory, host + '.json'));
        if (!previous) continue;
        if (data.host !== host || data.name !== name || data.target !== await resolvedTarget(host, { workspace }) ||
            Object.keys(data.files).length || data.skill) throw conflict();
        receipts.push(data);
      } catch (error) { result.conflicts.push({ component: host, code: error.code || 'INVALID_INSTALL_RECEIPT' }); }
    }
    try {
      const bytes = await snapshot(path.join(directory, 'framework.json'));
      if (bytes) {
        const data = JSON.parse(decode(bytes));
        if (data.version !== 1 || data.workspace !== workspace || data.name !== name || !data.files || typeof data.files !== 'object' || Array.isArray(data.files)) throw conflict();
        framework.push(data);
      }
    } catch (error) { result.conflicts.push({ component: 'framework', code: error.code || 'INVALID_INSTALL_RECEIPT' }); }
  }
  if (installed) {
    if (installed.target !== targetFor('claude', { workspace }) || !installed.entry) throw conflict();
    receipts.push({ host: 'claude', target: installed.target, name, entry: installed.entry });
  }
  for (const receipt of receipts) try {
    guard(); const before = await snapshot(receipt.target);
    if (!before) continue;
    const next = Buffer.from(removeEntry(decode(before), receipt.host, name, receipt));
    if (!before.equals(next)) { guard(); await replace(receipt.target, next, before); result.removed_entries++; }
  } catch (error) { result.conflicts.push({ component: receipt.host, code: error.code || 'CONFIGURATION_CONFLICT' }); }
  const root = path.join(workspace, '.agent-hub', 'framework', name), directories = new Set();
  for (const receipt of framework) for (const [relative, sha] of Object.entries(receipt.files)) try {
    if (relative.includes('\\') || relative.split('/').some(p => !p || p === '.' || p === '..') || !/^[a-f0-9]{64}$/.test(sha)) throw conflict();
    const file = path.join(root, ...relative.split('/'));
    if (!inside(root, file)) throw conflict();
    const bytes = await snapshot(file);
    if (!bytes) continue;
    if (digest(bytes) !== sha) throw conflict();
    if (await erase(file, bytes, guard)) result.removed_files++;
    for (let dir = path.dirname(file); inside(root, dir) || dir === root; dir = path.dirname(dir)) directories.add(dir);
  } catch (error) { result.conflicts.push({ component: 'framework', code: error.code || 'CONFIGURATION_CONFLICT' }); }
  // Remove empty, verified directories only. Unknown files keep their directories intact.
  for (const directory of [...directories].sort((a, b) => b.length - a.length)) {
    guard(); await safePath(directory);
    await fs.rmdir(directory).catch(error => { if (!['ENOENT', 'ENOTEMPTY', 'EEXIST'].includes(error.code)) throw error; });
  }
  return { ...result, state: result.conflicts.length ? 'conflicts' : 'complete', recursive_delete_used: false,
    business_files_deleted: false, local_checkpoints_deleted: false };
}

async function ownedProjectPaths({workspace,receiptDirectories,hookDirectory,name,installed}) {
  const paths = new Set();
  if(installed?.target === targetFor('claude',{workspace}) && installed.entry)paths.add('.mcp.json');
  for(const directory of receiptDirectories) {
    for(const host of hosts) {
      const {data,previous} = await readReceipt(path.join(directory,host+'.json'));
      if(previous) {
        if(data.host!==host || data.name!==name || data.target!==await resolvedTarget(host,{workspace}))throw conflict();
        paths.add(path.relative(workspace,data.target).split(path.sep).join('/'));
      }
    }
    const bytes = await snapshot(path.join(directory,'framework.json'));
    if(bytes) {
      const data = JSON.parse(decode(bytes)), root = path.join(workspace,'.agent-hub','framework',name);
      if(data.version!==1 || data.workspace!==workspace || data.name!==name || !data.files || Array.isArray(data.files))throw conflict();
      for(const relative of Object.keys(data.files)) {
        const file = path.join(root,...relative.split('/'));
        if(relative.includes('\\') || relative.split('/').some(p=>!p || p==='.' || p==='..') || !inside(root,file))throw conflict();
        paths.add(path.relative(workspace,file).split(path.sep).join('/'));
      }
    }
  }
  for(const file of await require('./hook-install.cjs').ownedPaths({workspace,hookDirectory}))paths.add(file);
  return [...paths];
}

module.exports = { install, merge, snapshot, replace, hosts, removeEntry, deployFramework, uninstallProject, ownedProjectPaths, applyPlans, safePath };
