'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const insight=require('../core/insight-tools.cjs');
const binding={projectId:crypto.randomUUID(),repositoryId:crypto.randomUUID()};
test('usage and retrieval tools preserve project binding and distinguish write privileges',()=>{
  const read=insight.request('hub_get_usage',{},binding,[]);
  assert.equal(read.path,`/api/projects/${binding.projectId}/usage?repository_id=${binding.repositoryId}`);
  const args={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),reference_hash:'a'.repeat(64),provider:'host',model:'reported',
    input_tokens:100,output_tokens:20,operation_id:crypto.randomUUID()};
  assert.throws(()=>insight.request('hub_report_usage',args,binding,[]),{code:'CAPABILITY_REQUIRED'});
  assert.equal(insight.request('hub_report_usage',args,binding,['tasks:write']).body.repository_id,binding.repositoryId);
  assert.throws(()=>insight.request('hub_evaluate_memory',{cases:[],operation_id:crypto.randomUUID()},binding,['tasks:publish']),{code:'INVALID_REQUEST'});
  assert.throws(()=>insight.request('hub_get_usage',{repository_id:crypto.randomUUID()},binding,[]),{code:'TASK_REPOSITORY_MISMATCH'});
  assert.ok(!insight.tools(['tasks:write']).some(t=>t.name==='hub_configure_usage_budget'));
});
