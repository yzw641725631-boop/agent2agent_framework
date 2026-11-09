'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),os=require('node:os'),path=require('node:path'),fs=require('node:fs/promises');
const {spawn}=require('node:child_process');
const {ExecutionRuntime,environment,fingerprint}=require('../core/execution-runtime.cjs');
const {tools,validate}=require('../core/execution-tools.cjs');
const {HubError}=require('../core/remote.cjs');
const id='610d71dd-bdbc-46fd-85c7-e18a305c01a5';
async function fixture(t,code,intervalMs=20000){
  const workspace=await fs.mkdtemp(path.join(os.tmpdir(),'hub-exec-'));t.after(()=>fs.rm(workspace,{recursive:true,force:true}));
  const binding={id,projectId:id,workspace,status:'active'};
  const profile={user:{id},deviceId:id,origin:'https://example.org',bindings:[binding]},remote={};
  const run={task_id:id,run_id:id,lease_epoch:1,instance_id:id,harness:'codex',execution_mode:'observed',context_hash:'context',policy:{content_hash:'policy'}};
  const c={profile,remote,connection:'active',closed:false,draining:false,binding:()=>binding,
    persist:async()=>{},emitState:()=>{},ensureCurrent:(p,r)=>{if(c.profile!==p || c.remote!==r)throw new HubError('LOGIN_REQUIRED');},
    work:{run:()=>run,check:async()=>({can_continue:true})},hooks:{invoke:async()=>({state:'observed'})},invokeProjectRemote:async()=>({shared:true})};
  let starts=0;
  const runtime=new ExecutionRuntime(c,{intervalMs,spawnProcess:(_exe,_args,opts)=>{starts++;return spawn(process.execPath,['-e',code],opts);}});
  t.after(()=>runtime.haltAll());
  await runtime.configure('codex',process.execPath);
  const preview=await runtime.preview(binding,{task_id:id,run_id:id,lease_epoch:1,harness:'codex',timeout_seconds:30});
  return {runtime,c,binding,preview,run,starts:()=>starts};
}
test('native execution requires a separate desktop approval, records real exit, and never saves stdout content',async t=>{
  const f=await fixture(t,"process.stdout.write('sensitive-model-text');process.stderr.write('private-code');");
  await assert.rejects(f.runtime.start(f.binding,f.preview.id),{code:'EXECUTION_DESKTOP_APPROVAL_REQUIRED'});
  assert.equal(f.starts(),0);
  await f.runtime.approve(f.preview.id,f.preview.preview_hash);
  await f.runtime.start(f.binding,f.preview.id);
  const job=[...f.runtime.active.values()][0];await job.finished;
  const [record]=f.runtime.list().items;
  assert.equal(record.state,'exited');assert.equal(record.exit_code,0);assert.equal(record.stdout_bytes,20);
  assert.ok(record.stdout_hash);assert.ok(record.receipt_shared);
  assert.ok(!JSON.stringify(record).includes('sensitive-model-text'));
  await assert.rejects(f.runtime.start(f.binding,f.preview.id),{code:'EXECUTION_APPROVAL_EXPIRED'});
});
test('authorization loss stops the actual owned process and preserves its receipt',async t=>{
  const f=await fixture(t,'setInterval(()=>{},5000);',50);
  await f.runtime.approve(f.preview.id,f.preview.preview_hash);await f.runtime.start(f.binding,f.preview.id);
  const job=[...f.runtime.active.values()][0];
  f.c.work.check=async()=>{throw new HubError('PROJECT_ACCESS_REVOKED');};
  const record=await job.finished;
  assert.equal(record.state,'stopped');assert.equal(record.reason,'PROJECT_ACCESS_REVOKED');
  assert.equal(f.runtime.active.size,0);
});
test('approval is bound to account, context, binary and fixed validated arguments',async t=>{
  const f=await fixture(t,'setInterval(()=>{},5000);');
  await f.runtime.approve(f.preview.id,f.preview.preview_hash);
  f.run.context_hash='new-context';
  await assert.rejects(f.runtime.start(f.binding,f.preview.id),{code:'EXECUTION_PREVIEW_CHANGED'});
  assert.equal(f.starts(),0);
  assert.throws(()=>validate('hub_start_execution',{approval_id:id,command:'remove-files'}),{code:'INVALID_REQUEST'});
  assert.ok(!tools([]).some(t=>t.name==='hub_start_execution'));
  process.env.HUB_SIGNING_SECRET='never-export-this';
  try{assert.ok(!JSON.stringify(environment()).includes('never-export-this'));}finally{delete process.env.HUB_SIGNING_SECRET;}
});
test('ended execution receipt retries the exact original payload after a lost response',async t=>{
  const f=await fixture(t,"process.stdout.write('done');");
  const attempts=[];f.c.invokeProjectRemote=async(_b,_name,args)=>{attempts.push(args);if(attempts.length===1)throw new HubError('NETWORK_UNAVAILABLE');return {shared:true};};
  await f.runtime.approve(f.preview.id,f.preview.preview_hash);await f.runtime.start(f.binding,f.preview.id);
  const job=[...f.runtime.active.values()][0];await job.finished;
  assert.equal(f.runtime.list().items[0].receipt_shared,false);
  await f.runtime.flush();assert.deepEqual(attempts[0],attempts[1]);
  assert.equal(f.runtime.list().items[0].receipt_shared,true);
});

