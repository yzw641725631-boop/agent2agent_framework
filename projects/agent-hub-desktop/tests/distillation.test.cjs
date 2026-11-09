'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {request,tools}=require('../core/distillation-tools.cjs'),{dispatch}=require('../core/mcp.cjs');
const id=crypto.randomUUID,project=id(),repo=id(),job=id(),binding={projectId:project,repositoryId:repo};
const claim=()=>({job_id:job,expected_version:1,instance_id:id(),operation_id:id()});
const candidate=()=>({source_card_id:id(),source_hash:'a'.repeat(64),source_quote:'逐字原文证据，需要实际核对。',
  topic:'web.auth.scope',kind:'constraint',title:'保持授权范围',body:'按照当前授权范围核对这项接口边界。',reason:'本项约定对后续开发交接需要持续保留。',generation:{method:'local-agent',harness:'codex'}});
test('distillation tools pin both list and job actions to the bound project/repository',()=>{
  const page=request('hub_list_distillations',{state:'queued',offset:20,limit:20},binding,[]);
  assert.equal(page.path,`/api/projects/${project}/distillations?repository_id=${repo}&state=queued&offset=20&limit=20`);
  assert.throws(()=>request('hub_list_distillations',{repository_id:id()},binding,[]),{code:'TASK_REPOSITORY_MISMATCH'});
  const args=claim(),result=request('hub_claim_distillation',args,binding,['tasks:write']);
  assert.equal(result.path,`/api/projects/${project}/distillations/${job}/claim?repository_id=${repo}`);
  assert.equal(result.operation_id,args.operation_id);assert.equal(result.body.operation_id,undefined);
  assert.equal(result.body.job_id,undefined);
  assert.throws(()=>request('hub_claim_distillation',args,binding,[]),{code:'CAPABILITY_REQUIRED'});
  assert.throws(()=>request('hub_read_distillation_input',{job_id:'../other'},binding,[]),{code:'INVALID_REQUEST'});
});
test('only publishers configure bounded preparation and no tool accepts model credentials or commands',()=>{
  const args={repository_id:repo,expected_version:0,enabled:true,min_sources:2,max_sources:4,input_budget_bytes:12000,
    max_candidates:2,reason:'按仓库明确准备适量来源供本地提炼。',operation_id:id()};
  assert.equal(request('hub_set_distillation_policy',args,binding,['tasks:publish']).method,'PUT');
  assert.throws(()=>request('hub_set_distillation_policy',args,binding,['tasks:write']),{code:'CAPABILITY_REQUIRED'});
  for(const patch of [{min_sources:5},{input_budget_bytes:9999},{max_candidates:4},{enabled:'true'},
    {command:'execute'},{api_key:'never-forward-credentials'}])
    assert.throws(()=>request('hub_set_distillation_policy',{...args,...patch},binding,['tasks:publish']),{code:'INVALID_REQUEST'});
  assert.ok(!tools(['tasks:write']).some(t=>t.name==='hub_set_distillation_policy'));
});
test('completion preserves exact citations and lease fencing while requiring an explicit result',()=>{
  const args={...claim(),lease_epoch:1,source_hash:'b'.repeat(64),outcome:'proposed',candidates:[candidate()],reason:'本次核对固定资料后，提出一条候选等待独立审核。'};
  const result=request('hub_complete_distillation',args,binding,['tasks:write']);
  assert.deepEqual(result.body.candidates,args.candidates);assert.equal(result.body.lease_epoch,1);
  for(const patch of [{candidates:[]},{lease_epoch:0},{candidates:Array(4).fill(candidate())},
    {candidates:[{...candidate(),repository_id:id()}]},{candidates:[{...candidate(),generation:{token:'forbidden'}}]}])
    assert.throws(()=>request('hub_complete_distillation',{...args,...patch},binding,['tasks:write']),{code:'INVALID_REQUEST'});
  assert.equal(request('hub_complete_distillation',{...args,outcome:'no_new_knowledge',candidates:[]},binding,['tasks:write']).body.outcome,'no_new_knowledge');
  assert.throws(()=>request('hub_complete_distillation',{...args,outcome:'no_new_knowledge'},binding,['tasks:write']),{code:'INVALID_REQUEST'});
});
test('MCP readers and authorized actions are available only before revocation',async()=>{
  const names=async meta=>(await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},async()=>meta)).result.tools.map(t=>t.name);
  assert.ok((await names({capabilities:[]})).includes('hub_list_distillations'));
  const allowed=await names({capabilities:['tasks:write']});
  assert.ok(allowed.includes('hub_complete_distillation'));assert.ok(!allowed.includes('hub_set_distillation_policy'));
  assert.ok(!(await names({capabilities:['tasks:write'],closeout_required:true})).includes('hub_list_distillations'));
  const result=await dispatch({jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'hub_read_distillation_input',arguments:{job_id:job}}},async name=>({name}));
  assert.equal(result.result.structuredContent.name,'hub_read_distillation_input');
});
