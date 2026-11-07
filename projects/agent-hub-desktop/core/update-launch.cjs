'use strict';

const { spawn } = require('node:child_process');
const { once } = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');
const { setTimeout: delay } = require('node:timers/promises');
const { readJson, writeJson } = require('./update-integrity.cjs');

function launchEnvironment(runAsNode = false) {
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !/^(electron_run_as_node|node_options|node_path)$/i.test(key)));
  if (runAsNode) env.ELECTRON_RUN_AS_NODE = '1';
  return env;
}

function startSupervisor(previous, directory, release) {
  const child = spawn(previous, [__filename, String(process.pid), previous, directory,
    release.executable, release.id, release.version, release.hidden ? 'hidden' : 'visible'], {
    detached: true, stdio: 'ignore', windowsHide: true, cwd: path.dirname(previous), env: launchEnvironment(true),
  });
  return once(child, 'spawn').then(() => child.unref());
}

function isAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { return error.code !== 'ESRCH'; }
}

async function supervise([parentPid, previous, directory, candidate, id, version, visibility]) {
  const pid = Number(parentPid);
  if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error('Invalid previous process');
  const exitDeadline = Date.now() + 90000;
  while (isAlive(pid)) {
    if (Date.now() >= exitDeadline) throw new Error('Previous release is still running');
    await delay(200);
  }
  const root = path.join(directory, 'desktop-updates');
  const dataOption = `--user-data-dir=${directory}`;
  const launchArgs = visibility === 'hidden' ? ['--hidden'] : [];
  const child = spawn(candidate, [dataOption, `--agent-hub-activation=${id}`, ...launchArgs], {
    stdio: 'ignore', windowsHide: true, cwd: path.dirname(candidate), env: launchEnvironment(),
  });
  let exited = false;
  const stopped = new Promise(resolve => {
    const finish = () => { exited = true; resolve(); };
    child.once('error', finish);
    child.once('exit', finish);
  });
  const startDeadline = Date.now() + 180000;
  while (Date.now() < startDeadline) {
    const active = await readJson(path.join(root, 'active.json')).catch(() => null);
    if (active?.id === id) { child.unref(); return; }
    if (exited) break;
    await delay(250);
  }
  if (!exited) {
    child.kill();
    await Promise.race([stopped, delay(5000, undefined, { ref: false })]);
  }
  await writeJson(path.join(root, 'failed.json'), { version });
  await fs.unlink(path.join(root, 'pending.json')).catch(() => {});
  const recovered = spawn(previous, [dataOption, ...launchArgs], {
    detached: true, stdio: 'ignore', windowsHide: true, cwd: path.dirname(previous), env: launchEnvironment(),
  });
  await once(recovered, 'spawn');
  recovered.unref();
}

if (require.main === module) supervise(process.argv.slice(2)).catch(() => { process.exitCode = 1; });
module.exports = { startSupervisor };
