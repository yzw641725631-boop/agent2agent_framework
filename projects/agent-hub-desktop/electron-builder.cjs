'use strict';

module.exports = {
  appId: 'org.agenthub.desktop',
  productName: 'Agent Hub',
  directories: { output: 'dist/installers', buildResources: 'assets' },
  artifactName: 'AgentHub-Setup-${version}.${ext}',
  // The portable tree already contains the final icon, metadata and integrity manifest.
  win: { icon: 'assets/agent-hub.ico', target: [{ target: 'nsis', arch: ['x64'] }],
    signAndEditExecutable: false },
  nsis: {
    oneClick: false, perMachine: false, allowElevation: false, packElevateHelper: false,
    allowToChangeInstallationDirectory: false,
    createDesktopShortcut: true, createStartMenuShortcut: true,
    shortcutName: 'Agent Hub', uninstallDisplayName: 'Agent Hub',
    installerIcon: 'assets/agent-hub.ico', uninstallerIcon: 'assets/agent-hub.ico',
    installerLanguages: ['zh_CN', 'en_US'], language: '2052',
    deleteAppDataOnUninstall: false, runAfterFinish: true,
    include: 'scripts/installer.nsh',
  },
  publish: null,
};
