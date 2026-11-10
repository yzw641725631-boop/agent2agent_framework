'use strict';

const crypto = require('node:crypto');
const path = require('node:path');

// The release tree contains physical files, including any future ASAR bundles.
const releaseFs = process.type === 'browser' ? require('original-fs') : require('node:fs');
const fs = releaseFs.promises;
const maxArchiveBytes = 512 * 1024 * 1024;
const maxExpandedBytes = 1024 * 1024 * 1024;
const sha256 = /^[a-f0-9]{64}$/;
const versionPattern = /^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$/;
const clockSkewMs = 5 * 60 * 1000;
const maxMetadataAgeMs = 31 * 24 * 60 * 60 * 1000;

class MetadataError extends Error {
  constructor(code) { super(code); this.code = code; }
}

function metadataTime(value) {
  const time = typeof value === 'string' ? Date.parse(value) : NaN;
  if (!Number.isFinite(time) || new Date(time).toISOString() !== value) {
    throw new MetadataError('UPDATE_METADATA_INVALID');
  }
  return time;
}

// Artifact verification below also accepts historical releases for startup/recovery.
// Freshness applies only to an online feed used to authorize normal project work.
function verifyFreshness(release, previous, { time = Date.now(), checkedAt = 0 } = {}) {
  if((release.root_version || 1)<(previous?.root_version || 1))throw new MetadataError('UPDATE_TRUST_ROLLBACK');
  if(release.root_expires_at && (metadataTime(release.root_expires_at)<=time ||
    metadataTime(release.root_issued_at)>time+clockSkewMs))throw new MetadataError('UPDATE_TRUST_EXPIRED');
  if (!Number.isSafeInteger(release.metadata_version) || release.metadata_version < 1) {
    throw new MetadataError('UPDATE_METADATA_INVALID');
  }
  const issued = metadataTime(release.issued_at), expires = metadataTime(release.expires_at);
  if (expires <= issued || expires - issued > maxMetadataAgeMs) {
    throw new MetadataError('UPDATE_METADATA_INVALID');
  }
  if (!Number.isFinite(time) || time + clockSkewMs < checkedAt || issued > time + clockSkewMs) {
    throw new MetadataError('UPDATE_CLOCK_INVALID');
  }
  if (expires <= time) throw new MetadataError('UPDATE_METADATA_EXPIRED');
  if (previous?.metadata_version) {
    if (release.metadata_version < previous.metadata_version ||
        issued < metadataTime(previous.issued_at)) throw new MetadataError('UPDATE_METADATA_ROLLBACK');
    if (release.metadata_version === previous.metadata_version &&
        (release.issued_at !== previous.issued_at || release.expires_at !== previous.expires_at ||
         release.version !== previous.version || release.bytes !== previous.bytes ||
         release.sha256 !== previous.sha256 || release.files_sha256 !== previous.files_sha256)) {
      throw new MetadataError('UPDATE_METADATA_REUSED');
    }
  }
  return { issued, expires };
}

function compareVersions(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string' ||
      !versionPattern.test(left) || !versionPattern.test(right)) throw new Error('Invalid release version');
  const a = left.split('.').map(Number);
  const b = right.split('.').map(Number);
  for (let index = 0; index < a.length; index++) {
    if (a[index] !== b[index]) return Math.sign(a[index] - b[index]);
  }
  return 0;
}

function releaseName(version) {
  compareVersions(version, version);
  return `AgentHub-${version}-win-x64`;
}

