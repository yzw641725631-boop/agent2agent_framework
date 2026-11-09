'use strict';
const {HubError}=require('./remote.cjs'),{valid}=require('./knowledge-tools.cjs');
const id={type:'string',format:'uuid'};
const defs=[
  {name:'hub_preview_execution',title:'预览本机任务执行',description:'为已准备的本人代码任务生成固定 CLI 执行预览。必须由本人在桌面端确认程序、任务、目录和时限后才能启动；MCP 无权批准。',
    inputSchema:{type:'object',properties:{task_id:id,run_id:id,lease_epoch:{type:'integer',minimum:1},harness:{type:'string',enum:['codex','claude-code']},timeout_seconds:{type:'integer',minimum:30,maximum:1800}},required:['task_id','run_id','lease_epoch','harness'],additionalProperties:false}},
  {name:'hub_start_execution',title:'启动本人已批准的本机执行',description:'只消费桌面本人刚批准的单次 approval_id；绑定账号、设备、任务、规则版本、程序摘要和时限。退出并不等于业务完成。',
    inputSchema:{type:'object',properties:{approval_id:id},required:['approval_id'],additionalProperties:false}},
  {name:'hub_execution_status',title:'查看本项目本机执行记录',description:'查看真实进程退出、超时或授权失效的停止记录及摘要，不返回模型原始输出或凭证。',
    inputSchema:{type:'object',properties:{},additionalProperties:false}},
  {name:'hub_stop_execution',title:'停止本项目本机执行',description:'停止本人桌面所启动的任务进程及可观察进程组，保留运行摘要和原工作；不能停止独立逃逸到其他服务的程序，不提供系统级隔离保证。',
    inputSchema:{type:'object',properties:{execution_id:id},required:['execution_id'],additionalProperties:false}},
];
function tools(caps){return defs.filter(d=>d.name==='hub_execution_status' || caps.includes('tasks:write')).map(d=>({...d,annotations:{readOnlyHint:d.name==='hub_execution_status',destructiveHint:false,idempotentHint:d.name!=='hub_preview_execution'}}));}
function validate(name,args){const def=defs.find(d=>d.name===name);if(!def)return false;if(!valid(args,def.inputSchema))throw new HubError('INVALID_REQUEST');return true;}
module.exports={tools,validate};
