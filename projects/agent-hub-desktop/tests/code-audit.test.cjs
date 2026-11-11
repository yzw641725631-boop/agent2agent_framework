'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {tools,request}=require('../core/audit-tools.cjs');
const {dispatch}=require('../core/mcp.cjs');
const {install}=require('../core/host-install.cjs');
const id='610d71dd-bdbc-46fd-85c7-e18a305c01a5',repo='69f8cc84-b7c2-4692-8fd2-74accdd1a1cc';
const caps=['tasks:review','logs:read'];

test('audit IDs cannot escape a repository or task-bound connection',()=>{
  const b={projectId:id,repositoryId:repo,taskId:repo};
  const detail=request('hub_read_code_audit',{job_id:id},b,[]);
  assert.equal(detail.path,`/api/projects/${id}/code-audits/${id}?task_id=${repo}&repository_id=${repo}`);
  assert.throws(()=>request('hub_get_code_audit_policy',{repository_id:id},b,[]),{code:'TASK_REPOSITORY_MISMATCH'});
  assert.throws(()=>request('hub_list_code_audits',{task_id:id},b,[]),{code:'TASK_WORKTREE_MISMATCH'});
  assert.throws(()=>request('hub_read_code_audit',{job_id:'../private'},b,[]),{code:'INVALID_REQUEST'});
  assert.throws(()=>request('hub_claim_code_audit',{job_id:id,expected_version:1,instance_id:id,operation_id:id},b,['tasks:write']),{code:'CAPABILITY_REQUIRED'});
  assert.ok(!tools(['tasks:review']).some(t=>t.name==='hub_complete_code_audit'));
});

test('MCP advertises authorized audit tools and emits source material once',async()=>{
  const invoke=async name=>name==='metadata'?{capabilities:caps}:{marker:'single review input'};
  const listed=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},invoke);
  assert.ok(listed.result.tools.some(t=>t.name==='hub_claim_code_audit'));
  assert.ok(!listed.result.tools.some(t=>t.name==='hub_set_code_audit_policy'));
  const response=await dispatch({jsonrpc:'2.0',id:2,method:'tools/call',params:{name:'hub_get_code_audit_input',arguments:{job_id:id,instance_id:id,lease_epoch:1}}},invoke);
  assert.equal(response.result.isError,false);
  assert.equal(response.result.structuredContent,undefined);
  assert.equal(response.result.content.length,1);
});

test('source paging reaches the end of a 16 MiB baseline without enlarging response slices',()=>{
  const args={job_id:id,instance_id:id,lease_epoch:1,path:'src/large.txt',side:'before',offset:16*1024*1024-6000,limit:6000};
  const result=request('hub_read_code_audit_source',args,{projectId:id,repositoryId:repo},caps);
  assert.ok(result.path.includes('offset=16771216'));
  assert.throws(()=>request('hub_read_code_audit_source',{...args,offset:16*1024*1024+1},{projectId:id},caps),{code:'INVALID_REQUEST'});
  assert.throws(()=>request('hub_read_code_audit_source',{...args,limit:12001},{projectId:id},caps),{code:'INVALID_REQUEST'});
});

test('structured reports validate arbitrary approved rule IDs without accepting values of other types',()=>{
  const args={job_id:id,operation_id:id,expected_version:2,instance_id:id,lease_epoch:1,source_hash:'a'.repeat(64),
    content:{conclusion:'incomplete',summary:'未取得完整代码，需要留下覆盖缺口。',checkout_commit:'b'.repeat(40),source_basis:'archived_worktree',
      reviewed_paths:['src/app.ts'],rule_checks:{layers:'unverified'},findings:[],gaps:['源码尚待核对'],verification:'仅读取归档，不声称远程提交代码已完成审查。'}};
  const result=request('hub_complete_code_audit',args,{projectId:id,repositoryId:repo},caps);
  assert.equal(result.body.content.rule_checks.layers,'unverified');
  assert.throws(()=>request('hub_complete_code_audit',{...args,content:{...args.content,rule_checks:{layers:true}}},{projectId:id},caps),{code:'INVALID_REQUEST'});
});

test('review Skill is installed and upgraded under the same ownership receipt, preserving user changes',async t=>{
  const base=await fs.realpath(os.tmpdir()),root=await fs.mkdtemp(path.join(base,'hub-audit-install-'));
  t.after(async()=>{const actual=await fs.realpath(root);assert.equal(path.dirname(actual),base);assert.match(path.basename(actual),/^hub-audit-install-/);await fs.rm(actual,{recursive:true,force:true});});
  const home=path.join(root,'home'),receiptDirectory=path.join(root,'receipts');await fs.mkdir(home);
  const options={home,receiptDirectory,hosts:['codex','claude','cursor'],name:'agent-hub-setup',
    entry:{type:'stdio',command:'C:\\Agent Hub\\Agent Hub.exe',args:['bridge.cjs'],env:{ELECTRON_RUN_AS_NODE:'1'}},
    skillDirectory:path.resolve(__dirname,'../../../skills/agent-hub'),
    additionalSkills:[{name:'agent-hub-code-review',directory:path.resolve(__dirname,'../../../skills/agent-hub-code-review')}]};
  await install(options);
  const skill=path.join(home,'.agents/skills/agent-hub-code-review/SKILL.md');await fs.access(skill);
  await fs.access(path.join(home,'.claude/skills/agent-hub-code-review/SKILL.md'));
  await fs.access(path.join(home,'.cursor/skills/agent-hub-code-review/SKILL.md'));
  assert.equal((await install(options)).changed,false);
  await fs.appendFile(skill,'\nUser review convention.\n');
  await assert.rejects(install(options),{code:'CONFIGURATION_CONFLICT'});
  assert.match(await fs.readFile(skill,'utf8'),/User review convention/);
});
