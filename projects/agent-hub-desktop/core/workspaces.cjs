'use strict';
const fs=require('node:fs/promises');
const path=require('node:path');
const crypto=require('node:crypto');
const {isDeepStrictEqual}=require('node:util');
const {HubError}=require('./remote.cjs');

async function directory(value) {
  if(typeof value!=='string' || !path.isAbsolute(value))throw new HubError('INVALID_WORKSPACE','请选择已存在的项目文件夹。');
  const actual=await fs.realpath(value);
  if(!(await fs.stat(actual)).isDirectory())throw new HubError('INVALID_WORKSPACE');
  if(actual===path.parse(actual).root)throw new HubError('INVALID_WORKSPACE','请选择具体项目文件夹。');
  return actual;
}
function digest(data){return crypto.createHash('sha256').update(data).digest('hex');}
async function preview(workspace,name,configuration,ownership={}) {
  if(await directory(workspace)!==workspace)throw new HubError('WORKSPACE_CHANGED','工作目录已变化，请重新绑定项目。');
  const target=path.join(workspace,'.mcp.json');
  let previous='';
  try {
    const stat=await fs.lstat(target);
    if(stat.isSymbolicLink() || !stat.isFile() || stat.size>512*1024)throw new HubError('CONFIGURATION_CONFLICT','现有 MCP 配置不能安全修改。');
    previous=await fs.readFile(target,'utf8');
  } catch(error){if(error.code!=='ENOENT')throw error;}
  let data;
  try { data=previous?JSON.parse(previous):{}; }catch{throw new HubError('CONFIGURATION_CONFLICT','现有 .mcp.json 不是有效 JSON，已保留原文件。');}
  if(!data || Array.isArray(data) || typeof data!=='object' ||
     (data.mcpServers && (typeof data.mcpServers!=='object' || Array.isArray(data.mcpServers))))throw new HubError('CONFIGURATION_CONFLICT');
  data.mcpServers??={};
  if(Object.hasOwn(data.mcpServers,name) &&
    !isDeepStrictEqual(data.mcpServers[name],configuration) &&
    !isDeepStrictEqual(data.mcpServers[name],ownership.entry) &&
    ownership.fileHash!==digest(previous))throw new HubError('CONFIGURATION_CONFLICT','同名连接已被修改，无法自动覆盖。请核对后手动移除旧项，再重新预览。');
  data.mcpServers[name]=configuration;
  return {target,name,previousHash:digest(previous),previous,next:JSON.stringify(data,null,2)+'\n'};
}
async function apply(workspace,name,configuration,expectedHash,ownership={}) {
  const result=await preview(workspace,name,configuration,ownership);
  if(result.previousHash!==expectedHash)throw new HubError('CONFIGURATION_CHANGED','配置在预览后发生变化，请重新预览。');
  let backup=null;
  if(result.previous){
    backup=result.target+`.agent-hub-${crypto.randomUUID()}.bak`;
    await fs.writeFile(backup,result.previous,{flag:'wx',mode:0o600});
  }
  const temporary=result.target+`.${crypto.randomUUID()}.tmp`;
  try{
  await fs.writeFile(temporary,result.next,{flag:'wx',mode:0o600});
  // Re-check before replacement; do not overwrite an edited configuration.
  const current=await preview(workspace,name,configuration,ownership);
  if(current.previousHash!==expectedHash)throw new HubError('CONFIGURATION_CHANGED');
  await fs.rename(temporary,result.target);
  return {target:result.target,backup,installedHash:digest(result.next),entry:configuration};
  }finally{await fs.unlink(temporary).catch(error=>{if(error.code!=='ENOENT')throw error;});}
}
module.exports={directory,preview,apply};
