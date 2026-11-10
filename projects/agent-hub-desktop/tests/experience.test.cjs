'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {WorkRuntime}=require('../core/work-runtime.cjs'),{Vault}=require('../core/vault.cjs');
const tasks=require('../core/task-tools.cjs'),cache=require('../core/experience-cache.cjs');
const {dispatch}=require('../core/mcp.cjs');

function response(args,item,invalid=false){
  const known=args.known || [],unchanged=known.some(v=>v.id===item.id && v.revision===item.revision);
  const exhausted=args.consumed_units>=23000;
  const data={schema_version:1,engine:'native-source-authorized-v1',
    items:args.validate_only || exhausted || unchanged?[]:[item],
    invalidated:invalid?['EXP-'+item.id]:[],unchanged:unchanged?1:0,omitted:0,gaps:[],
    delivery:{session_id:args.memory_session_id,receipt:crypto.randomBytes(32).toString('hex'),
      state:args.validate_only?'validated':exhausted?'read-budget-exhausted':'delivered'}};
  data.budget={limit:args.max_tokens || 2048,used:0,serialized_bytes:0,method:'utf8-bytes-conservative-fallback'};
  for(let i=0;i<10;i++)data.budget.used=data.budget.serialized_bytes=Buffer.byteLength(JSON.stringify(data));
  return data;
}

test('experience MCP routes and schemas reject forged cache state and scope switching',()=>{
  const project=crypto.randomUUID(),args={task_id:crypto.randomUUID(),context_id:crypto.randomUUID(),run_id:crypto.randomUUID(),
    lease_epoch:1,memory_session_id:crypto.randomUUID(),query:'中文登录错误',max_tokens:2048};
  const request=tasks.request('hub_recall_experience',args,['tasks:write'],project);
  assert.equal(request.method,'POST');assert.ok(!request.path.includes('?'));
  assert.equal(request.body.query,args.query);assert.equal(request.body.context_id,undefined);
  for(const patch of [{known:[]},{consumed_units:0},{max_tokens:10000},{experience_ids:[crypto.randomUUID(),crypto.randomUUID(),...Array(11).fill(crypto.randomUUID())]},
    {memory_session_id:'../../elsewhere'},{after_receipt:'fake'},{repository_id:crypto.randomUUID()}])
    assert.throws(()=>tasks.validate('hub_recall_experience',{...args,...patch}),{code:'INVALID_REQUEST'});
  const internal=tasks.request('hub_recall_experience',{...args,known:[],validate_only:true,consumed_units:0},['tasks:write'],project,{internal:true});
  assert.equal(internal.body.validate_only,true);
});

