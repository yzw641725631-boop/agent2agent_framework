'use strict';
const {HubError}=require('./remote.cjs');
const languages={type:'string',enum:['zh','en']};
const properties={doc_name_lang:languages,doc_content_lang:languages};
const tools=[
  {name:'hub_get_roles',title:'读取职业角色',description:'读取内置职业职责、交付物和流程角色。角色不授予人员或仓库权限；每次任务只加载当前角色。',
    inputSchema:{type:'object',properties:{language:languages},additionalProperties:false},annotations:{readOnlyHint:true}},
  {name:'hub_preview_framework',title:'预览项目职责部署',description:'预览固定工程内的角色文档、流程、索引和自检脚本，返回部署摘要。按原 install 的职责与专注要求生成；不启动其他 Agent。',
    inputSchema:{type:'object',properties,additionalProperties:false},annotations:{readOnlyHint:true}},
  {name:'hub_install_framework',title:'部署项目职责框架',description:'使用预览的 preview_hash 部署到本工程专属目录并保存安装收据。保留已有改动，撤权时按收据清理；不会因此启动子 Agent、取得仓库权限或改变当前任务专注。',
    inputSchema:{type:'object',properties:{...properties,preview_hash:{type:'string',pattern:'^[a-f0-9]{64}$'}},required:['preview_hash'],additionalProperties:false},
    annotations:{readOnlyHint:false,destructiveHint:false,idempotentHint:true}},
];
function validate(name,args){
  const def=tools.find(t=>t.name===name);if(!def)return null;
  if(!args || typeof args!=='object' || Array.isArray(args) || Object.keys(args).some(k=>!Object.hasOwn(def.inputSchema.properties,k)) ||
    (def.inputSchema.required || []).some(k=>!Object.hasOwn(args,k)) ||
    ['language','doc_name_lang','doc_content_lang'].some(k=>args[k]!==undefined && !['zh','en'].includes(args[k])) ||
    args.preview_hash!==undefined && (typeof args.preview_hash!=='string' || !/^[a-f0-9]{64}$/.test(args.preview_hash)))throw new HubError('INVALID_REQUEST');
  return def;
}
module.exports={tools,validate};
