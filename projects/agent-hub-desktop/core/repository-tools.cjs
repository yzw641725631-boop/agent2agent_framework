'use strict';
const {HubError}=require('./remote.cjs');
const uuid=value=>typeof value==='string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value);
const id={type:'string',format:'uuid'};
const define=(name,title,description,properties,required=[],readOnly=false)=>({name,title,description,
  inputSchema:{type:'object',properties,required,additionalProperties:false},
  annotations:{readOnlyHint:readOnly,destructiveHint:false,idempotentHint:name!=='hub_fetch_repository'}});
const tools=[
  define('hub_list_repositories','列出获准仓库','列出当前项目组获准的仓库、目标端和本人权限。项目权限不自动授予全部仓库。',{},[],true),
  define('hub_list_document_sources','列出获准文档源','列出可读取的仓库文档源。搜索、展开时使用相应 repository_id，不能只依赖文件名。',{},[],true),
  define('hub_register_repository','接入项目组仓库','按用户要求登记独立 GitHub 仓库。需要仓库管理权限。私有仓库使用后台已配置的 GitHub App；专用凭证应直接在后台填写，不传给 Agent。接入后仍须由超管批准成员代码及文档权限。',
    {slug:{type:'string',pattern:'^[a-z][a-z0-9-]{1,79}$'},name:{type:'string',minLength:1,maxLength:200},
      platform:{type:'string',enum:['web','pc','api','mobile','shared','other']},repository_url:{type:'string',maxLength:400},
      branch:{type:'string',maxLength:200},auth_mode:{type:'string',enum:['public','github_app']},installation_id:{type:'integer',minimum:1},operation_id:id},
    ['slug','name','platform','repository_url','auth_mode','operation_id']),
  define('hub_assemble_project','组装获准项目组','按用户要求，在本连接绑定目录的新子文件夹中拉取指定获准仓库，生成各仓库固定连接。保留现有目录；不会覆盖已有工作。上游 GitHub 凭证不交给 Agent。',
    {repository_ids:{type:'array',minItems:1,maxItems:100,items:id},harness:{type:'string',enum:['codex','claude','cursor']},operation_id:id},['operation_id']),
  define('hub_fetch_repository','获取仓库更新','只在绑定的仓库获取最新分支到 FETCH_HEAD，保留当前分支与工作文件。每次访问重新验证仓库权限。',{},[]),
  define('hub_push_task','推送已记录的任务提交','将本机干净 Git 工作区的当前提交推送到当前有效任务独有分支。须先保存含该提交号的最新 checkpoint 日志。禁止直接更新主分支、其他任务分支或删除远程分支。此操作不完成评审、合并或发布。',
    {task_id:id,run_id:id,lease_epoch:{type:'integer',minimum:1}},['task_id','run_id','lease_epoch']),
];
function validate(name,args){
  const tool=tools.find(tool=>tool.name===name);if(!tool)return null;
  if(!args || typeof args!=='object' || Array.isArray(args) || Object.keys(args).some(key=>!Object.hasOwn(tool.inputSchema.properties,key)) ||
    tool.inputSchema.required.some(key=>args[key]===undefined) ||
    ['operation_id','task_id','run_id'].some(key=>args[key]!==undefined && !uuid(args[key])) ||
    args.repository_ids!==undefined && (!Array.isArray(args.repository_ids) || !args.repository_ids.length || args.repository_ids.length>100 ||
      args.repository_ids.some(value=>!uuid(value)) || new Set(args.repository_ids).size!==args.repository_ids.length) ||
    args.harness!==undefined && !['codex','claude','cursor'].includes(args.harness) ||
    args.lease_epoch!==undefined && (!Number.isSafeInteger(args.lease_epoch) || args.lease_epoch<1))throw new HubError('INVALID_REQUEST');
  return tool;
}
function available(meta){
  return tools.filter(tool=>tool.name==='hub_register_repository'?meta.capabilities.includes('repositories:manage'):
    tool.name==='hub_list_document_sources'?meta.capabilities.includes('documents:read'):
    tool.name==='hub_assemble_project'?meta.repository_code_read && !meta.repository_id:
    tool.name==='hub_fetch_repository'?meta.repository_id && meta.repository_code_read:
    tool.name==='hub_push_task'?meta.repository_id && meta.repository_code_write && meta.capabilities.includes('tasks:write'):true);
}
module.exports={tools,available,validate,uuid};
