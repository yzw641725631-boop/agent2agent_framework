'use strict';

const path = require('node:path');
const os = require('node:os');
const { install, hosts, snapshot } = require('./host-install.cjs');
const { version } = require('../package.json');

async function configure({ directory, home = os.homedir(), codexHome = process.env.CODEX_HOME, executable,
  selected, configFile, skillDirectory = path.resolve(__dirname, '../skills/agent-hub'),
  auditSkillDirectory = path.resolve(__dirname, '../skills/agent-hub-code-review') }) {
  if (!path.isAbsolute(directory) || !path.isAbsolute(home) || !path.isAbsolute(executable) ||
      codexHome && !path.isAbsolute(codexHome) || !selected?.length || selected.some(host => !hosts.includes(host))) {
    throw new Error('Invalid installation options');
  }
  const bytes = await snapshot(path.join(directory, 'setup-mcp.json'));
  if (!bytes) throw new Error('请先启动新版 Agent Hub，等待一键安装连接就绪后重试。');
  const descriptor = JSON.parse(bytes.toString('utf8'));
  if (descriptor.version !== 1 || descriptor.scope !== 'setup' || descriptor.protocol !== 'desktop-mcp-v1' ||
      !/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(descriptor.binding)) throw new Error('Invalid setup connection');
  const entry = { type: 'stdio', command: executable,
    args: [path.join(__dirname, 'bridge.cjs'), '--binding', path.join(directory, 'setup-mcp.json'),
      '--setup', '--connection-id', descriptor.binding], env: { ELECTRON_RUN_AS_NODE: '1' } };
  const result = await install({ hosts: selected, home, codexHome, configFile,
    receiptDirectory: path.join(directory, 'setup-installations'), skillDirectory,
    additionalSkills:[{name:'agent-hub-code-review',directory:auditSkillDirectory}],name: 'agent-hub-setup', entry });
  return { version, ...result, message: '桌面端、Skill 和 MCP 已准备。请在 PC 端完成本人登录，并重新加载开发工具。' };
}

async function main(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 2) {
    if (!['--data-dir', '--home', '--codex-home', '--config-file', '--executable', '--hosts'].includes(argv[index]) || !argv[index + 1]) throw new Error('Invalid arguments');
    options[argv[index]] = argv[index + 1];
  }
  const result = await configure({ directory: options['--data-dir'], home: options['--home'],
    codexHome: options['--codex-home'], configFile:options['--config-file'], executable: options['--executable'], selected: options['--hosts']?.split(',') });
  process.stdout.write(JSON.stringify(result) + '\n');
}

if (require.main === module) main(process.argv.slice(2)).catch(error => {
  process.stderr.write((error.code || 'SETUP_CONFIGURATION_FAILED') + ': ' +
    (error.code ? error.message : '配置未完成，原有设置已保留。请检查安装路径及同名 Skill、MCP 配置后重试。') + '\n');
  process.exitCode = 1;
});
module.exports = { configure };
