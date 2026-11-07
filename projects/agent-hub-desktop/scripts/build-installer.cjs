'use strict';

const path = require('node:path');
const fs = require('node:fs/promises');
const { spawn } = require('node:child_process');
const { version } = require('../package.json');
const { hashFile, readJson, verifyTree } = require('../core/update-integrity.cjs');

async function main() {
  if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('Build on Windows x64');
  const root = path.resolve(__dirname, '..');
  const bundle = path.join(root, 'dist', `AgentHub-${version}-win-x64`);
  const manifest = path.join(bundle, 'files.sha256.json');
  const integrity = { version, files_sha256: await hashFile(manifest) };
  await verifyTree(bundle, integrity);
  const config = require('../electron-builder.cjs');
  const builder = require.resolve('electron-builder/cli.js');
  const buildCache = path.resolve(root, '..', '..', '.local', 'windows-builder-cache');
  const buildTemp = path.resolve(root, '..', '..', '.local', 'windows-builder-temp');
  await fs.mkdir(buildTemp, { recursive: true });
  const child = spawn(process.execPath, [builder, '--prepackaged', bundle, '--win', 'nsis', '--x64',
    '--config', path.join(root, 'electron-builder.cjs'), '--publish', 'never'],
  { cwd: root, stdio: 'inherit', windowsHide: true, env: { ...process.env,
    CSC_IDENTITY_AUTO_DISCOVERY: 'false', ELECTRON_BUILDER_CACHE: buildCache,
    TEMP: buildTemp, TMP: buildTemp } });
  const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
  if (code !== 0) throw new Error('Installer build did not complete');
  await verifyTree(bundle, integrity);
  const file = path.join(root, config.directories.output, `AgentHub-Setup-${version}.exe`);
  const result = { version, file, bytes: (await fs.stat(file)).size, sha256: await hashFile(file),
    type: 'Windows x64 per-user NSIS installer', authenticode_signed: false };
  await fs.writeFile(file + '.sha256', `${result.sha256}  ${path.basename(file)}\n`);
  await fs.writeFile(path.join(root, config.directories.output, 'installer.json'), JSON.stringify(result, null, 2));
  const pkg = await readJson(path.join(bundle, 'resources', 'app', 'package.json'));
  if (pkg.version !== version) throw new Error('Installer version mismatch');
  process.stdout.write(JSON.stringify(result) + '\n');
}
main().catch(error => { process.stderr.write(error.message + '\n'); process.exitCode = 1; });
