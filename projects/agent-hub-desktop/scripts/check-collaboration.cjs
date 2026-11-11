'use strict';

// Run with the Electron executable, with ELECTRON_RUN_AS_NODE unset.
const { app, safeStorage } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');

if (!app || process.platform !== 'win32' || process.env.HUB_TEST_HTTP !== 'collaboration-only') {
  throw new Error('Use Electron on Windows and explicitly opt in to the disposable collaboration fixture.');
}
const reportFile = process.env.HUB_COLLABORATION_REPORT;
if (!reportFile || !path.isAbsolute(reportFile)) throw new Error('HUB_COLLABORATION_REPORT must be an absolute output file path.');
app.disableHardwareAcceleration();
const profile = path.join(os.tmpdir(), 'hub-collaboration-electron-' + crypto.randomUUID());
app.setPath('userData', profile);
app.whenReady().then(async () => {
  const report = await require('../tests/collaboration.integration.cjs').run({
    protector: safeStorage, protectorName: 'Electron-Windows-DPAPI',
  });
  await fs.writeFile(reportFile, JSON.stringify(report, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  console.log('COLLABORATION_RESULT ' + JSON.stringify(report));
  app.exit(0);
}).catch(error => {
  console.error(error.code || error.name, error.stack);
  app.exit(1);
});
