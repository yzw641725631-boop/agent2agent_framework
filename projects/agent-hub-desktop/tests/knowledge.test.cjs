'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const knowledge=require('../core/knowledge-tools.cjs'),{dispatch}=require('../core/mcp.cjs');

test('knowledge proposals are scoped, bounded and use a stable idempotency header',()=>{
  const project=crypto.randomUUID(),repo=crypto.randomUUID();
  const args={source_card_id:crypto.randomUUID(),source_hash:'a'.repeat(64),source_quote:'exact source quotation',
    topic:'web.auth.scope',kind:'constraint',title:'Web scope',body:'Keep the authorized Web scope.',reason:'Checked against the exact source.',
    operation_id:crypto.randomUUID(),generation:{method:'local-agent',harness:'codex',model_reported:'author-reported'}};
  const request=knowledge.request('hub_propose_knowledge',args,project,repo,['tasks:write']);
  assert.equal(request.method,'POST');assert.equal(request.operation_id,args.operation_id);
  assert.equal(request.body.repository_id,repo);assert.equal(request.body.operation_id,undefined);
  assert.equal(request.path,`/api/projects/${project}/memory/knowledge`);
  assert.throws(()=>knowledge.request('hub_propose_knowledge',{...args,repository_id:crypto.randomUUID()},project,repo,['tasks:write']),{code:'DOCUMENT_SOURCE_NOT_ACCESSIBLE'});
  assert.throws(()=>knowledge.request('hub_propose_knowledge',args,project,repo,[]),{code:'CAPABILITY_REQUIRED'});
  for(const invalid of [{generation:{token:'never-forward-secrets'}},{source_card_id:'../private'},
    {body:'x'},{kind:'system-authority'},{generation:{input_tokens_reported:-1}},{quote:'unknown-argument'}]){
    assert.throws(()=>knowledge.request('hub_propose_knowledge',{...args,...invalid},project,repo,['tasks:write']),{code:'INVALID_REQUEST'});
  }
});

test('knowledge review requires publishing capability and never accepts a free API path',()=>{
  const project=crypto.randomUUID(),repo=crypto.randomUUID(),fact=crypto.randomUUID();
  const args={fact_id:fact,expected_version:1,state:'active',reason:'Reviewed the cited original content.',operation_id:crypto.randomUUID()};
  const request=knowledge.request('hub_decide_knowledge',args,project,repo,['tasks:publish']);
  assert.equal(request.path,`/api/projects/${project}/memory/knowledge/${fact}/decisions`);
  assert.equal(request.body.fact_id,undefined);assert.equal(request.body.repository_id,repo);
  assert.throws(()=>knowledge.request('hub_decide_knowledge',args,project,repo,['tasks:write']),{code:'CAPABILITY_REQUIRED'});
  assert.throws(()=>knowledge.request('hub_decide_knowledge',{...args,fact_id:'../../other'},project,repo,['tasks:publish']),{code:'INVALID_REQUEST'});
  const search=knowledge.request('hub_search_knowledge',{query:'中文 & % _',include_history:true},project,repo,[]);
  assert.equal(new URL('https://example.test'+search.path).searchParams.get('query'),'中文 & % _');
});

test('MCP exposes knowledge writes only to authorized capabilities and hides them after revocation',async()=>{
  async function names(meta){
    const result=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},async()=>meta);
    return result.result.tools.map(t=>t.name);
  }
  const read=await names({capabilities:['project:read']});
  assert.ok(read.includes('hub_search_knowledge'));assert.ok(!read.includes('hub_propose_knowledge'));
  const write=await names({capabilities:['project:read','tasks:write']});
  assert.ok(write.includes('hub_propose_knowledge'));assert.ok(!write.includes('hub_decide_knowledge'));
  assert.ok((await names({capabilities:['tasks:publish']})).includes('hub_decide_knowledge'));
  assert.ok(!(await names({capabilities:[],closeout_required:true})).includes('hub_search_knowledge'));
});