function verifiedRelease(envelope, publicKey) {
  if (typeof envelope?.payload !== 'string' || envelope.payload.length > 4096 ||
      envelope.signature!==undefined && (typeof envelope.signature !== 'string' || envelope.signature.length > 128)) {
    throw new Error('Invalid release envelope');
  }
  const payload = Buffer.from(envelope.payload, 'base64url');
  const root=require('./update-trust.cjs').verifyEnvelope(envelope,publicKey,payload);
  const release = JSON.parse(payload.toString('utf8'));
  if (release.schema !== 1 || release.channel !== 'stable' || release.platform !== 'win-x64' ||
      !Number.isSafeInteger(release.bytes) || release.bytes <= 0 || release.bytes > maxArchiveBytes ||
      typeof release.sha256 !== 'string' || !sha256.test(release.sha256) ||
      typeof release.files_sha256 !== 'string' || !sha256.test(release.files_sha256)) {
    throw new Error('Invalid signed release fields');
  }
  releaseName(release.version);
  Object.defineProperties(release,{root_version:{value:root.version,enumerable:false},
    root_expires_at:{value:root.expires_at,enumerable:false},root_issued_at:{value:root.issued_at,enumerable:false}});
  return release;
}

async function hashFile(file) {
  const hash = crypto.createHash('sha256');
  for await (const chunk of releaseFs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

async function readSmallFile(file, maxBytes = 16384) {
  const info = await fs.lstat(file);
  if (!info.isFile() || info.size > maxBytes) throw new Error('Invalid release state file');
  return fs.readFile(file);
}

async function readJson(file, maxBytes=16384) {
  return JSON.parse(await readSmallFile(file,maxBytes));
}

async function writeJson(file, value) {
  const temporary = `${file}.${crypto.randomUUID()}.tmp`;
  try {
    await fs.writeFile(temporary, JSON.stringify(value), { flag: 'wx', mode: 0o600 });
    await fs.rename(temporary, file);
  } finally {
    await fs.unlink(temporary).catch(() => {});
  }
}

async function verifyTree(directory, release) {
  if (!(await fs.lstat(directory)).isDirectory()) throw new Error('Invalid release directory');
  const manifest = await readSmallFile(path.join(directory, 'files.sha256.json'), 1024 * 1024);
  if (crypto.createHash('sha256').update(manifest).digest('hex') !== release.files_sha256) {
    throw new Error('Invalid release file manifest');
  }
  const rows = JSON.parse(manifest);
  if (!Array.isArray(rows) || rows.length < 1 || rows.length > 4096) {
    throw new Error('Invalid release file count');
  }
  const expected = new Map();
  for (const row of rows) {
    if (typeof row?.path !== 'string' || typeof row.sha256 !== 'string' ||
        !sha256.test(row.sha256) || expected.has(row.path)) throw new Error('Invalid release file entry');
    expected.set(row.path, row.sha256);
  }
  for (const required of ['Agent Hub.exe', 'resources/app/package.json', 'resources/app/main.cjs',
    'resources/app/core/bridge.cjs']) {
    if (!expected.has(required)) throw new Error('Incomplete release');
  }
  let bytes = 0;
  async function walk(folder, prefix = '') {
    for (const entry of await fs.readdir(folder, { withFileTypes: true })) {
      const relative = prefix + entry.name;
      const file = path.join(folder, entry.name);
      if (entry.isDirectory()) {
        await walk(file, `${relative}/`);
        continue;
      }
      if (!entry.isFile()) throw new Error('Unexpected release link or special file');
      if (relative === 'files.sha256.json') continue;
      const digest = expected.get(relative);
      bytes += (await fs.stat(file)).size;
      if (!digest || bytes > maxExpandedBytes || await hashFile(file) !== digest) {
        throw new Error('Unexpected or damaged release file');
      }
      expected.delete(relative);
    }
  }
  await walk(directory);
  if (expected.size) throw new Error('Missing release files');
  const pkg = await readJson(path.join(directory, 'resources/app/package.json'));
  if (pkg.name !== 'agent-hub-desktop' || pkg.version !== release.version || pkg.main !== 'main.cjs') {
    throw new Error('Unexpected release identity');
  }
  return path.join(directory, 'Agent Hub.exe');
}

module.exports = { compareVersions, releaseName, verifiedRelease, verifyFreshness, MetadataError,
  hashFile, readJson, writeJson, verifyTree, maxExpandedBytes, releaseFs };
