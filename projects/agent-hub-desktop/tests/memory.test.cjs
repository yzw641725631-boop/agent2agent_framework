'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {promisify}=require('node:util'),{execFile}=require('node:child_process');
const execute=promisify(execFile);
const memory=require('../core/memory-tools.cjs'),tasks=require('../core/task-tools.cjs');
const {WorkRuntime}=require('../core/work-runtime.cjs'),{HubError,Remote,identity}=require('../core/remote.cjs');
const {Vault}=require('../core/vault.cjs'),{dispatch}=require('../core/mcp.cjs');

test('memory MCP scope cannot switch a repository or inject a source path',async()=>{
  const project=crypto.randomUUID(),repo=crypto.randomUUID(),card=crypto.randomUUID();
  const request=memory.request('hub_search_memory',{query:'中文 & % _',include_history:true},project,repo);
  const url=new URL('https://example.test'+request.path);
  assert.equal(url.searchParams.get('query'),'中文 & % _');
  assert.equal(url.searchParams.get('repository_id'),repo);
  assert.throws(()=>memory.request('hub_search_memory',{repository_id:crypto.randomUUID()},project,repo),{code:'DOCUMENT_SOURCE_NOT_ACCESSIBLE'});
  assert.throws(()=>memory.request('hub_read_memory_source',{card_id:'../../other'},project,repo),{code:'INVALID_REQUEST'});
  assert.throws(()=>memory.request('hub_search_memory',{include_history:'true'},project,repo),{code:'INVALID_REQUEST'});
  assert.throws(()=>memory.request('hub_read_memory_source',{card_id:card,limit:201},project,repo),{code:'INVALID_REQUEST'});
  const listed=await dispatch({jsonrpc:'2.0',id:1,method:'tools/list'},async()=>({capabilities:['project:read']}));
  assert.ok(listed.result.tools.some(t=>t.name==='hub_search_memory'));
  const revoked=await dispatch({jsonrpc:'2.0',id:2,method:'tools/list'},async()=>({closeout_required:true,capabilities:[]}));
  assert.ok(!revoked.result.tools.some(t=>t.name==='hub_search_memory'));
});

test('memory validation routes retain execution identity and never send context IDs as free paths',()=>{
  const project=crypto.randomUUID(),task=crypto.randomUUID(),context=crypto.randomUUID(),run=crypto.randomUUID();
  const request=tasks.request('hub_validate_work_context',{task_id:task,context_id:context,run_id:run,lease_epoch:2},['tasks:write'],project);
  assert.equal(request.path,`/api/projects/${project}/tasks/${task}/contexts/${context}/status?run_id=${run}&lease_epoch=2`);
  assert.equal(request.method,'GET');
});

test('real transport accepts encoded Chinese query values while rejecting encoded or injected API paths',async()=>{
  const profile={origin:'https://example.test',identity:identity(),sessions:{platform:{access_token:'fixture-token'}}};
  let requests=0;
  const remote=new Remote(profile,async()=>{},async url=>{
    requests++;assert.equal(new URL(url).searchParams.get('query'),'中文 & % _');return Response.json({items:[]});
  });
  await remote.request('GET','/api/projects/'+crypto.randomUUID()+'/memory?'+new URLSearchParams({query:'中文 & % _'}));
  for(const route of ['/api/%2e%2e/other','/api/../other','/api/projects?query=%ZZ','/api/projects?query=x?other=y','//example.test/api/projects']){
    await assert.rejects(remote.request('GET',route),{code:'INVALID_API_PATH'});
  }
  assert.equal(requests,1);
});

