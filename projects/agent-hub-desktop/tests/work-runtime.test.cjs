'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const observer=require('../core/git-observer.cjs'),{WorkStore}=require('../core/work-store.cjs'),{WorkRuntime}=require('../core/work-runtime.cjs');
const {HubError}=require('../core/remote.cjs');
const execute=promisify(execFile),scope={paths:['src','tests']},policy={version:0,content_hash:'a'.repeat(64),protected_paths:['src/frozen'],rules:[{id:'B-SCOPE',text:'Only web'}]};
async function temporary(t){
  const base=await fs.realpath(os.tmpdir()),dir=await fs.mkdtemp(path.join(base,'agent-hub-work-'));
  t.after(async()=>{const rel=path.relative(base,dir);assert.ok(rel.startsWith('agent-hub-work-') && !rel.includes(path.sep));await fs.rm(dir,{recursive:true,force:true});});return dir;
}
async function repo(t){
  const dir=await temporary(t);
  await execute('git',['init','-q',dir],{windowsHide:true});
  await fs.mkdir(path.join(dir,'src/frozen'),{recursive:true});await fs.mkdir(path.join(dir,'tests'));
  await fs.writeFile(path.join(dir,'src/main.ts'),'initial\n');await fs.writeFile(path.join(dir,'src/frozen/old.ts'),'protected\n');
  await execute('git',['-C',dir,'add','.'],{windowsHide:true});
  await execute('git',['-C',dir,'-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','baseline'],{windowsHide:true});return dir;
}
function protector(){
  const key=crypto.randomBytes(32);return {
    async encryptStringAsync(value){const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);const bytes=Buffer.concat([cipher.update(value,'utf8'),cipher.final()]);return Buffer.concat([iv,cipher.getAuthTag(),bytes]);},
    async decryptStringAsync(bytes){const cipher=crypto.createDecipheriv('aes-256-gcm',key,bytes.subarray(0,12));cipher.setAuthTag(bytes.subarray(12,28));return {result:Buffer.concat([cipher.update(bytes.subarray(28)),cipher.final()]).toString('utf8')};},
  };
}

test('real Git observer includes tracked, committed, untracked and deleted changes with content hashes',async t=>{
  const dir=await repo(t),base=await observer.initialize(dir);
  await fs.writeFile(path.join(dir,'src/main.ts'),'modified\n');
  await execute('git',['-C',dir,'add','src/main.ts'],{windowsHide:true});
  await execute('git',['-C',dir,'-c','user.name=Test','-c','user.email=test@example.org','commit','-qm','task change'],{windowsHide:true});
  await fs.writeFile(path.join(dir,'src/new.ts'),'new\n');await fs.unlink(path.join(dir,'src/main.ts'));
  const result=await observer.capture(dir,base.base_commit,scope,policy);
  assert.deepEqual(result.evidence.files.map(f=>[f.path,f.status]),[['src/main.ts','deleted'],['src/new.ts','added']]);
  assert.equal(result.contents[0].content_base64,Buffer.from('new\n').toString('base64'));
  assert.notEqual(result.evidence.base_commit,result.evidence.head_commit);
  assert.equal(result.evidence_hash,observer.digest(result.evidence));
});

test('observer refuses dirty starts, protected modifications, ignored declarations and unsafe links',async t=>{
  const dir=await repo(t),base=await observer.initialize(dir);
  await fs.writeFile(path.join(dir,'src/frozen/old.ts'),'forbidden\n');
  await assert.rejects(observer.initialize(dir),{code:'WORKTREE_NOT_CLEAN'});
  await assert.rejects(observer.capture(dir,base.base_commit,scope,policy),{code:'WORK_PATH_PROTECTED'});
  assert.equal(observer.violations(['SRC/FROZEN/old.ts'],scope,policy)[0].code,'WORK_PATH_PROTECTED');
  assert.equal(observer.violations(['pc/new.ts'],scope,policy)[0].code,'TASK_PATH_OUT_OF_SCOPE');
  assert.throws(()=>observer.violations(['src/../secret'],scope,policy),{code:'WORK_PATH_UNSAFE'});
  const outside=await temporary(t);await fs.mkdir(path.join(dir,'tests/linked'),{recursive:true});
  await fs.rmdir(path.join(dir,'tests/linked'));await fs.symlink(outside,path.join(dir,'tests/linked'),'junction');
  await fs.writeFile(path.join(outside,'hidden.ts'),'must not read');
  await assert.rejects(observer.safeRead(dir,'tests/linked/hidden.ts'),{code:'WORK_PATH_UNSAFE'});
});

