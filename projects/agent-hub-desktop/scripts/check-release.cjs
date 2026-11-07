'use strict';

// Read-only release health check; suitable for an operator's monitoring system.
const { publicKey, url } = require('../core/update-channel.cjs');
const { verifiedRelease, verifyFreshness } = require('../core/update-integrity.cjs');

async function main() {
  const response = await fetch(new URL('stable.json', url), {
    redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(10000),
  });
  if (!response.ok || !response.body) throw new Error('Release endpoint unavailable');
  const chunks = [];
  let bytes = 0;
  for await (const chunk of response.body) {
    bytes += chunk.byteLength;
    if (bytes > 16384) throw new Error('Release metadata exceeds size limit');
    chunks.push(chunk);
  }
  const release = verifiedRelease(JSON.parse(Buffer.concat(chunks)), publicKey);
  const { expires } = verifyFreshness(release);
  const hours = Math.floor((expires - Date.now()) / 3600000);
  process.stdout.write(JSON.stringify({ version: release.version, metadata_version: release.metadata_version,
    expires_at: release.expires_at, remaining_hours: hours }) + '\n');
  if (hours < 72) { process.stderr.write('Renew the signed release metadata within 72 hours.\n'); process.exitCode = 2; }
}

main().catch(error => { process.stderr.write(`${error.code || error.message}\n`); process.exitCode = 1; });