test('run memory is encrypted at rest and stale/offline/other-instance loads cannot reveal it',async t=>{
  const temp=await fs.realpath(os.tmpdir()),root=await fs.mkdtemp(path.join(temp,'agent-hub-memory-'));
  t.after(async()=>{assert.equal(path.dirname(root),temp);assert.ok(path.basename(root).startsWith('agent-hub-memory-'));await fs.rm(root,{recursive:true,force:true});});
  const workspace=path.join(root,'repo'),data=path.join(root,'desktop');
  await fs.mkdir(workspace);await fs.writeFile(path.join(workspace,'README.md'),'baseline');
  await execute('git',['init','-q',workspace],{windowsHide:true});
  await execute('git',['-C',workspace,'add','.'],{windowsHide:true});
  await execute('git',['-C',workspace,'-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','baseline'],{windowsHide:true});
  const key=crypto.randomBytes(32),protector={isAsyncEncryptionAvailable:async()=>true,
    encryptStringAsync:async value=>{const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);const bytes=Buffer.concat([cipher.update(value),cipher.final()]);return Buffer.concat([iv,cipher.getAuthTag(),bytes]);},
    decryptStringAsync:async bytes=>{const cipher=crypto.createDecipheriv('aes-256-gcm',key,bytes.subarray(0,12));cipher.setAuthTag(bytes.subarray(12,28));return {result:Buffer.concat([cipher.update(bytes.subarray(28)),cipher.final()]).toString()};}};
  const vault=new Vault(data,protector);await vault.load();
  const binding={id:crypto.randomUUID(),projectId:crypto.randomUUID(),repositoryId:crypto.randomUUID(),workspace,status:'active'};
  const profile={user:{id:crypto.randomUUID()},deviceId:crypto.randomUUID(),origin:'https://example.test',bindings:[binding],workRuns:{}};
  vault.data.profiles.account=profile;vault.data.current='account';
  const lease={task_id:crypto.randomUUID(),run_id:crypto.randomUUID(),lease_epoch:1},context=crypto.randomUUID();
  const contentHash='a'.repeat(64),body={items:[{id:crypto.randomUUID(),body:'private-memory-content-不要写入工程'}]};
  let denied=false;
  const c={profile,directory:data,vault,connection:'active',remote:{},persist:()=>vault.save(),emitState(){},
    ensureCurrent(p,r){assert.equal(this.profile,p);assert.equal(this.remote,r);},
    async invokeProjectRemote(b,action){
      if(action==='hub_prepare_work')return {id:context,content_hash:contentHash,payload:{schema_version:2,memory:body,
        policy:{rules:[]},task:{scope:{paths:['src']},execution_mode:'observed'}}};
      if(action==='hub_acknowledge_work')return {can_record:true};
      if(action==='hub_validate_work_context'){
        if(denied)throw new HubError('WORK_MEMORY_STALE');
        return {can_continue:true,content_hash:contentHash,memory:{automatic_extraction:true}};
      }
      throw new Error(action);
    }};
  const runtime=new WorkRuntime(c);t.after(()=>runtime.stop());
  const prepared=await runtime.prepare(binding,{...lease,operation_id:crypto.randomUUID()});
  assert.equal(prepared.local_memory.storage,'os-encrypted-vault');
  await runtime.acknowledge(binding,{...lease,context_id:context,content_hash:contentHash,rule_ids:[],operation_id:crypto.randomUUID()});
  const loaded=await runtime.loadMemory(binding,lease);
  assert.equal(loaded.memory.items[0].body,body.items[0].body);
  assert.equal(loaded.fresh_source_authorization,true);
  const encrypted=await fs.readFile(path.join(data,'vault.bin'));
  assert.ok(!encrypted.includes(Buffer.from('private-memory-content')));
  assert.deepEqual(await fs.readdir(workspace),['.git','README.md']);
  await assert.rejects(runtime.loadMemory(binding,{...lease,run_id:crypto.randomUUID()}),{code:'WORK_CONTEXT_REQUIRED'});
  c.connection='offline';await assert.rejects(runtime.loadMemory(binding,lease),{code:'NETWORK_UNAVAILABLE'});
  c.connection='active';denied=true;
  await assert.rejects(runtime.loadMemory(binding,lease),{code:'WORK_MEMORY_STALE'});
  assert.equal(profile.workRuns[lease.run_id].memory,undefined);
  // Keep the acknowledged baseline for revocation closeout code capture.
  assert.equal(profile.workRuns[lease.run_id].acknowledged,true);
});
