'use strict';
const {valid}=require('./knowledge-tools.cjs');
const {HubError}=require('./remote.cjs');
const id={type:'string',format:'uuid'},version={type:'integer',minimum:1};
const kind={type:'string',enum:['pull_request','ci','merge','deployment','rollback']};
const defs=[
  ['hub_get_delivery_policy','查阅仓库交付要求','读取绑定仓库发布所需的外部凭据、每项测试、目标环境和有效期。Git 推送本身不证明合并或部署。',
    'GET','policy',null,{repository_id:id},['repository_id']],
  ['hub_set_delivery_policy','设置仓库交付要求','发布负责人明确配置必须核验的环节。要求 CI 时所有已列工作流都须成功；要求部署时所有已列环境都须成功。变更使旧版凭据失效。',
    'PUT','policy','tasks:publish',{repository_id:id,expected_version:{type:'integer',minimum:0},
      required_kinds:{type:'array',maxItems:5,uniqueItems:true,items:kind},
      workflows:{type:'array',maxItems:20,uniqueItems:true,items:{type:'string',pattern:'^\\.github/workflows/[A-Za-z0-9_.-]+\\.ya?ml$'}},
      environments:{type:'array',maxItems:20,uniqueItems:true,items:{type:'string',minLength:1,maxLength:100}},
      max_age_seconds:{type:'integer',minimum:60,maximum:86400},
      ci_target:{type:'string',enum:['delivered','merged']},deployment_target:{type:'string',enum:['delivered','merged']},
      change_reason:{type:'string',minLength:10,maxLength:1000}},['repository_id','expected_version','required_kinds','change_reason']],
  ['hub_list_delivery_evidence','查看任务交付核验','分页列出本人可读日志对应的外部观察，附当前提交和规则下逐项发布条件。结果标明核验时间，不表示持续监控目标系统健康。',
    'GET','task',null,{task_id:id,offset:{type:'integer',minimum:0,maximum:100000},limit:{type:'integer',minimum:1,maximum:100}},['task_id']],
  ['hub_verify_delivery_evidence','从 GitHub 核验交付','只读核验指定 PR、测试运行或部署编号，并绑定已推送的实际检查点。合并后核验使用 merge_evidence_id；回滚同时引用被替换和恢复目标的部署凭据。不创建 PR、不执行合并、部署或远程命令。',
    'POST','task','verify',{task_id:id,worklog_id:id,expected_task_version:version,kind,
      provider_id:{type:'integer',minimum:1,maximum:9007199254740991},merge_evidence_id:id,rollback_of_id:id,rollback_to_id:id},
    ['task_id','worklog_id','expected_task_version','kind','provider_id']],
];
function allowed(d,caps){return !d[5] || (d[5]==='verify'?caps.some(c=>['tasks:write','tasks:publish'].includes(c)):caps.includes(d[5]));}
function definition(d){const write=d[3]!=='GET';return {name:d[0],title:d[1],description:d[2],inputSchema:{type:'object',
  properties:{...d[6],...(write?{operation_id:id}:{})},required:[...d[7],...(write?['operation_id']:[])],additionalProperties:false},
  annotations:{readOnlyHint:!write,destructiveHint:false,idempotentHint:true}};}
function tools(caps){return defs.filter(d=>allowed(d,caps)).map(definition);}
function request(name,args,binding,caps){
  const d=defs.find(d=>d[0]===name);if(!d)return null;
  if(!valid(args,definition(d).inputSchema))throw new HubError('INVALID_REQUEST');
  if(!allowed(d,caps))throw new HubError('CAPABILITY_REQUIRED');
  const {task_id,repository_id,operation_id,...body}=args;
  if(binding.repositoryId && repository_id && binding.repositoryId!==repository_id)throw new HubError('TASK_REPOSITORY_MISMATCH');
  const query=d[4]==='task' && binding.repositoryId?{repository_id:binding.repositoryId}:{};
  if(d[3]==='GET')Object.assign(query,body);
  const suffix=new URLSearchParams(query).toString();
  const resource=d[4]==='policy'?`repositories/${repository_id}/delivery-policy`:`tasks/${task_id}/delivery-evidence`;
  return {method:d[3],path:`/api/projects/${binding.projectId}/${resource}`+(suffix?'?'+suffix:''),
    body:d[3]==='GET'?undefined:body,operation_id};
}
module.exports={tools,request};
