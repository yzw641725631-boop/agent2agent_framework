'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const { version } = require('../package.json');
const { publicKey } = require('../core/update-channel.cjs');
const { hashFile, releaseName, compareVersions, verifiedRelease, verifyFreshness, readJson, verifyTree, writeJson } =
  require('../core/update-integrity.cjs');

async function main(keyPath, options = {}) {
  if (!keyPath || !path.isAbsolute(keyPath)) throw new Error('Pass the absolute path to the protected release key');
  const key = crypto.createPrivateKey(await fs.readFile(keyPath));
  const pinned = crypto.createPublicKey(publicKey).export({ type: 'spki', format: 'der' });
  if (!crypto.createPublicKey(key).export({ type: 'spki', format: 'der' }).equals(pinned)) {
    throw new Error('Release key does not match the public key built into the client');
  }
  const directory = path.resolve(__dirname, '..', 'dist');
  // Preserve the published feed locally before signing, including renewals of the
  // same ZIP. Never reset this sequence when creating a new software version.
  const stateFile = path.join(directory, 'stable.json');
  let previous;
  try { previous = verifiedRelease(await readJson(stateFile), publicKey); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (options.previous) {
    const published = verifiedRelease(await readJson(path.resolve(options.previous)), publicKey);
    if (!previous || (published.metadata_version || 0) > (previous.metadata_version || 0)) previous = published;
  }
  const sequence = options.sequence === undefined ? (previous?.metadata_version || 0) + 1 : Number(options.sequence);
  const days = options.days === undefined ? 30 : Number(options.days);
  if (!Number.isSafeInteger(sequence) || sequence <= (previous?.metadata_version || 0) ||
      !Number.isFinite(days) || days < 1 || days > 31) throw new Error('Invalid metadata sequence or lifetime');
  const issued = new Date();
  const name = releaseName(version);
  const archive = path.join(directory, `${name}.zip`);
  const bundle = path.join(directory, name);
  const release = { schema: 1, channel: 'stable', platform: 'win-x64', version,
    metadata_version: sequence, issued_at: issued.toISOString(),
    expires_at: new Date(issued.getTime() + days * 86400000).toISOString(),
    bytes: (await fs.stat(archive)).size, sha256: await hashFile(archive),
    files_sha256: await hashFile(path.join(bundle, 'files.sha256.json')) };
  await verifyTree(bundle, release);
  const payload = Buffer.from(JSON.stringify(release));
  const envelope = { payload: payload.toString('base64url'),
    signature: crypto.sign(null, payload, key).toString('base64url') };
  verifiedRelease(envelope, publicKey);
  verifyFreshness(release, previous);
  if (previous && compareVersions(version, previous.version) < 0) {
    throw new Error('Refusing to sign an older application version');
  }
  if (previous?.version === version && (previous.sha256 !== release.sha256 ||
      previous.files_sha256 !== release.files_sha256 || previous.bytes !== release.bytes)) {
    throw new Error('Do not rebuild an already signed application version');
  }
  await writeJson(stateFile, envelope);
  process.stdout.write(JSON.stringify(release) + '\n');
}

const args = process.argv.slice(3), options = {};
for (let i = 0; i < args.length; i += 2) {
  const name = args[i].slice(2);
  if (!args[i].startsWith('--') || !['previous', 'sequence', 'days'].includes(name) || !args[i + 1]) {
    throw new Error('Use --previous <saved-feed> --sequence <increasing-integer> --days <1..31>');
  }
  options[name] = args[i + 1];
}
main(process.argv[2], options).catch(error => {
  process.stderr.write(`${error.code || error.message}\n`); process.exitCode = 1;
});