test('owned untracked framework files can be omitted without hiding tracked or unknown changes',async t=>{
  const dir=await repo(t),relative='.agent-hub/framework/agent-hub-test/main.md';
  await fs.mkdir(path.dirname(path.join(dir,relative)),{recursive:true});await fs.writeFile(path.join(dir,relative),'local framework');
  const options={ignoreUntracked:[relative]},base=await observer.initialize(dir,options);
  assert.equal((await observer.capture(dir,base.base_commit,scope,policy,options)).evidence.files.length,0);
  await fs.writeFile(path.join(dir,'.agent-hub/unknown.md'),'not owned');
  await assert.rejects(observer.initialize(dir,options),{code:'WORKTREE_NOT_CLEAN'});
  await assert.rejects(observer.capture(dir,base.base_commit,scope,policy,options),{code:'TASK_PATH_OUT_OF_SCOPE'});
  await fs.unlink(path.join(dir,'.agent-hub/unknown.md'));
  await execute('git',['-C',dir,'add','--',relative],{windowsHide:true});
  await assert.rejects(observer.initialize(dir,options),{code:'WORKTREE_NOT_CLEAN'});
  await assert.rejects(observer.capture(dir,base.base_commit,scope,policy,options),{code:'TASK_PATH_OUT_OF_SCOPE'});
});

test('encrypted checkpoint store survives reopen, hides other identities and exports into a new directory',async t=>{
  const dir=await repo(t),data=await temporary(t),key=protector(),base=await observer.initialize(dir);
  await fs.writeFile(path.join(dir,'src/main.ts'),'private changed code');
  const snapshot=await observer.capture(dir,base.base_commit,scope,policy),owner={user_id:'user',device_id:'device',origin:'https://hub.example'};
  const record={schema:1,operation_id:crypto.randomUUID(),owner,created_at:new Date().toISOString(),body:{notes:'private note'},snapshot};
  const store=new WorkStore(data,key);await store.write(record,{create:true});
  assert.ok(!(await fs.readFile(store.file(record.operation_id))).includes(Buffer.from('private note')));
  const reopened=new WorkStore(data,key);assert.deepEqual(await reopened.read(record.operation_id),record);
  assert.equal((await reopened.list({...owner,user_id:'other'})).length,0);
  const target=path.join(data,'export');await reopened.export(record,target);
  assert.equal(await fs.readFile(path.join(target,'changed-files/src/main.ts'),'utf8'),'private changed code');
  await assert.rejects(reopened.export(record,target),{code:'EEXIST'});
});

