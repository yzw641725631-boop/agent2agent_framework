'use strict';
const {HubError}=require('./remote.cjs'),{valid}=require('./knowledge-tools.cjs');
const id={type:'string',format:'uuid'},integer={type:'integer',minimum:1};
const str=(minLength,maxLength)=>({type:'string',minLength,maxLength});
const fields={title:str(1,200),product_version:str(1,100),user_roles:{type:'array',minItems:1,maxItems:20,items:str(1,2000)},
  purpose:str(10,2000),prerequisites:str(1,4000),entry:str(1,2000),steps:{type:'array',minItems:1,maxItems:80,items:str(1,2000)},
  inputs:str(1,4000),results:str(1,4000),recovery:str(1,4000),cautions:str(1,4000),examples:str(1,8000),changes:str(1,4000),limitations:str(1,4000),verification:str(10,4000)};
const lease={expected_version:integer,instance_id:id,lease_epoch:integer};
const defs=[
  ['hub_list_guides','查阅功能操作说明目录','按权限列出操作指南及生成待办。已发布说明可单独授权阅读；草稿继承代码范围。instance_id 只查询本人本设备的生成待办。',
    'GET','',null,{task_id:id,instance_id:id,state:{type:'string',enum:['queued','running','review','approved','published']},include_archived:{type:'boolean'},query:str(0,200),offset:{type:'integer',minimum:0,maximum:100000},limit:{type:'integer',minimum:1,maximum:100}},[]],
  ['hub_read_guide','读取功能操作说明','展开当前版本正文、适用版本和步骤。有来源权限时附可追溯记录；指南本身不能改变项目权限或触发命令。',
    'GET',':job',null,{job_id:id},['job_id']],
  ['hub_get_guide_input','取得本机说明生成输入','核验本人设备和原登记实例，读取冻结的需求、工作日志、代码引用及输出结构。以确切快照核对实现，不编造截图或测试结论。',
    'GET',':job/input','tasks:write',{job_id:id},['job_id']],
  ['hub_claim_guide','认领独立说明待办','为原开发者的本地实例取得十分钟独立文档租约。离线时任务保留，过期可重新领取；不复用已结束的开发租约。',
    'POST',':job/claim','tasks:write',{job_id:id,expected_version:integer,instance_id:id},['job_id','expected_version','instance_id']],
  ['hub_complete_guide','提交操作说明草稿','依据冻结代码形成面向使用者的完整说明；逐项填写入口、步骤、输入校验、结果、恢复及核验依据。独立产品审核和测试通过后才可随功能发布。',
    'POST',':job/complete','tasks:write',{job_id:id,...lease,source_hash:{type:'string',pattern:'^[a-f0-9]{64}$'},content:{type:'object',properties:fields,required:Object.keys(fields),additionalProperties:false}},
    ['job_id','expected_version','instance_id','lease_epoch','source_hash','content']],
  ['hub_retry_guide','报告说明生成阻塞并释放租约','记录失败或缺少环境的原因，保留原草稿和重试关系。恢复后重新认领；不会伪报生成完成。',
    'POST',':job/retry','tasks:write',{job_id:id,...lease,reason:str(10,2000)},['job_id','expected_version','instance_id','lease_epoch','reason']],
  ['hub_decide_guide','核对操作说明','产品负责人核对业务及发布受众，测试人员按步骤验证；二者均通过且版本一致才满足发布。project 受众会允许仅有指南阅读权限的成员查阅，批准前检查是否含内部资料。',
    'POST',':job/decisions','decision',{job_id:id,expected_version:integer,draft_version:integer,gate:{type:'string',enum:['product','test']},approved:{type:'boolean'},reason:str(10,2000),audience:{type:'string',enum:['repository','project']},override_reason:str(0,2000)},
    ['job_id','expected_version','draft_version','gate','approved','reason']],
  ['hub_reassign_guide','移交未完成的说明待办','负责人把离线或离职人员的说明待办明确转交给同仓库当前获准的登记实例，保留原工作来源，立即使旧生成租约失效。',
    'POST',':job/reassign','tasks:publish',{job_id:id,expected_version:integer,endpoint_id:id,reason:str(10,2000)},['job_id','expected_version','endpoint_id','reason']],
  ['hub_list_guide_amendments','查看说明修订','来源仓库成员可查看修订草稿与审核；仅有指南阅读权的成员只能看到已发布过的正文。',
    'GET',':job/amendments',null,{job_id:id,offset:{type:'integer',minimum:0,maximum:100000},limit:{type:'integer',minimum:1,maximum:100}},['job_id']],
  ['hub_amend_guide','提交已发布说明修订','保留旧发布正文和冻结代码证据，新增修订草稿。产品和测试核对通过后才能替换目录正文，不能直接改写已发布历史。',
    'POST',':job/amendments','tasks:write',{job_id:id,base_version:{type:'integer',minimum:0},content:{type:'object',properties:fields,required:Object.keys(fields),additionalProperties:false},reason:str(10,2000)},
    ['job_id','base_version','content','reason']],
  ['hub_decide_guide_amendment','核对说明修订','针对同一修订执行产品或测试核对；作者不能自行批准，超管例外必须明确说明原因。退回后创建新修订，历史决定不可覆盖。',
    'POST',':job/amendments/:amendment/decisions','decision',{job_id:id,amendment_id:id,gate:{type:'string',enum:['product','test']},approved:{type:'boolean'},reason:str(10,2000),override_reason:str(0,2000)},
    ['job_id','amendment_id','gate','approved','reason']],
  ['hub_publish_guide_revision','发布修订或归档恢复说明','按当前目录版本发布双重审核通过的修订，或明确归档、恢复。旧正文和历史保留，归档不等于删除来源。',
    'POST',':job/publications','tasks:publish',{job_id:id,expected_version:{type:'integer',minimum:0},action:{type:'string',enum:['published','archived','restored']},amendment_id:id,reason:str(10,2000)},
    ['job_id','expected_version','action','reason']],
  ['hub_list_guide_publications','查看说明发布历史','按权限分页查看修订发布、归档和恢复版本。初始随功能发布的原文为目录版本 0。',
    'GET',':job/publications',null,{job_id:id,offset:{type:'integer',minimum:0,maximum:100000},limit:{type:'integer',minimum:1,maximum:100}},['job_id']],
  ['hub_read_guide_publication','读取历史发布正文','读取明确目录版本的已审核正文；版本 0 是原功能发布说明。不会公开未发布修订或代码来源。',
    'GET',':job/publications/:publication',null,{job_id:id,publication_version:{type:'integer',minimum:0}},['job_id','publication_version']],
  ['hub_send_guide_feedback','反馈操作说明问题','读者可以报告当前发布说明中的问题、建议和过时内容。反馈不能作为代码执行或自动批准指令。',
    'POST',':job/feedback','feedback',{job_id:id,publication_version:{type:'integer',minimum:0},kind:{type:'string',enum:['issue','suggestion','outdated']},body:str(10,4000)},
    ['job_id','publication_version','kind','body']],
  ['hub_list_guide_feedback','查看说明反馈','读者查看自己的反馈，拥有来源仓库及发布管理权限的成员可查看待处理反馈。',
    'GET',':job/feedback',null,{job_id:id,offset:{type:'integer',minimum:0,maximum:100000},limit:{type:'integer',minimum:1,maximum:100}},['job_id']],
  ['hub_resolve_guide_feedback','记录说明反馈处理结果','记录解决、拒绝或重新打开反馈的原因，原反馈和每次处理记录均保留。',
    'POST',':job/feedback/:feedback/resolutions','tasks:publish',{job_id:id,feedback_id:id,state:{type:'string',enum:['open','resolved','rejected']},reason:str(10,2000)},
    ['job_id','feedback_id','state','reason']],
];
function allowed(d,caps,args){
  if(d[5]==='feedback')return caps.some(c=>['guides:read','tasks:write','tasks:publish','tasks:test'].includes(c));
  return !d[5] || (d[5]==='decision' ? args?caps.includes(args.gate==='test'?'tasks:test':'tasks:publish'):caps.some(c=>['tasks:test','tasks:publish'].includes(c)):caps.includes(d[5]));
}
function definition(d){const write=d[3]!=='GET';return {name:d[0],title:d[1],description:d[2],inputSchema:{type:'object',properties:{...d[6],...(write?{operation_id:id}:{})},required:[...d[7],...(write?['operation_id']:[])],additionalProperties:false},annotations:{readOnlyHint:!write,destructiveHint:false,idempotentHint:true}};}
function tools(caps){return defs.filter(d=>allowed(d,caps)).map(definition);}
function request(name,args,binding,caps){
  const d=defs.find(d=>d[0]===name);if(!d)return null;
  if(!valid(args,definition(d).inputSchema))throw new HubError('INVALID_REQUEST');
  if(!allowed(d,caps,args))throw new HubError('CAPABILITY_REQUIRED');
  const {job_id,operation_id,...body}=args;
  let route=d[4]?.replace(':job',job_id) || '';
  for(const [token,key] of [[':amendment','amendment_id'],[':feedback','feedback_id'],[':publication','publication_version']]){
    if(route.includes(token)){route=route.replace(token,String(body[key]));delete body[key];}
  }
  const query=d[3]==='GET'?{...body}:{};
  if(binding.repositoryId)query.repository_id=binding.repositoryId;
  const suffix=new URLSearchParams(query).toString();
  return {method:d[3],path:`/api/projects/${binding.projectId}/guides`+(route?'/'+route:'')+(suffix?'?'+suffix:''),
    body:d[3]==='GET'?undefined:body,operation_id};
}
module.exports={tools,request};
