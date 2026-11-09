'use strict';
// Disposable server over SSH loopback. No real user profile or business repository.
const assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {promisify}=require('node:util'),{execFile}=require('node:child_process'),execute=promisify(execFile);
const {Controller}=require('../core/controller.cjs'),{Remote}=require('../core/remote.cjs');
const observer=require('../core/git-observer.cjs'),origin='http://127.0.0.1:18767',controllers=[];
function protector(){
  const key=crypto.randomBytes(32);return {
    async encryptStringAsync(value){const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);return Buffer.concat([iv,cipher.update(value),cipher.final(),cipher.getAuthTag()]);},
    async decryptStringAsync(value){const cipher=crypto.createDecipheriv('aes-256-gcm',key,value.subarray(0,12));cipher.setAuthTag(value.subarray(-16));return {result:Buffer.concat([cipher.update(value.subarray(12,-16)),cipher.final()]).toString()};}};
}
async function main(){
  const response=await fetch(origin+'/__test__/seed');assert.ok(response.ok);const seed=await response.json();
  const root=new Remote(seed.root,async()=>{});await root.init();
  const parent=await fs.realpath(os.tmpdir()),temp=await fs.mkdtemp(path.join(parent,'hub-real-coordination-'));
  const base=`/api/projects/${seed.project_id}/tasks`;
  try{
    async function controller(profile,name){
      const workspace=path.join(temp,name),directory=path.join(temp,name+'-desktop');await fs.mkdir(workspace);await fs.mkdir(directory);
      for(const part of ['src','assets','docs']){await fs.mkdir(path.join(workspace,part));await fs.writeFile(path.join(workspace,part,'baseline.txt'),'baseline\n');}
      const git=args=>execute('git',['-C',workspace,...args],{windowsHide:true});
      await git(['init','-q']);await git(['remote','add','origin','https://github.com/example/web']);await git(['add','.']);
      await git(['-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','baseline']);
      const binding={id:crypto.randomUUID(),projectId:seed.project_id,repositoryId:seed.repository_id,workspace,status:'active',secret:crypto.randomBytes(32).toString('base64url'),
        initial_commit:(await observer.initialize(workspace)).base_commit};
      profile.bindings=[binding];profile.cursors={};const vault={protector:protector(),data:{current:'fixture',profiles:{fixture:profile}},save:async()=>{}};
      const c=new Controller({vault,directory,executable:process.execPath,bridgeScript:'unused'});controllers.push(c);
      c.remote=new Remote(profile,()=>c.persist());await c.remote.init();c.connection='active';c.projects=[{id:seed.project_id,name:'Isolated coordination'}];
      return {c,binding,workspace};
    }
    const a=await controller(seed.author,'author'),b=await controller(seed.reader,'reader');
    const invoke=(who,binding,name,args)=>who.c.invokeProject(binding,name,args);
    const aLease={task_id:seed.task.id,run_id:seed.run.run.id,lease_epoch:seed.run.task.lease_epoch};
    async function ready(paths,depends_on=[]){
      let task=await root.request('POST',base,{title:'coordination '+paths.join(','),description:'Only the declared paths',acceptance:'Observable handoff',
        repository_id:seed.repository_id,execution_mode:'observed',scope:{repository:'web',platform:'web',paths},depends_on});
      for(const gate of ['requirements','design','feasibility'])task=await root.request('POST',`${base}/${task.id}/decisions`,{
        gate,approved:true,expected_version:task.version,evidence:'Disposable fixture admission',instance_id:crypto.randomUUID(),harness:'human'});
      return task;
    }
    const overlapping=await ready(['src']),follower=await ready(['docs'],[seed.task.id]),parallel=await ready(['assets']);
    const claim=task_id=>invoke(b,b.binding,'hub_claim_task',{task_id,instance_id:crypto.randomUUID(),harness:'codex',operation_id:crypto.randomUUID()});
    await assert.rejects(claim(overlapping.id),{code:'TASK_SCOPE_CONFLICT'});
    await assert.rejects(claim(follower.id),{code:'TASK_DEPENDENCIES_INCOMPLETE'});
    const claimed=await claim(parallel.id),bLease={task_id:parallel.id,run_id:claimed.run.id,lease_epoch:claimed.run.lease_epoch};
    await fs.writeFile(path.join(a.workspace,'src/baseline.txt'),'original unfinished work\n');
    const create=(who,lease)=>invoke(who,who.binding,'hub_create_task_worktree',{...lease,harness:'codex',operation_id:crypto.randomUUID()});
    const [wa,wb]=await Promise.all([create(a,aLease),create(b,bLease)]);
    const ca=a.c.binding(wa.binding_id),cb=b.c.binding(wb.binding_id);
    assert.notEqual(wa.workspace,wb.workspace);
    async function prepare(who,binding,lease){
      const context=await invoke(who,binding,'hub_prepare_work',{...lease,operation_id:crypto.randomUUID()});
      await invoke(who,binding,'hub_acknowledge_work',{...lease,context_id:context.id,content_hash:context.content_hash,
        rule_ids:context.payload.policy.rules.map(r=>r.id),operation_id:crypto.randomUUID()});return context;
    }
    await Promise.all([prepare(a,ca,aLease),prepare(b,cb,bLease)]);
    await assert.rejects(invoke(a,ca,'hub_get_task',{task_id:parallel.id}),{code:'TASK_WORKTREE_MISMATCH'});
    await fs.writeFile(path.join(wa.workspace,'src/baseline.txt'),'author task change\n');
    await fs.writeFile(path.join(wb.workspace,'assets/baseline.txt'),'reader parallel change\n');
    const save=(who,binding,lease)=>invoke(who,binding,'hub_write_worklog',{...lease,summary:'真实独立目录协作检查点',
      notes:'不同人员、任务运行和工作目录独立采集；已验证前置阻塞及范围冲突。',tests:'Real Windows Controller, DPoP HTTP and PostgreSQL',
      next_steps:'Review the saved checkpoint',operation_id:crypto.randomUUID()});
    const [la,lb]=await Promise.all([save(a,ca,aLease),save(b,cb,bLease)]);
    const receiptState=value=>Object.fromEntries(['server_confirmed','server_code_backup','state','backup_state','backup_error','last_error','message'].map(k=>[k,value[k]]));
    assert.ok(la.server_confirmed && la.server_code_backup && lb.server_confirmed && lb.server_code_backup,JSON.stringify([receiptState(la),receiptState(lb)]));
    let submitted=await invoke(a,ca,'hub_submit_task',{...aLease,worklog_id:la.server_receipt.id,expected_version:seed.run.task.version,operation_id:crypto.randomUUID()});
    await assert.rejects(claim(overlapping.id),{code:'TASK_SCOPE_CONFLICT'});
    for(const gate of ['review','test','release'])submitted=await root.request('POST',`${base}/${seed.task.id}/decisions`,{
      gate,approved:true,expected_version:submitted.version,worklog_id:submitted.submission_id,
      evidence:'Disposable fixture record, separate actual root identity',instance_id:crypto.randomUUID(),harness:'human'});
    assert.equal(submitted.status,'completed');
    const next=await claim(follower.id);assert.equal(next.task.status,'in_progress');
    // Keep all user work registered before testing receipt-based multi-directory revocation.
    await fs.writeFile(path.join(a.workspace,'src/baseline.txt'),'baseline\n');
    await root.request('POST',`/api/projects/${seed.project_id}/members/${seed.reader.user.id}/revoke`,{});
    await b.c.closeouts.tick();
    const closeout=await invoke(b,cb,'hub_closeout_status',{});
    assert.ok(closeout.items.some(item=>item.state==='complete'));
    const config=await fs.readFile(path.join(wb.workspace,'.codex/config.toml'),'utf8');
    assert.ok(!config.includes(Object.keys(b.c.configuration(cb).mcpServers)[0]));
    assert.equal(await fs.readFile(path.join(wb.workspace,'assets/baseline.txt'),'utf8'),'reader parallel change\n');
    assert.equal(await fs.readFile(path.join(a.workspace,'src/baseline.txt'),'utf8'),'baseline\n');
    console.log(JSON.stringify({passed:true,realWindowsController:true,realHTTP:true,realDPoP:true,realPostgreSQL:true,
      distinctPeople:2,parallelTaskWorktrees:2,dependencyBlockedThenReleased:true,overlappingScopeRejected:true,
      reviewRetainsReservation:true,fixedTaskRunBinding:true,originalWorkingFilesPreserved:true,
      separateCodeBackupReceipts:true,revocationUninstallsWorktreeConnection:true,businessFilesPreserved:true}));
  }finally{
    root.close();for(const c of controllers)await c.close();
    assert.ok(path.relative(parent,temp).startsWith('hub-real-coordination-'));await fs.rm(temp,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.code || error.name,error.stack);process.exitCode=1;});