async function runtimeFixture(t){
  const dir=await repo(t),data=await temporary(t),runId=crypto.randomUUID(),taskId=crypto.randomUUID(),contextId=crypto.randomUUID();
  const binding={id:crypto.randomUUID(),projectId:crypto.randomUUID(),workspace:dir,status:'active'};
  const profile={user:{id:crypto.randomUUID()},deviceId:crypto.randomUUID(),origin:'https://hub.example',bindings:[binding],workRuns:{}};
  let offline=false,uploads=0,heartbeats=0;
  const c={profile,directory:data,vault:{protector:protector()},connection:'active',persist:async()=>{},emitState(){},ensureCurrent(p,r){assert.equal(p,this.profile);assert.equal(r,this.remote);},remote:{}};
  c.invokeProjectRemote=async(b,action,args)=>{
    assert.equal(b,binding);
    if(offline)throw new HubError('NETWORK_UNAVAILABLE');
    if(action==='hub_prepare_work')return {id:contextId,content_hash:'b'.repeat(64),payload:{policy,task:{scope,execution_mode:'observed'}}};
    if(action==='hub_acknowledge_work')return {can_record:true};
    if(action==='hub_get_policy')return policy;
    if(action==='hub_get_task')return {task:{status:'in_progress',active_run_id:runId,lease_epoch:1,
      lease_until:new Date(Date.now()+600000).toISOString(),execution_mode:'observed',scope}};
    if(action==='hub_write_worklog'){uploads++;return {id:'server-log',evidence_hash:observer.digest(args.change_evidence)};}
    if(action==='hub_submit_task')return {status:'review'};
    if(action==='hub_heartbeat_task'){heartbeats++;return {task:{lease_until:new Date(Date.now()+600000).toISOString()}};}
    throw new Error('Unexpected action '+action);
  };
  const runtime=new WorkRuntime(c);t.after(()=>runtime.stop());
  const lease={task_id:taskId,run_id:runId,lease_epoch:1};
  await runtime.prepare(binding,{...lease,operation_id:crypto.randomUUID()});
  await runtime.acknowledge(binding,{...lease,context_id:contextId,content_hash:'b'.repeat(64),rule_ids:['B-SCOPE'],operation_id:crypto.randomUUID()});
  return {dir,data,binding,c,profile,runtime,lease,setOffline(value){offline=value;c.connection=value?'offline':'active';},uploads:()=>uploads,heartbeats:()=>heartbeats};
}

test('offline work is durably queued, retries once and rejects a changed retry payload',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'round code');f.setOffline(true);
  const args={...f.lease,summary:'round',notes:'actual code changed',tests:'not run',next_steps:'review',operation_id:crypto.randomUUID()};
  const pending=await f.runtime.save(f.binding,args);
  assert.equal(pending.server_confirmed,false);assert.equal(pending.local_code_backup,true);assert.equal(f.uploads(),0);
  f.setOffline(false);
  const uploaded=await f.runtime.save(f.binding,args);assert.equal(uploaded.server_confirmed,true);assert.equal(f.uploads(),1);
  await f.runtime.save(f.binding,args);assert.equal(f.uploads(),1);
  await assert.rejects(f.runtime.save(f.binding,{...args,notes:'changed retry'}),{code:'IDEMPOTENCY_PAYLOAD_CHANGED'});
  const list=await f.runtime.list(f.binding);assert.equal(list.items[0].state,'shared');
});

test('new changes after the logged checkpoint prevent submit; forbidden files cannot be omitted from a log',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'round code');
  const args={...f.lease,summary:'round',notes:'changed',tests:'one passed',next_steps:'review',operation_id:crypto.randomUUID()};
  await assert.rejects(f.runtime.save(f.binding,{...args,changed_paths:[]}),{code:'WORK_CHANGE_PATHS_MISMATCH'});
  await f.runtime.save(f.binding,args);
  await fs.writeFile(path.join(f.dir,'src/main.ts'),'changed after checkpoint');
  await assert.rejects(f.runtime.submit(f.binding,{...f.lease,worklog_id:'server-log',expected_version:5,operation_id:crypto.randomUUID()}),{code:'WORK_EVIDENCE_CHANGED'});
  await fs.writeFile(path.join(f.dir,'src/frozen/old.ts'),'not in declared changes');
  await assert.rejects(f.runtime.save(f.binding,{...args,operation_id:crypto.randomUUID(),changed_paths:['src/main.ts']}),{code:'WORK_PATH_PROTECTED'});
});

test('expired or revoked upload remains blocked in the encrypted store and never becomes shared',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'kept on revocation');
  f.c.invokeProjectRemote=async()=>{throw new HubError('TASK_LEASE_STALE');};
  const r=await f.runtime.save(f.binding,{...f.lease,summary:'interrupted',notes:'kept',tests:'not run',next_steps:'handoff',operation_id:crypto.randomUUID()});
  assert.equal(r.state,'blocked');assert.equal(r.server_confirmed,false);assert.equal(r.last_error,'TASK_LEASE_STALE');
  assert.ok((await f.runtime.store.read(r.local_checkpoint_id)).snapshot.contents.length);
});

test('background heartbeat does not call a model and stops renewing an inactive execution',async t=>{
  const f=await runtimeFixture(t);await f.runtime.tick();assert.equal(f.heartbeats(),1);
  const run=f.profile.workRuns[f.lease.run_id];run.next_heartbeat=0;run.last_activity=Date.now()-601000;
  await f.runtime.tick();assert.equal(f.heartbeats(),1);
});

