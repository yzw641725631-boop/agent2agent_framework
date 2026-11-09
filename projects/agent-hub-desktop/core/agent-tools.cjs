'use strict';
const {HubError}=require('./remote.cjs');
const uuid=value=>typeof value==='string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
const id={type:'string',format:'uuid'},version={type:'integer',minimum:1};
const page={offset:{type:'integer',minimum:0,maximum:100000},limit:{type:'integer',minimum:1,maximum:100}};
const definitions=[
  ['hub_register_agent','登记本执行实例','登记当前人员设备上的真实执行实例及职业；不会启动程序或自动接受任务。身份、仓库和宿主固定，修改名称或职业须带当前版本。',
    {instance_id:id,repository_id:id,name:{type:'string',minLength:1,maxLength:100},
      harness:{type:'string',enum:['human','claude-code','codex','cursor','qoder','workbuddy','codebuddy','trae']},
      roles:{type:'array',minItems:1,maxItems:4,uniqueItems:true,items:{type:'string',enum:['project-manager','architect','designer','developer']}},
      role_profiles:{type:'array',maxItems:32,uniqueItems:true,items:{type:'string',pattern:'^[a-z][a-z0-9-]{0,63}$'}},
      expected_version:{type:'integer',minimum:0},enabled:{type:'boolean'}},['instance_id','name','harness','roles','expected_version'],'agents','recipient'],
  ['hub_list_agents','列出可投递执行者','负责人查看当前获准的仓库执行者，其他人只看本设备登记。最近活动不代表模型正在运行。',
    {repository_id:id,...page},[],'agents',null],
  ['hub_agent_inbox','读取本执行实例待办','执行者本人读取离线保留的待办并更新活动时间；不自动接收、不认领任务。桌面后台查看不更新 Agent 活动。',
    {endpoint_id:id,...page},['endpoint_id'],'@local','recipient'],
  ['hub_list_agent_dispatches','查看任务投递记录','查看有权读取的投递、接收、认领和拒绝回执。queued/received 均未取得代码任务租约。',
    {endpoint_id:id,task_id:id,repository_id:id,...page},[],'agent-dispatches',null],
  ['hub_dispatch_task','投递正式任务','负责人向指定登记实例投递 ready 任务。接受时原子认领，继续检查依赖、版本、路径占用与租约；不自动执行或唤醒宿主。',
    {task_id:id,endpoint_id:id,expected_task_version:version,expected_endpoint_version:version,
      note:{type:'string',minLength:1,maxLength:1000},expires_in_seconds:{type:'integer',minimum:60,maximum:604800}},
    ['task_id','endpoint_id','expected_task_version','expected_endpoint_version','note'],'agent-dispatches','tasks:publish'],
  ['hub_decide_agent_dispatch','接收、接受或拒绝投递','本人设备接收/接受/拒绝，负责人可取消。只有 accept 成功返回正式 run；随后准备规则和记忆。接收不占租约，拒绝或取消须写原因。',
    {task_id:id,dispatch_id:id,expected_version:version,action:{type:'string',enum:['receive','accept','decline','cancel']},reason:{type:'string',maxLength:1000}},
    ['task_id','dispatch_id','expected_version','action'],'agent-dispatches/:dispatch/decision','decision'],
];
const writes=new Set(['hub_register_agent','hub_dispatch_task','hub_decide_agent_dispatch']);
function allowed(def,caps,args){
  if(def[6]==='recipient')return caps.includes('tasks:claim') && caps.includes('tasks:write');
  if(def[6]==='decision')return args?.action==='cancel'?caps.includes('tasks:publish'):
    caps.includes('tasks:claim') && caps.includes('tasks:write') || !args && caps.includes('tasks:publish');
  return !def[6] || caps.includes(def[6]);
}
function tools(caps){return definitions.filter(d=>allowed(d,caps)).map(d=>({name:d[0],title:d[1],description:d[2],
  inputSchema:{type:'object',properties:{...d[3],...(writes.has(d[0])?{operation_id:id}:{})},
    required:[...d[4],...(writes.has(d[0])?['operation_id']:[])],additionalProperties:false},
  annotations:{readOnlyHint:!writes.has(d[0]) && d[0]!=='hub_agent_inbox',destructiveHint:false,idempotentHint:true}}));}
function validate(name,args){
  const d=definitions.find(d=>d[0]===name);if(!d)return null;
  if(!args || typeof args!=='object' || Array.isArray(args) ||
    Object.keys(args).some(k=>!Object.hasOwn(d[3],k) && !(writes.has(name) && k==='operation_id')) ||
    [...d[4],...(writes.has(name)?['operation_id']:[])].some(k=>args[k]===undefined) ||
    ['instance_id','repository_id','task_id','endpoint_id','dispatch_id','operation_id'].some(k=>args[k]!==undefined && !uuid(args[k])) ||
    Object.entries(d[3]).some(([k,s])=>args[k]!==undefined && (s.type==='integer' && (!Number.isSafeInteger(args[k]) || args[k]<s.minimum || args[k]>(s.maximum || Number.MAX_SAFE_INTEGER)) ||
      s.type==='string' && (typeof args[k]!=='string' || s.enum && !s.enum.includes(args[k]) || args[k].length<(s.minLength || 0) || args[k].length>(s.maxLength || 1000)) ||
      s.type==='boolean' && typeof args[k]!=='boolean')) ||
    args.roles!==undefined && (!Array.isArray(args.roles) || !args.roles.length || args.roles.length>4 || new Set(args.roles).size!==args.roles.length || args.roles.some(r=>!definitions[0][3].roles.items.enum.includes(r))) ||
    args.role_profiles!==undefined && (!Array.isArray(args.role_profiles) || args.role_profiles.length>32 || new Set(args.role_profiles).size!==args.role_profiles.length || args.role_profiles.some(r=>typeof r!=='string' || !/^[a-z][a-z0-9-]{0,63}$/.test(r))) ||
    ['decline','cancel'].includes(args.action) && !args.reason?.trim())throw new HubError('INVALID_REQUEST');
  return d;
}
function request(name,args,caps,binding){
  const d=validate(name,args);if(!d)return null;
  if(!allowed(d,caps,args))throw new HubError('CAPABILITY_REQUIRED');
  if(d[5]==='@local')throw new HubError('AGENT_RUNTIME_REQUIRED');
  const {operation_id,dispatch_id,...body}=args;
  if(binding.repositoryId){
    if(body.repository_id && body.repository_id!==binding.repositoryId)throw new HubError('AGENT_REPOSITORY_MISMATCH');
    if(['hub_register_agent','hub_list_agents','hub_list_agent_dispatches'].includes(name))body.repository_id=binding.repositoryId;
  }
  if(binding.taskId){
    if(body.task_id && body.task_id!==binding.taskId)throw new HubError('TASK_WORKTREE_MISMATCH');
    if(name==='hub_list_agent_dispatches')body.task_id=binding.taskId;
  }
  if(name==='hub_decide_agent_dispatch')delete body.task_id;
  const route=d[5].replace(':dispatch',dispatch_id || '');
  const query=!writes.has(name) && Object.keys(body).length?'?'+new URLSearchParams(body):'';
  return {method:writes.has(name)?'POST':'GET',path:`/api/projects/${binding.projectId}/${route}${query}`,
    body:writes.has(name)?body:undefined,operation_id};
}
module.exports={tools,validate,request};