test('restored unfinished processes block preview immediately and do not require waiting for the receipt timer',async t=>{
  const f=await fixture(t,'setInterval(()=>{},5000);');
  f.c.profile.executionRecords=[{id:'previous',binding_id:f.binding.id,state:'running'}];
  await assert.rejects(f.runtime.preview(f.binding,{task_id:id,run_id:id,lease_epoch:1,harness:'codex'}),
    {code:'EXECUTION_PREVIOUS_STOP_UNCONFIRMED'});
  const record=f.runtime.list().items[0];assert.equal(record.state,'interrupted');
  assert.equal(record.requires_process_check,true);const payload=structuredClone(record.receipt_payload);
  await f.runtime.confirmStopped(record.id);await f.runtime.flush();
  assert.deepEqual(record.receipt_payload,payload);assert.equal(record.receipt_shared,true);
});

test('starting reserves the workspace and shutdown cancels a start waiting for its durable record',async t=>{
  const f=await fixture(t,'setInterval(()=>{},5000);');
  await f.runtime.approve(f.preview.id,f.preview.preview_hash);
  let entered,release;const waiting=new Promise(r=>{entered=r;});
  const saved=new Promise(r=>{release=r;});let once=true;
  f.c.persist=async()=>{if(once && f.runtime.list().items[0]?.state==='starting'){once=false;entered();await saved;}};
  const starting=f.runtime.start(f.binding,f.preview.id);
  const rejected=assert.rejects(starting,{code:'EXECUTION_START_CANCELLED'});
  await waiting;
  await assert.rejects(f.runtime.preview(f.binding,{task_id:id,run_id:id,lease_epoch:1,harness:'codex'}),{code:'EXECUTION_WORKSPACE_BUSY'});
  const halting=f.runtime.haltAll('SIGNED_OUT');release();await rejected;await halting;
  assert.equal(f.starts(),0);assert.equal(f.runtime.list().items[0].state,'cancelled');
  assert.equal(f.runtime.list().items[0].reason,'SIGNED_OUT');
});

test('authorization failure may cancel its own pending start without waiting on itself',async t=>{
  const f=await fixture(t,'setInterval(()=>{},5000);');
  await f.runtime.approve(f.preview.id,f.preview.preview_hash);
  f.c.work.check=async()=>{await f.runtime.haltProject(id,'PROJECT_ACCESS_REVOKED');throw new HubError('PROJECT_ACCESS_REVOKED');};
  await assert.rejects(f.runtime.start(f.binding,f.preview.id),{code:'PROJECT_ACCESS_REVOKED'});
  assert.equal(f.starts(),0);assert.equal(f.runtime.starting.size,0);
});
test('real supervisor records native child exit and kills its owned child when the desktop pipe closes',async t=>{
  const executable=await fingerprint(process.execPath);
  async function run(code,closeAfterSpawn){
    const child=spawn(process.execPath,[path.join(__dirname,'../core/execution-supervisor.cjs')],
      {stdio:['pipe','pipe','pipe','pipe'],windowsHide:true,shell:false});
    t.after(()=>{if(child.exitCode===null)child.kill('SIGKILL');});
    let buffer='',events=[];
    child.stdout.resume();child.stderr.resume();child.stdin.on('error',()=>{});
    child.stdio[3].on('data',chunk=>{buffer+=chunk.toString();for(let at;(at=buffer.indexOf('\n'))>=0;){
      const event=JSON.parse(buffer.slice(0,at));buffer=buffer.slice(at+1);events.push(event);
      if(closeAfterSpawn && event.event==='spawned')child.stdin.end();
    }});
    const ended=new Promise(resolve=>child.once('close',(code)=>resolve(code)));
    child.stdin.write(JSON.stringify({type:'start',executable:executable.file,sha256:executable.sha256,
      args:['-e',code],cwd:os.tmpdir(),prompt:'',timeout_seconds:5})+'\n');
    let timeout;const exit=await Promise.race([ended,new Promise((_,reject)=>{timeout=setTimeout(()=>reject(Error('supervisor did not terminate')),8000);})]).finally(()=>clearTimeout(timeout));
    return {exit,events};
  }
  const normal=await run("process.stdout.write('ok');",false);
  assert.equal(normal.exit,0);assert.ok(normal.events.some(e=>e.event==='exited' && e.code===0));
  const lost=await run('setInterval(()=>{},5000);',true);
  assert.ok(lost.events.some(e=>e.event==='stop_requested' && e.reason==='DESKTOP_PIPE_CLOSED'));
  assert.ok(lost.events.some(e=>e.event==='exited'));assert.notEqual(lost.exit,0);
});