test('offline background observations retain actual code without claiming a shared worklog',async t=>{
  const f=await runtimeFixture(t);f.setOffline(true);
  await fs.writeFile(path.join(f.dir,'src/main.ts'),'offline observation one');
  await f.runtime.tick();
  const run=f.profile.workRuns[f.lease.run_id],id=run.automatic_checkpoint_id;
  let record=await f.runtime.store.read(id);
  assert.equal(record.state,'local_only');assert.equal(f.uploads(),0);assert.equal(f.heartbeats(),0);
  await fs.writeFile(path.join(f.dir,'src/main.ts'),'offline observation two');run.next_observe=0;
  await f.runtime.tick();record=await f.runtime.store.read(id);
  assert.equal(Buffer.from(record.snapshot.contents[0].content_base64,'base64').toString(),'offline observation two');
  assert.equal((await f.runtime.list(f.binding)).items.length,1);
  f.setOffline(false);await f.runtime.tick();assert.equal(f.uploads(),0);
});

test('account switch while capturing cannot assign old code to the new account or upload it',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'old account code');
  const original=observer.capture;
  observer.capture=async(...args)=>{
    const result=await original(...args);
    f.c.profile={...f.profile,user:{id:crypto.randomUUID()},workRuns:{},bindings:[]};f.c.remote={};
    return result;
  };
  t.after(()=>{observer.capture=original;});
  const saved=await f.runtime.save(f.binding,{...f.lease,summary:'old owner',notes:'old code',tests:'not run',next_steps:'handoff',operation_id:crypto.randomUUID()});
  assert.equal(saved.server_confirmed,false);assert.equal(f.uploads(),0);
  assert.equal((await f.runtime.list()).items.length,0);
  const stored=await f.runtime.store.read(saved.local_checkpoint_id);assert.equal(stored.owner.user_id,f.profile.user.id);
});

test('logout during background capture stops renewal and keeps the checkpoint under the original owner',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'code on logout');
  const original=observer.capture;
  observer.capture=async(...args)=>{const result=await original(...args);f.c.connection='signed_out';f.c.remote=null;return result;};
  t.after(()=>{observer.capture=original;});
  await f.runtime.tick();assert.equal(f.uploads(),0);assert.equal(f.heartbeats(),0);
  assert.equal((await f.runtime.store.list(f.runtime.owner())).length,1);
});

test('rejected heartbeat stops renewal and projections while retaining the closeout baseline',async t=>{
  const f=await runtimeFixture(t),run=f.profile.workRuns[f.lease.run_id],invoke=f.c.invokeProjectRemote;
  run.memory={items:[]};run.role={id:'architect'};run.next_heartbeat=0;
  f.c.invokeProjectRemote=async(b,action,args)=>{
    if(action==='hub_heartbeat_task')throw new HubError('PROJECT_ACCESS_REVOKED');
    return invoke(b,action,args);
  };
  await f.runtime.tick();
  assert.equal(run.stopped,'PROJECT_ACCESS_REVOKED');assert.equal(run.memory,undefined);assert.equal(run.role,undefined);
  assert.equal(run.acknowledged,true);assert.ok(run.base_commit);
});

test('export cleanup requires a shared log and verifies both the exported notes and code',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'exported code');
  const saved=await f.runtime.save(f.binding,{...f.lease,summary:'export',notes:'handoff note',tests:'not run',next_steps:'review',operation_id:crypto.randomUUID()});
  await assert.rejects(f.runtime.removeExported(saved.local_checkpoint_id),{code:'WORK_EXPORT_NOT_CONFIRMED'});
  const target=path.join(f.data,'verified-export');await f.runtime.export(saved.local_checkpoint_id,target);
  const file=path.join(target,'changed-files/src/main.ts');await fs.writeFile(file,'tampered');
  await assert.rejects(f.runtime.removeExported(saved.local_checkpoint_id),{code:'WORK_EXPORT_NOT_CONFIRMED'});
  assert.ok(await f.runtime.store.read(saved.local_checkpoint_id));await fs.writeFile(file,'exported code');
  const receiptFile=path.join(target,'checkpoint.json'),receipt=await fs.readFile(receiptFile,'utf8'),edited=JSON.parse(receipt);
  edited.worklog.notes='lost original handoff';await fs.writeFile(receiptFile,JSON.stringify(edited));
  await assert.rejects(f.runtime.removeExported(saved.local_checkpoint_id),{code:'WORK_EXPORT_NOT_CONFIRMED'});
  await fs.writeFile(receiptFile,receipt);assert.equal((await f.runtime.removeExported(saved.local_checkpoint_id)).removed,true);
  assert.equal(await f.runtime.store.read(saved.local_checkpoint_id),null);
  assert.equal(await fs.readFile(file,'utf8'),'exported code');
});

