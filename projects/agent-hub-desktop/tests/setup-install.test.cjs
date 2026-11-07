'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const toml = require('smol-toml');
const jsonc = require('jsonc-parser');
const { install, merge } = require('../core/host-install.cjs');

const entry = { type: 'stdio', command: 'C:\\Program Files\\Agent Hub\\Agent Hub.exe',
  args: ['bridge.cjs', '--setup', '--connection-id', 'one'], env: { ELECTRON_RUN_AS_NODE: '1' } };
async function fixture(t) {
  const base = await fs.realpath(os.tmpdir()), root = await fs.mkdtemp(path.join(base, 'agent-hub-install-'));
  const home = path.join(root, '用户 home'), directory = path.join(root, 'data');
  await fs.mkdir(home); await fs.mkdir(directory);
  const skillDirectory = path.resolve(__dirname, '../../../skills/agent-hub');
  const options = { home, receiptDirectory: path.join(directory, 'setup-installations'), skillDirectory,
    hosts: ['codex', 'claude', 'cursor'], name: 'agent-hub-setup', entry };
  t.after(async () => {
    const actual = await fs.realpath(root); assert.equal(path.dirname(actual), base);
    assert.match(path.basename(actual), /^agent-hub-install-/);
    await fs.rm(actual, { recursive: true, force: true });
  });
  return { root, home, directory, options };
}

test('three host installs retain existing settings and comments and repeat without changes', async t => {
  const f = await fixture(t);
  await fs.mkdir(path.join(f.home, '.codex'));
  const codex = '# keep my settings\nmodel = "my-model"\n[mcp_servers.other]\ncommand = "other.exe"\n';
  await fs.writeFile(path.join(f.home, '.codex/config.toml'), codex);
  await fs.writeFile(path.join(f.home, '.claude.json'), '{\n// retain my preference\n"theme":"dark",\n"mcpServers":{"other":{"command":"other.exe"}},\n}\n');
  const first = await install(f.options), second = await install(f.options);
  assert.equal(first.targets.length, 3); assert.equal(first.changed, true); assert.equal(second.changed, false);
  const source = await fs.readFile(path.join(f.home, '.codex/config.toml'), 'utf8');
  assert.ok(source.startsWith(codex)); assert.equal(toml.parse(source).mcp_servers.other.command, 'other.exe');
  assert.equal(toml.parse(source).mcp_servers['agent-hub-setup'].command, entry.command);
  const claude = await fs.readFile(path.join(f.home, '.claude.json'), 'utf8');
  assert.match(claude, /retain my preference/); assert.equal(jsonc.parse(claude).theme, 'dark');
  assert.equal(jsonc.parse(claude).mcpServers.other.command, 'other.exe');
  assert.equal(jsonc.parse(await fs.readFile(path.join(f.home, '.cursor/mcp.json'), 'utf8')).mcpServers['agent-hub-setup'].command, entry.command);
  await fs.access(path.join(f.home, '.agents/skills/agent-hub/SKILL.md'));
  await fs.access(path.join(f.home, '.claude/skills/agent-hub/references/work-records.md'));
  assert.ok(first.backups.length >= 2); assert.equal(second.backups.length, 0);
});

test('custom CODEX_HOME is honored without writing the default Codex locations', async t => {
  const f = await fixture(t), codexHome = path.join(f.root, 'custom codex');
  await install({ ...f.options, hosts: ['codex'], codexHome });
  await fs.access(path.join(codexHome, 'config.toml'));
  await fs.access(path.join(codexHome, 'skills/agent-hub/SKILL.md'));
  await assert.rejects(fs.access(path.join(f.home, '.codex')), { code: 'ENOENT' });
});

test('a conflict in any selected host leaves every host and Skill untouched', async t => {
  const f = await fixture(t);
  const file = path.join(f.home, '.claude.json');
  const original = '{"mcpServers":{"agent-hub-setup":{"command":"user-owned.exe"}}}';
  await fs.writeFile(file, original);
  await assert.rejects(install(f.options), { code: 'CONFIGURATION_CONFLICT' });
  assert.equal(await fs.readFile(file, 'utf8'), original);
  await assert.rejects(fs.access(path.join(f.home, '.codex')), { code: 'ENOENT' });
  await assert.rejects(fs.access(path.join(f.home, '.agents')), { code: 'ENOENT' });
});

test('only a previously owned entry and unmodified Skill can be upgraded', async t => {
  const f = await fixture(t); await install(f.options);
  const next = { ...entry, args: ['new-bridge.cjs'] };
  assert.equal((await install({ ...f.options, entry: next })).changed, true);
  const file = path.join(f.home, '.claude/skills/agent-hub/SKILL.md');
  await fs.appendFile(file, '\nMy personal changes.\n');
  await assert.rejects(install(f.options), { code: 'CONFIGURATION_CONFLICT' });
  assert.match(await fs.readFile(file, 'utf8'), /My personal changes/);
});

test('invalid JSONC and duplicate keys are rejected while valid inline TOML keeps its settings', () => {
  for (const source of ['{"mcpServers":{},"mcpServers":{}}', '{invalid', '[]']) {
    assert.throws(() => merge(source, 'cursor', 'agent-hub-setup', entry), { code: 'CONFIGURATION_CONFLICT' });
  }
  const inline = merge('model="keep"\nmcp_servers = {other = { command = "other.exe" }}\n', 'codex', 'agent-hub-setup', entry);
  assert.equal(toml.parse(inline.next).model, 'keep');
  assert.equal(toml.parse(inline.next).mcp_servers.other.command, 'other.exe');
  assert.equal(toml.parse(inline.next).mcp_servers['agent-hub-setup'].command, entry.command);
});

test('a stopped authorization during file writes restores the previous files', async t => {
  const f = await fixture(t), file = path.join(f.home, '.claude.json');
  await fs.writeFile(file, '{"theme":"dark"}\n');
  let checks = 0;
  await assert.rejects(install({ ...f.options, hosts: ['claude'], guard() {
    if (++checks === 3) throw Object.assign(new Error('signed out'), { code: 'LOGIN_REQUIRED' });
  } }), { code: 'LOGIN_REQUIRED' });
  assert.equal(await fs.readFile(file, 'utf8'), '{"theme":"dark"}\n');
  await assert.rejects(fs.access(path.join(f.home, '.claude/skills/agent-hub/SKILL.md')), { code: 'ENOENT' });
});

test('junctions in configuration directories are rejected without writing through them', async t => {
  const f = await fixture(t), outside = path.join(f.root, 'outside');
  await fs.mkdir(outside);
  await fs.symlink(outside, path.join(f.home, '.cursor'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(install({ ...f.options, hosts: ['cursor'] }), { code: 'CONFIGURATION_CONFLICT' });
  assert.deepEqual(await fs.readdir(outside), []);
});
