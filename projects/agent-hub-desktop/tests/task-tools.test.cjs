'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const tasks=require('../core/task-tools.cjs');
const {dispatch}=require('../core/mcp.cjs');
const taskId=crypto.randomUUID(),projectId=crypto.randomUUID(),operationId=crypto.randomUUID();
const all=['tasks:publish','tasks:claim','tasks:write','tasks:review','tasks:test','logs:read'];

test('task discovery separates read, claim, log and review permissions',async()=>{
  assert.deepEqual(tasks.tools([]).map(t=>t.name),['hub_list_tasks','hub_get_task','hub_list_worklogs']);
  assert.ok(!tasks.tools(['tasks:claim']).some(t=>t.name==='hub_claim_task'));
  assert.ok(!tasks.tools(['tasks:review']).some(t=>t.name==='hub_decide_task'));
  assert.ok(tasks.tools(['tasks:review','logs:read']).some(t=>t.name==='hub_decide_task'));
  const response=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},async()=>({capabilities:['tasks:write']}));
  assert.ok(response.result.tools.some(t=>t.name==='hub_write_worklog'));
  assert.ok(!response.result.tools.some(t=>t.name==='hub_claim_task'));
});

test('fixed task routes reject path injection, unknown fields and unapproved gates',()=>{
  assert.throws(()=>tasks.request('hub_get_task',{task_id:'../../other-project'},all,projectId),{code:'INVALID_REQUEST'});
  assert.throws(()=>tasks.request('hub_get_task',{task_id:taskId,project_id:crypto.randomUUID()},all,projectId),{code:'INVALID_REQUEST'});
  assert.throws(()=>tasks.request('hub_list_tasks',{offset:-1},all,projectId),{code:'INVALID_REQUEST'});
  assert.throws(()=>tasks.request('hub_claim_task',{task_id:taskId,instance_id:crypto.randomUUID(),harness:'codex',operation_id:operationId},['tasks:claim'],projectId),{code:'CAPABILITY_REQUIRED'});
  const input={task_id:taskId,gate:'release',approved:true,expected_version:7,evidence:'all passed',instance_id:crypto.randomUUID(),harness:'human',operation_id:operationId};
  assert.throws(()=>tasks.request('hub_decide_task',input,['tasks:review','logs:read'],projectId),{code:'CAPABILITY_REQUIRED'});
  assert.throws(()=>tasks.request('hub_decide_task',input,['tasks:publish'],projectId),{code:'CAPABILITY_REQUIRED'});
});

test('worklog and lease calls carry explicit identities and stable retry keys',()=>{
  const args={task_id:taskId,run_id:crypto.randomUUID(),lease_epoch:3,operation_id:operationId};
  const request=tasks.request('hub_heartbeat_task',args,all,projectId);
  assert.equal(request.path,`/api/projects/${projectId}/tasks/${taskId}/heartbeat`);
  assert.equal(request.method,'POST');
  assert.equal(request.operation_id,operationId);
  assert.deepEqual(request.body,{run_id:args.run_id,lease_epoch:3});
  const read=tasks.request('hub_list_worklogs',{task_id:taskId,offset:20,limit:20},[],projectId);
  assert.equal(read.path,`/api/projects/${projectId}/tasks/${taskId}/worklogs?offset=20&limit=20`);
  assert.equal(read.body,undefined);
  assert.equal(tasks.request('unknown',{},all,projectId),null);
});

test('MCP task errors retain conflict information for safe retries',async()=>{
  const failure=Object.assign(new Error('stale'),{code:'TASK_VERSION_CONFLICT',details:{current_version:8}});
  const response=await dispatch({jsonrpc:'2.0',id:9,method:'tools/call',params:{name:'hub_submit_task',arguments:{}}},async()=>{throw failure;});
  assert.equal(response.result.isError,true);
  assert.equal(JSON.parse(response.result.content[0].text).current_version,8);
});