test('session receipts avoid repeated prose; new sessions reload and withdrawal clears encrypted cache',async t=>{
  const temp=await fs.realpath(os.tmpdir()),dir=await fs.mkdtemp(path.join(temp,'agent-hub-experience-'));
  t.after(async()=>{assert.equal(path.dirname(dir),temp);assert.ok(path.basename(dir).startsWith('agent-hub-experience-'));await fs.rm(dir,{recursive:true,force:true});});
  const key=crypto.randomBytes(32),protector={
    async isAsyncEncryptionAvailable(){return true;},
    async encryptStringAsync(s){const iv=crypto.randomBytes(12),c=crypto.createCipheriv('aes-256-gcm',key,iv);return Buffer.concat([iv,c.update(s),c.final(),c.getAuthTag()]);},
    async decryptStringAsync(raw){const d=crypto.createDecipheriv('aes-256-gcm',key,raw.subarray(0,12));d.setAuthTag(raw.subarray(-16));return {result:Buffer.concat([d.update(raw.subarray(12,-16)),d.final()]).toString()};}};
  const vault=new Vault(dir,protector);await vault.load();
  const binding={id:crypto.randomUUID(),projectId:crypto.randomUUID(),workspace:dir,status:'active'};
  const profile={user:{id:crypto.randomUUID()},deviceId:crypto.randomUUID(),origin:'https://example.test',workRuns:{}};
  vault.data.profiles.account=profile;vault.data.current='account';
  const args={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),lease_epoch:1,context_id:crypto.randomUUID(),memory_session_id:crypto.randomUUID()};
  const hash='a'.repeat(64),item={id:crypto.randomUUID(),kind:'knowledge',revision:'b'.repeat(64),body:'private-durable-experience-必须加密'};
  item.experience_id='EXP-'+item.id;
  let withdrawn=false;
  const c={profile,directory:dir,vault,remote:{},connection:'active',persist:()=>vault.save(),
    ensureCurrent(p,r){assert.equal(this.profile,p);assert.equal(this.remote,r);},
    async invokeProjectRemote(b,action,a){
      if(action==='hub_validate_work_context')return {content_hash:hash};
      if(action==='hub_recall_experience')return response(a,item,withdrawn);
      throw new Error(action);
    }};
  const runtime=new WorkRuntime(c);t.after(()=>runtime.stop());
  const run=profile.workRuns[args.run_id]={binding_id:binding.id,project_id:binding.projectId,task_id:args.task_id,run_id:args.run_id,
    lease_epoch:1,workspace:dir,owner:runtime.owner(),context_id:args.context_id,context_hash:hash,
    memory_required:true,memory:{items:[]},role:{id:'developer'},acknowledged:true};
  const first=await runtime.recallExperience(binding,args);assert.equal(first.items[0].body,item.body);
  const repeated=await runtime.recallExperience(binding,args);assert.equal(repeated.items.length,1); // Lost acknowledgements never silently suppress facts.
  const delta=await runtime.recallExperience(binding,{...args,after_receipt:repeated.delivery.receipt});
  assert.equal(delta.items.length,0);assert.ok(!JSON.stringify(delta).includes(item.body));
  const newSession=await runtime.recallExperience(binding,{...args,memory_session_id:crypto.randomUUID()});
  assert.equal(newSession.items[0].body,item.body);
  assert.ok(!(await fs.readFile(path.join(dir,'vault.bin'))).includes(Buffer.from(item.body)));
  const reopened=new Vault(dir,protector);await reopened.load();
  assert.equal(reopened.data.profiles.account.workRuns[args.run_id].experience_cache['knowledge:'+item.id].body,item.body);
  const base=await runtime.loadMemory(binding,args);
  assert.ok(base.memory);
  const unchanged=await runtime.loadMemory(binding,{...args,known_content_hash:hash});
  assert.equal(unchanged.unchanged,true);assert.equal(unchanged.memory,null);
  await assert.rejects(runtime.recallExperience(binding,{...args,after_receipt:'c'.repeat(64)}),{code:'MEMORY_RECEIPT_MISMATCH'});
  run.experience_units=24000;
  const exhausted=await runtime.recallExperience(binding,args);assert.equal(exhausted.items.length,0);
  assert.equal(exhausted.delivery.state,'read-budget-exhausted');
  withdrawn=true;
  await assert.rejects(runtime.loadMemory(binding,args),{code:'WORK_EXPERIENCE_STALE'});
  assert.equal(run.experience_cache,undefined);assert.equal(run.memory,undefined);assert.equal(run.role,undefined);
  assert.equal(run.acknowledged,true);
});

test('invalid complete-response measurement cannot enter the cache',()=>{
  const args={memory_session_id:crypto.randomUUID()},item={id:crypto.randomUUID(),kind:'knowledge',revision:'a'.repeat(64)};
  item.experience_id='EXP-'+item.id;
  const data=response(args,item);data.budget.serialized_bytes--;
  const run={};assert.throws(()=>cache.accept(run,data,args.memory_session_id),{code:'MEMORY_RESPONSE_INVALID'});
  assert.equal(run.experience_cache,undefined);
});

test('repreparing a run while experience validation is pending rejects the old context',async()=>{
  const id=crypto.randomUUID(),item={id:crypto.randomUUID(),kind:'knowledge',revision:'a'.repeat(64)};
  item.experience_id='EXP-'+item.id;
  const old={run_id:id,memory_required:true,memory:{items:[]},context_hash:'b'.repeat(64),
    experience_cache:{['knowledge:'+item.id]:item}};
  const profile={workRuns:{[id]:old}},replacement={...old,memory:{current:true}};
  const c={directory:os.tmpdir(),vault:{},profile,remote:{},connection:'active',
    async persist(){},ensureCurrent(p,r){assert.equal(p,this.profile);assert.equal(r,this.remote);},
    async invokeProjectRemote(binding,action,args){
      if(action==='hub_validate_work_context')return {content_hash:old.context_hash};
      assert.equal(action,'hub_recall_experience');
      profile.workRuns[id]=replacement;
      return response(args,item);
    }};
  const runtime=new WorkRuntime(c);
  await assert.rejects(runtime.validateMemory({status:'active'},old),{code:'WORK_CONTEXT_STALE'});
  assert.deepEqual(replacement.memory,{current:true});
});

test('stdio experience output contains one copy of the bounded response',async()=>{
  const data={items:[{body:'one-copy-of-experience'}]};
  const result=await dispatch({jsonrpc:'2.0',id:1,method:'tools/call',params:{name:'hub_recall_experience',arguments:{}}},async()=>data);
  assert.equal(result.result.structuredContent,undefined);
  assert.equal(result.result.content.length,1);
  assert.deepEqual(JSON.parse(result.result.content[0].text),data);
});