test('background flush and a simultaneous model retry share one pending operation',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'queued once');f.setOffline(true);
  const args={...f.lease,summary:'queue',notes:'pending once',tests:'not run',next_steps:'review',operation_id:crypto.randomUUID()};
  await f.runtime.save(f.binding,args);f.setOffline(false);
  await Promise.all([f.runtime.tick(),f.runtime.save(f.binding,args)]);
  assert.equal(f.uploads(),1);
});

test('pre-edit checks validate the current lease and policy as well as planned and actual paths',async t=>{
  const f=await runtimeFixture(t);
  assert.equal((await f.runtime.check(f.binding,{...f.lease,proposed_paths:['src/main.ts']})).can_continue,true);
  await assert.rejects(f.runtime.check(f.binding,{...f.lease,proposed_paths:['src/frozen/old.ts']}),{code:'WORK_PATH_PROTECTED'});
  const invoke=f.c.invokeProjectRemote;
  f.c.invokeProjectRemote=async(b,action,args)=>action==='hub_get_task'?{task:{status:'in_progress',active_run_id:f.lease.run_id,
    lease_epoch:1,lease_until:new Date(Date.now()-1000).toISOString(),execution_mode:'observed',scope}}:invoke(b,action,args);
  await assert.rejects(f.runtime.check(f.binding,f.lease),{code:'TASK_LEASE_EXPIRED'});
  f.c.invokeProjectRemote=async(b,action,args)=>action==='hub_get_policy'?{...policy,content_hash:'f'.repeat(64)}:invoke(b,action,args);
  await assert.rejects(f.runtime.check(f.binding,f.lease),{code:'WORK_CONTEXT_STALE'});
});

test('shutdown drains a background capture before finishing and preserves the encrypted checkpoint',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'capture before shutdown');
  let enter,release;const entered=new Promise(r=>{enter=r;}),gate=new Promise(r=>{release=r;});
  const original=observer.capture;
  observer.capture=async(...args)=>{enter();await gate;return original(...args);};
  t.after(()=>{observer.capture=original;});
  const running=f.runtime.tick();await entered;f.c.closed=true;
  let finished=false;const draining=f.runtime.drain().then(()=>{finished=true;});
  await new Promise(r=>setTimeout(r,15));assert.equal(finished,false);release();
  await Promise.all([running,draining]);assert.equal((await f.runtime.list(f.binding)).items.length,1);
});

test('a corrupt checkpoint does not block other pending logs and metadata lists exclude code contents',async t=>{
  const f=await runtimeFixture(t);await fs.writeFile(path.join(f.dir,'src/main.ts'),'pending survives another corrupted record');f.setOffline(true);
  const saved=await f.runtime.save(f.binding,{...f.lease,summary:'valid queue',notes:'keep',tests:'not run',next_steps:'review',operation_id:crypto.randomUUID()});
  await fs.writeFile(f.runtime.store.file(crypto.randomUUID()),'invalid ciphertext');
  const index=await f.runtime.store.list(f.runtime.owner());assert.equal(index.length,1);assert.equal(index[0].snapshot.contents,undefined);
  f.setOffline(false);await f.runtime.tick();
  assert.equal((await f.runtime.store.read(saved.local_checkpoint_id)).state,'shared');assert.equal(f.uploads(),1);
  assert.equal((await f.runtime.list(f.binding)).unreadable_records,1);
});
