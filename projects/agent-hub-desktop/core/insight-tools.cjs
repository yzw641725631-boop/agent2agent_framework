'use strict';
const {HubError}=require('./remote.cjs'),{valid}=require('./knowledge-tools.cjs');
const id={type:'string',format:'uuid'},integer=(minimum,maximum)=>({type:'integer',minimum,maximum});
const text=(minLength,maxLength)=>({type:'string',minLength,maxLength});
const defs=[
  ['hub_get_usage','查看用量与预算','读取当前获准范围内的本月上报 Token、费用及程序测得的上下文大小。缺少宿主报告时不代表零消耗；费用尚未核对账单。',
    'usage','GET',null,{repository_id:id},[]],
  ['hub_configure_usage_budget','设置项目用量额度','明确设置按已收到的宿主报告观察或暂停受管开发。不能限制未上报调用或独立外部程序；达到额度后仍允许保存日志与交接。',
    'usage/budget','PUT','tasks:publish',{expected_version:integer(0,1000000000),mode:{type:'string',enum:['observe','pause-managed-work']},
      monthly_reported_token_limit:integer(0,1000000000000),change_reason:text(10,1000)},['expected_version','change_reason']],
  ['hub_report_usage','上报本运行模型用量','仅在取得宿主或模型响应的真实计数时填写，不猜测；reference_hash 为来源请求标识的 SHA-256。相同来源不会重复计入，报告仍标为未核对账单。',
    'usage','POST','tasks:write',{repository_id:id,task_id:id,run_id:id,reference_hash:{type:'string',pattern:'^[a-f0-9]{64}$'},provider:text(1,100),model:text(1,200),
      input_tokens:integer(0,10000000),output_tokens:integer(0,10000000),cached_input_tokens:integer(0,10000000),reported_cost_micro_usd:integer(0,1000000000)},
    ['task_id','run_id','reference_hash','provider','model','input_tokens','output_tokens']],
  ['hub_evaluate_memory','验证记忆检索命中','给出获准且有效的预期原文摘录 ID，实际执行查询并保存 Recall@k、MRR、返回大小及真实检索方式。结果只是这批标注题的历史观察；不等于完整理解。',
    'memory/evaluations','POST','tasks:publish',{repository_id:id,k:integer(1,20),search_mode:{type:'string',enum:['hybrid','keyword']},cases:{type:'array',minItems:1,maxItems:10,
      items:{type:'object',properties:{query:text(1,200),expected_card_ids:{type:'array',minItems:1,maxItems:20,items:id}},required:['query','expected_card_ids'],additionalProperties:false}}},['cases']],
  ['hub_list_memory_evaluations','查看检索验证记录','按当前来源权限查看历史检索指标；来源已撤权的记录不会重新导出。',
    'memory/evaluations','GET','tasks:publish',{repository_id:id,limit:integer(1,50)},[]],
];
function definition(d){return {name:d[0],title:d[1],description:d[2],inputSchema:{type:'object',properties:{...d[6],...(d[4]!=='GET'?{operation_id:id}:{})},
  required:[...d[7],...(d[4]!=='GET'?['operation_id']:[])],additionalProperties:false},annotations:{readOnlyHint:d[4]==='GET',destructiveHint:false,idempotentHint:true}};}
function tools(caps){return defs.filter(d=>!d[5] || caps.includes(d[5])).map(definition);}
function request(name,args,binding,caps){
  const d=defs.find(d=>d[0]===name);if(!d)return null;
  if(!valid(args,definition(d).inputSchema))throw new HubError('INVALID_REQUEST');
  if(d[5] && !caps.includes(d[5]))throw new HubError('CAPABILITY_REQUIRED');
  const {operation_id,...body}=args;
  if(binding.repositoryId && Object.hasOwn(d[6],'repository_id')){
    if(body.repository_id && body.repository_id!==binding.repositoryId)throw new HubError('TASK_REPOSITORY_MISMATCH');
    body.repository_id=binding.repositoryId;
  }
  return {method:d[4],path:`/api/projects/${binding.projectId}/${d[3]}`+(d[4]==='GET' && Object.keys(body).length?'?'+new URLSearchParams(body):''),
    body:d[4]!=='GET'?body:undefined,operation_id};
}
module.exports={tools,request};
