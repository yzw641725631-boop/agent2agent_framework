'use strict';
// Disposable hub_test HTTP fixture only; never receives a production identity.
const assert=require('node:assert/strict'),crypto=require('node:crypto'),fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const {Controller}=require('../core/controller.cjs'),{Remote}=require('../core/remote.cjs'),host=require('../core/host-install.cjs');
const execute=promisify(execFile),origin='http://127.0.0.1:18767',controllers=[];
function protector(){const key=crypto.randomBytes(32);return {
  async encryptStringAsync(value){const iv=crypto.randomBytes(12),c=crypto.createCipheriv('aes-256-gcm',key,iv);const bytes=Buffer.concat([c.update(value,'utf8'),c.final()]);return Buffer.concat([iv,c.getAuthTag(),bytes]);},
  async decryptStringAsync(bytes){const c=crypto.createDecipheriv('aes-256-gcm',key,bytes.subarray(0,12));c.setAuthTag(bytes.subarray(12,28));return {result:Buffer.concat([c.update(bytes.subarray(28)),c.final()]).toString('utf8')};}};}
async function main(){
  const response=await fetch(origin+'/__test__/seed');assert.ok(response.ok);const seed=await response.json();assert.equal(seed.author.origin,origin);
  const base=await fs.realpath(os.tmpdir()),temporary=await fs.mkdtemp(path.join(base,'hub-real-closeout-'));
  try{
    const workspace=path.join(temporary,'author');await fs.mkdir(workspace);await fs.mkdir(path.join(workspace,'src'));await fs.mkdir(path.join(workspace,'docs'));
    const git=(...args)=>execute('git',['-C',workspace,...args],{windowsHide:true});
    await git('init','-q');await git('config','core.autocrlf','false');await git('remote','add','origin','https://github.com/example/web');
    await fs.writeFile(path.join(workspace,'src/main.ts'),'baseline\n');await fs.writeFile(path.join(workspace,'src/deleted.ts'),'delete me\n');
    await fs.writeFile(path.join(workspace,'docs/设计.md'),'需要交接的原设计\n');await git('add','.');
    await git('-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','baseline');
    async function controller(profile,workspace){
      const directory=path.join(temporary,crypto.randomUUID());await fs.mkdir(directory);
      const binding={id:crypto.randomUUID(),projectId:seed.project_id,repositoryId:seed.repository_id,workspace,status:'active',secret:crypto.randomBytes(32).toString('base64url')};
      profile.bindings=[binding];const vault={protector:protector(),data:{current:'test',profiles:{test:profile}},save:async()=>{}};
      const c=new Controller({vault,directory,executable:process.execPath,bridgeScript:'fixture-bridge'});controllers.push(c);
      c.remote=new Remote(profile,()=>c.persist());await c.remote.init();c.connection='active';return {c,binding};
    }
    const {c:author,binding}=await controller(seed.author,workspace),lease={task_id:seed.task.id,run_id:seed.run.run.id,lease_epoch:seed.run.task.lease_epoch};
    const invoke=(name,args)=>author.invokeProject(binding,name,{...lease,...args});
    const context=await invoke('hub_prepare_work',{operation_id:crypto.randomUUID()});
    await invoke('hub_acknowledge_work',{context_id:context.id,content_hash:context.content_hash,rule_ids:context.payload.policy.rules.map(r=>r.id),operation_id:crypto.randomUUID()});
    // Install into the real workspace with receipts. These untracked framework files are excluded only by verified ownership.
    const conf=author.configuration(binding).mcpServers,name=Object.keys(conf)[0];
    await host.install({workspace,hosts:['claude'],receiptDirectory:path.join(author.directory,'setup-projects',binding.id),name,entry:conf[name]});
    await host.deployFramework({workspace,receiptDirectory:path.join(author.directory,'framework-deployments',binding.id),name,files:[
      {path:'scripts/managed.cjs',bytes:Buffer.from('managed framework script')},{path:'tests/managed.test.cjs',bytes:Buffer.from('managed framework test')}]});
    await author.writeDescriptor(binding);
    // Hide only the known framework paths from normal Git observation, as assembly does.
    const exclude=path.join(workspace,'.git/info/exclude');await fs.appendFile(exclude,'\n/.mcp.json\n/.agent-hub/framework/\n');
    await fs.writeFile(path.join(workspace,'src/main.ts'),'committed change\n');await git('add','src/main.ts');
    await git('-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','local change');
    await fs.writeFile(path.join(workspace,'src/main.ts'),'unsubmitted work before revocation\n');
    author.remote.transport=async(url,options)=>{if(new URL(url).pathname.endsWith('/worklogs') && options.method==='POST')throw new Error('simulated network outage');return fetch(url,options);};
    const pending=await invoke('hub_write_worklog',{summary:'离场前待上传的人工记录',notes:'已经本机提交并继续修改；网络中断导致记录未交付。',tests:'尚未完成最终验证',next_steps:'交接给其他同事',operation_id:crypto.randomUUID()});
    assert.equal(pending.server_confirmed,false);author.cancelRecovery();
    // Additional edits after the last explicit worklog must be included in the final closeout snapshot.
    await fs.writeFile(path.join(workspace,'src/main.ts'),'final code at revocation\n');await fs.unlink(path.join(workspace,'src/deleted.ts'));
    await fs.writeFile(path.join(workspace,'src/new.bin'),Buffer.from([0,255,13,10,128]));
    const root=new Remote(seed.root,async()=>{});await root.init();
    await root.request('POST',`/api/projects/${seed.project_id}/members/${seed.author.user.id}/revoke`,{});
    author.remote.transport=fetch;await author.refresh();await author.closeouts.tick();
    const status=author.closeouts.status(binding);assert.equal(status.items[0].state,'complete',JSON.stringify(status));
    assert.equal(status.items[0].server_upload_confirmed,true);assert.ok(!JSON.stringify(status).includes('co1_'));
    assert.equal(binding.status,'uninstalled');
    await assert.rejects(author.invokeProject(binding,'hub_get_project',{}),{code:'PROJECT_CLOSEOUT_REQUIRED'});
    assert.ok(!(await fs.readFile(path.join(workspace,'.mcp.json'),'utf8')).includes(name));
    await assert.rejects(fs.stat(path.join(workspace,'.agent-hub/framework',name,'scripts/managed.cjs')),{code:'ENOENT'});
    await assert.rejects(fs.stat(path.join(workspace,'.agent-hub/framework',name,'tests/managed.test.cjs')),{code:'ENOENT'});
    const audit=await root.request('GET',`/api/projects/${seed.project_id}/closeouts`);assert.equal(audit.items[0].state,'complete');
    const task=await root.request('GET',`/api/projects/${seed.project_id}/tasks/${seed.task.id}`);assert.equal(task.task.status,'ready');assert.equal(task.task.active_run_id,null);
    const local=await author.work.store.list(author.work.owner());assert.equal(local.length,2);
    const latest=local.find(row=>row.body.summary==='撤权自动收尾检查点');assert.ok(latest?.code_receipt);
    const history=await root.request('GET',`/api/projects/${seed.project_id}/tasks/${seed.task.id}/worklogs`);
    assert.equal(history.items[0].id,latest.server_receipt.id,'default newest log must recover final work');
    const target=path.join(temporary,'reader');await fs.mkdir(target);
    const {c:reader,binding:readerBinding}=await controller(seed.reader,target);
    const restored=await reader.invokeProject(readerBinding,'hub_download_work_backup',{task_id:seed.task.id,worklog_id:latest.server_receipt.id,operation_id:crypto.randomUUID()});
    const output=await reader.work.export(restored.local_checkpoint_id,path.join(temporary,'recovered'));
    assert.equal(await fs.readFile(path.join(output.working_tree_directory,'src/main.ts'),'utf8'),'final code at revocation\n');
    assert.equal(await fs.readFile(path.join(output.working_tree_directory,'docs/设计.md'),'utf8'),'需要交接的原设计\n');
    assert.deepEqual(await fs.readFile(path.join(output.working_tree_directory,'src/new.bin')),Buffer.from([0,255,13,10,128]));
    await assert.rejects(fs.stat(path.join(output.working_tree_directory,'src/deleted.ts')),{code:'ENOENT'});
    assert.equal(await fs.readFile(path.join(workspace,'src/main.ts'),'utf8'),'final code at revocation\n');
    const restricted=new Remote(seed.restricted,async()=>{});await restricted.init();
    await assert.rejects(restricted.request('GET',`/api/projects/${seed.project_id}/tasks/${seed.task.id}/worklogs/${latest.server_receipt.id}/snapshot/content`,undefined,{slot:seed.project_id,binary:true}),{code:'TASK_NOT_FOUND'});
    console.log(JSON.stringify({passed:true,realController:true,realHTTP:true,realDPoP:true,realPostgreSQL:true,
      offlineManualLogRecovered:true,postLogFinalEditsCaptured:true,frameworkScriptsAndTestsRemoved:true,
      mcpEntryRemoved:true,normalOperationsDenied:true,leaseReleased:true,permittedColleagueRestores:true,
      otherRepositoryDenied:true,binaryAndDeletion:true,baselineChineseDocument:true,businessCodePreserved:true,
      checkpointsPreserved:true,newestServerWorklogIsFinalSnapshot:true,cleanupVerification:'client_report_only',physicalWindowsHosts:1}));
  }finally{
    await Promise.allSettled(controllers.map(c=>c.close()));
    assert.equal(path.dirname(await fs.realpath(temporary)),base);assert.match(path.basename(temporary),/^hub-real-closeout-/);
    await fs.rm(temporary,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.code || error.message);process.exitCode=1;});
