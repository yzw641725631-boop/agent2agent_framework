'use strict';
const {HubError}=require('./remote.cjs');
const RUN_LIMIT=24000,MAX_RECEIPTS=32,MAX_SESSIONS=8;
function clear(run){
  for(const key of ['memory','role','experience_cache','experience_sessions','loaded_memory_sessions'])delete run[key];
}
function versions(run){
  return Object.values(run.experience_cache || {}).map(({id,kind,revision})=>({id,kind,revision}));
}
function session(run,id){
  const sessions=run.experience_sessions??={};
  if(!Object.hasOwn(sessions,id)){
    if(Object.keys(sessions).length>=MAX_SESSIONS)throw new HubError('MEMORY_SESSION_LIMIT','本运行的记忆会话已达上限，请交接并重新认领。');
    sessions[id]={known:[]};
  }
  return sessions[id];
}
function accept(run,data,id,{validation=false}={}){
  if(!data || data.schema_version!==1 || data.delivery?.session_id!==id ||
      !Array.isArray(data.items) || data.items.length>12 || !Array.isArray(data.invalidated) ||
      !Number.isSafeInteger(data.budget?.used) || data.budget.used<0 || data.budget.used>data.budget.limit ||
      data.budget.serialized_bytes!==Buffer.byteLength(JSON.stringify(data)) || data.budget.serialized_bytes>24000 ||
      !/^[a-f0-9]{64}$/.test(data.delivery.receipt || ''))throw new HubError('MEMORY_RESPONSE_INVALID');
  if(data.invalidated.length){
    clear(run);
    throw new HubError('WORK_EXPERIENCE_STALE','已读经验被撤回、更新或来源权限变化，请重新准备任务并核对当前经验。');
  }
  if(validation)return data;
  const entries=run.experience_cache??={},pending=[];
  for(const item of data.items){
    if(!item || !['knowledge','card'].includes(item.kind) || !/^[a-f0-9]{64}$/.test(item.revision || '') ||
        typeof item.id!=='string' || item.experience_id!==`${item.kind==='knowledge'?'EXP':'SRC'}-${item.id}`)throw new HubError('MEMORY_RESPONSE_INVALID');
    pending.push({id:item.id,kind:item.kind,revision:item.revision});
  }
  if(new Set([...Object.keys(entries),...pending.map(p=>p.kind+':'+p.id)]).size>MAX_RECEIPTS)
    throw new HubError('MEMORY_WORKING_SET_LIMIT','本运行已读经验达到上限，请保存交接并重新准备任务。');
  for(const item of data.items)entries[item.kind+':'+item.id]=item;
  const s=session(run,id);s.pending={receipt:data.delivery.receipt,versions:pending};
  if(data.delivery.state==='delivered')run.experience_units=Math.min(RUN_LIMIT,(run.experience_units || 0)+data.budget.used);
  return data;
}
module.exports={clear,versions,session,accept,RUN_LIMIT,MAX_RECEIPTS};
