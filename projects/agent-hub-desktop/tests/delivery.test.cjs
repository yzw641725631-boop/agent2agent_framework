'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {tools,request}=require('../core/delivery-tools.cjs');
const {dispatch}=require('../core/mcp.cjs');
const id='610d71dd-bdbc-46fd-85c7-e18a305c01a5',repo='69f8cc84-b7c2-4692-8fd2-74accdd1a1cc';
test('delivery tools pin project and repository while separating publish and observation permissions',()=>{
  const binding={projectId:id,repositoryId:repo};
  assert.ok(tools([]).some(t=>t.name==='hub_list_delivery_evidence'));
  assert.ok(!tools(['tasks:write']).some(t=>t.name==='hub_set_delivery_policy'));
  assert.equal(request('hub_list_delivery_evidence',{task_id:id,offset:30,limit:30},binding,[]).path,
    `/api/projects/${id}/tasks/${id}/delivery-evidence?repository_id=${repo}&offset=30&limit=30`);
  assert.throws(()=>request('hub_get_delivery_policy',{repository_id:id},binding,[]),{code:'TASK_REPOSITORY_MISMATCH'});
  const body={task_id:id,worklog_id:id,expected_task_version:3,kind:'ci',provider_id:45,merge_evidence_id:id,operation_id:id};
  const result=request('hub_verify_delivery_evidence',body,binding,['tasks:write']);
  assert.equal(result.body.merge_evidence_id,id);
  assert.equal(result.path,`/api/projects/${id}/tasks/${id}/delivery-evidence?repository_id=${repo}`);
  assert.equal(result.operation_id,id);
  assert.throws(()=>request('hub_verify_delivery_evidence',body,binding,[]),{code:'CAPABILITY_REQUIRED'});
  for(const invalid of [true,-1,Number.MAX_SAFE_INTEGER+1,'https://evil.example'])
    assert.throws(()=>request('hub_verify_delivery_evidence',{...body,provider_id:invalid},binding,['tasks:write']),{code:'INVALID_REQUEST'});
  assert.throws(()=>request('hub_list_delivery_evidence',{task_id:'../secrets'},binding,[]),{code:'INVALID_REQUEST'});
});
test('delivery policy forwards only validated fields and never accepts arbitrary provider URLs or commands',()=>{
  const binding={projectId:id},args={repository_id:repo,expected_version:0,required_kinds:['merge','ci'],
    workflows:['.github/workflows/test.yml'],ci_target:'merged',change_reason:'明确核验合并后的实际提交。',operation_id:id};
  assert.equal(request('hub_set_delivery_policy',args,binding,['tasks:publish']).method,'PUT');
  assert.throws(()=>request('hub_set_delivery_policy',{...args,workflows:['../../run.sh']},binding,['tasks:publish']),{code:'INVALID_REQUEST'});
  assert.throws(()=>request('hub_set_delivery_policy',{...args,url:'https://evil.example'},binding,['tasks:publish']),{code:'INVALID_REQUEST'});
});
test('MCP advertises delivery readers and only permitted mutations',async()=>{
  const invoke=async name=>name==='metadata'?{capabilities:['tasks:write']}:{name};
  const listed=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},invoke);
  assert.ok(listed.result.tools.some(t=>t.name==='hub_verify_delivery_evidence'));
  assert.ok(!listed.result.tools.some(t=>t.name==='hub_set_delivery_policy'));
  const result=await dispatch({jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'hub_list_delivery_evidence',arguments:{task_id:id}}},invoke);
  assert.equal(result.result.structuredContent.name,'hub_list_delivery_evidence');
});
