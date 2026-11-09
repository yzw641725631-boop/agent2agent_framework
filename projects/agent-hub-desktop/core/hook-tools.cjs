'use strict';
const { HubError } = require('./remote.cjs');
const { catalog } = require('./host-hooks.cjs');
const harness = { type: 'string', enum: Object.keys(catalog).filter(key => catalog[key].file) };
const tools = [
  ['hub_preview_hooks', '预览逐轮检查安装', '预览当前固定工程的钩子合并与修改前检查，不改变信任或宿主权限。'],
  ['hub_install_hooks', '安装逐轮检查', '使用最新 preview_hash 安全合并钩子，保存归属收据。需在宿主正常信任并重新加载；安装不表示事件已经触发。'],
  ['hub_get_hook_status', '检查逐轮覆盖', '查看配置是否匹配、实际触发过哪些事件，以及宿主未加载、已修改和覆盖不足状态。'],
  ['hub_uninstall_hooks', '卸载本工程钩子', '使用状态返回的 uninstall_hash 作为 preview_hash，仅移除所选宿主本连接登记的条目。冲突保留，其他配置保留。'],
].map(([name, title, description]) => ({ name, title, description,
  inputSchema: { type: 'object', properties: { harness,
    ...(['hub_install_hooks','hub_uninstall_hooks'].includes(name) ? { preview_hash: { type:'string',pattern:'^[a-f0-9]{64}$' } } : {}) },
  required: ['harness', ...(['hub_install_hooks','hub_uninstall_hooks'].includes(name) ? ['preview_hash'] : [])], additionalProperties: false },
  annotations: { readOnlyHint: ['hub_preview_hooks','hub_get_hook_status'].includes(name), destructiveHint: name === 'hub_uninstall_hooks', idempotentHint: true },
}));
function validate(name, args) {
  const def = tools.find(tool => tool.name === name);
  if (!def) return null;
  if (!args || Array.isArray(args) || Object.keys(args).some(key => !Object.hasOwn(def.inputSchema.properties,key)) ||
    !harness.enum.includes(args.harness) || def.inputSchema.required.some(key => !Object.hasOwn(args,key)) ||
    args.preview_hash !== undefined && !/^[a-f0-9]{64}$/.test(args.preview_hash)) throw new HubError('INVALID_REQUEST');
  return def;
}
module.exports = { tools, validate };
