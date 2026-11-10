'use strict';
const {HubError}=require('./remote.cjs');
const id={type:'string',format:'uuid'};
const string=(minLength,maxLength)=>({type:'string',minLength,maxLength});
const query={repository_id:id,query:string(0,200),search_mode:{type:'string',enum:['hybrid','keyword']},include_history:{type:'boolean'},
  offset:{type:'integer',minimum:0,maximum:100000},limit:{type:'integer',minimum:1,maximum:50}};
const generation={type:'object',properties:{method:{type:'string',enum:['local-agent','human','server-model']},
  harness:{type:'string',enum:['human','claude-code','codex','cursor','qoder','workbuddy','codebuddy','trae']},
  model_reported:string(0,120),prompt_version:string(1,80),
  input_tokens_reported:{type:'integer',minimum:0,maximum:10000000},
  output_tokens_reported:{type:'integer',minimum:0,maximum:10000000}},additionalProperties:false};
function define(name,title,description,properties,required,cap){
  const write=!!cap;
  return {name,title,description,inputSchema:{type:'object',properties:{...properties,...(write?{operation_id:id}:{})},
    required:[...required,...(write?['operation_id']:[])],additionalProperties:false},cap,
    annotations:{readOnlyHint:!write,...(write?{destructiveHint:false,idempotentHint:true}:{})}};
}
const definitions=[
  define('hub_search_knowledge','检索长期知识','检索本人获准的长期约定、接口和经验。默认只返回审核生效且来源有效的内容；include_history 展示候选及历史。读取 source_card_id 可展开原文，知识不授予权限。',query,[]),
  define('hub_propose_knowledge','提交长期知识候选','从已读获准记忆中提炼一条可长期复用的结论，引用主来源和最多七个 additional_sources，每项包含 source_card_id、source_hash 和逐字 source_quote。所有来源的读取权限同时适用；候选须独立审核，生成模型和 Token 用量仅为作者报告。',
    {repository_id:id,source_card_id:id,source_hash:{type:'string',pattern:'^[a-f0-9]{64}$'},source_quote:string(8,1200),
      topic:{type:'string',pattern:'^[a-z0-9][a-z0-9._/-]{0,119}$'},kind:{type:'string',enum:['constraint','decision','interface','lesson','pitfall']},
      title:string(1,200),body:string(10,2000),reason:string(10,1000),generation,
      additional_sources:{type:'array',maxItems:7,items:{type:'object',properties:{source_card_id:id,
        source_hash:{type:'string',pattern:'^[a-f0-9]{64}$'},source_quote:string(8,1200)},
        required:['source_card_id','source_hash','source_quote'],additionalProperties:false}}},
    ['source_card_id','source_hash','source_quote','topic','kind','title','body','reason'],'tasks:write'),
  define('hub_decide_knowledge','审核长期知识','有 tasks:publish 且能读来源者审核候选；作者不能独立批准，超管自审需写 override_reason。同主题替换须提供 replaces_id 和 replaces_version；过期来源拒绝激活。',
    {repository_id:id,fact_id:id,expected_version:{type:'integer',minimum:1},
      state:{type:'string',enum:['active','conflicted','retired','rejected']},reason:string(10,2000),override_reason:string(0,2000),
      replaces_id:id,replaces_version:{type:'integer',minimum:1}},['fact_id','expected_version','state','reason'],'tasks:publish'),
];
function valid(value,schema){
  if(schema.type==='array')return Array.isArray(value) && value.length>=(schema.minItems??0) && value.length<=(schema.maxItems??100) &&
    (!schema.uniqueItems || new Set(value).size===value.length) && value.every(item=>valid(item,schema.items));
  if(schema.type==='object')return value && typeof value==='object' && !Array.isArray(value) &&
    Object.keys(value).length<=(schema.maxProperties??1000) &&
    Object.keys(value).every(key=>Object.hasOwn(schema.properties || {},key) ? valid(value[key],schema.properties[key]) :
      schema.additionalProperties && typeof schema.additionalProperties==='object' && valid(value[key],schema.additionalProperties)) &&
    (schema.required || []).every(key=>Object.hasOwn(value,key));
  if(schema.type==='boolean')return typeof value==='boolean';
  if(schema.type==='integer')return Number.isSafeInteger(value) && value>=(schema.minimum??0) && value<=(schema.maximum??Number.MAX_SAFE_INTEGER);
  return typeof value==='string' && [...value].length>=(schema.minLength??0) && [...value].length<=(schema.maxLength??100000) &&
    (!schema.enum || schema.enum.includes(value)) && (!schema.pattern || new RegExp(schema.pattern).test(value)) &&
    (schema.format!=='uuid' || /^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(value));
}
function tools(caps){return definitions.filter(d=>!d.cap || caps.includes(d.cap)).map(({cap,...tool})=>tool);}
function request(name,args,projectId,repositoryId,caps){
  const def=definitions.find(d=>d.name===name);if(!def)return null;
  if(!valid(args,def.inputSchema))throw new HubError('INVALID_REQUEST');
  if(def.cap && !caps.includes(def.cap))throw new HubError('CAPABILITY_REQUIRED');
  if(repositoryId && args.repository_id && args.repository_id!==repositoryId)throw new HubError('DOCUMENT_SOURCE_NOT_ACCESSIBLE');
  const {fact_id,operation_id,...body}=args;
  if(repositoryId)body.repository_id=repositoryId;
  const base=`/api/projects/${projectId}/memory/knowledge`;
  return def.cap?{method:'POST',path:base+(fact_id?`/${fact_id}/decisions`:''),body,operation_id}:
    {method:'GET',path:base+'?'+new URLSearchParams(body)};
}
module.exports={tools,request,valid};
