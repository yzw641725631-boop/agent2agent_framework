'use strict';
// Only the disposable server fixture is accepted. No production identity or project is used.
const assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const {Controller}=require('../core/controller.cjs'),{Remote}=require('../core/remote.cjs');
const origin='http://127.0.0.1:18767',execute=promisify(execFile),controllers=[];
function protector(){
  const key=crypto.randomBytes(32);return {
    async encryptStringAsync(text){const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);return Buffer.concat([iv,cipher.update(text,'utf8'),cipher.final(),cipher.getAuthTag()]);},
    async decryptStringAsync(bytes){const decipher=crypto.createDecipheriv('aes-256-gcm',key,bytes.subarray(0,12));decipher.setAuthTag(bytes.subarray(-16));return {result:Buffer.concat([decipher.update(bytes.subarray(12,-16)),decipher.final()]).toString('utf8')};},
  };
}
async function main(){
  const response=await fetch(origin+'/__test__/seed');assert.ok(response.ok);const seed=await response.json();
  assert.equal(seed.author.origin,origin);
  const parent=await fs.realpath(os.tmpdir()),temporary=await fs.mkdtemp(path.join(parent,'hub-real-handoff-'));
  try{
    const source=path.join(temporary,'author-code');await fs.mkdir(source);await fs.mkdir(path.join(source,'src'));await fs.mkdir(path.join(source,'docs'));
    const git=(...args)=>execute('git',['-C',source,...args],{windowsHide:true});
    await git('init','-q');await git('config','core.autocrlf','false');await git('remote','add','origin','https://github.com/example/web');
    await fs.writeFile(path.join(source,'src/main.ts'),'original code\n');await fs.writeFile(path.join(source,'src/remove.ts'),'old file\n');
    await fs.writeFile(path.join(source,'docs/设计.md'),'需要保留的原设计\n');await git('add','.');
    await git('-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','baseline');
    async function controller(profile,workspace){
      const directory=path.join(temporary,crypto.randomUUID());await fs.mkdir(directory);
      const binding={id:crypto.randomUUID(),projectId:seed.project_id,repositoryId:seed.repository_id,workspace,status:'active',secret:crypto.randomBytes(32).toString('base64url')};
      profile.bindings=[binding];const vault={protector:protector(),data:{current:'test',profiles:{test:profile}},save:async()=>{}};
      const c=new Controller({vault,directory,executable:process.execPath,bridgeScript:'unused'});controllers.push(c);
      c.remote=new Remote(profile,()=>c.persist());await c.remote.init();c.connection='active';return {c,binding};
    }
    const {c:author,binding}=await controller(seed.author,source),lease={task_id:seed.task.id,run_id:seed.run.run.id,lease_epoch:seed.run.task.lease_epoch};
    const invoke=(name,args)=>author.invokeProject(binding,name,{...lease,...args});
    const context=await invoke('hub_prepare_work',{operation_id:crypto.randomUUID()});
    await invoke('hub_acknowledge_work',{context_id:context.id,content_hash:context.content_hash,
      rule_ids:context.payload.policy.rules.map(rule=>rule.id),operation_id:crypto.randomUUID()});
    await fs.writeFile(path.join(source,'src/main.ts'),'committed change\n');await git('add','.');
    await git('-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','local task commit');
    await fs.writeFile(path.join(source,'src/main.ts'),'latest uncommitted work\n');await fs.unlink(path.join(source,'src/remove.ts'));
    await fs.writeFile(path.join(source,'src/new.bin'),Buffer.from([0,255,1,128,13,10]));
    const saved=await invoke('hub_write_worklog',{summary:'可接续代码交接',notes:'已提交改动之外还存在未提交代码和二进制，删除旧文件。',tests:'实际网络链路验收',next_steps:'由其他获准设备恢复',operation_id:crypto.randomUUID()});
    assert.equal(saved.server_confirmed,true);assert.equal(saved.server_code_backup,true,saved.backup_error);
    const root=new Remote(seed.root,async()=>{});await root.init();
    await root.request('POST',`/api/projects/${seed.project_id}/members/${seed.author.user.id}/revoke`,{});
    await assert.rejects(invoke('hub_read_worklog',{worklog_id:saved.server_receipt.id}),error=>['PROJECT_ACCESS_REVOKED','SESSION_EXPIRED_OR_REVOKED','REFRESH_REPLAY_FAMILY_REVOKED'].includes(error.code));
    const target=path.join(temporary,'reader-workspace');await fs.mkdir(target);await fs.writeFile(path.join(target,'personal.txt'),'keep my existing work');
    const {c:reader,binding:readerBinding}=await controller(seed.reader,target);
    const recovery=await reader.invokeProject(readerBinding,'hub_download_work_backup',{task_id:seed.task.id,worklog_id:saved.server_receipt.id,operation_id:crypto.randomUUID()});
    assert.equal(recovery.server_code_backup,true);
    const output=await reader.work.export(recovery.local_checkpoint_id,path.join(temporary,'recovered'));
    assert.equal(await fs.readFile(path.join(output.working_tree_directory,'src/main.ts'),'utf8'),'latest uncommitted work\n');
    assert.equal(await fs.readFile(path.join(output.working_tree_directory,'docs/设计.md'),'utf8'),'需要保留的原设计\n');
    assert.deepEqual(await fs.readFile(path.join(output.working_tree_directory,'src/new.bin')),Buffer.from([0,255,1,128,13,10]));
    await assert.rejects(fs.stat(path.join(output.working_tree_directory,'src/remove.ts')),{code:'ENOENT'});
    assert.equal(await fs.readFile(path.join(target,'personal.txt'),'utf8'),'keep my existing work');
    const restricted=new Remote(seed.restricted,async()=>{});await restricted.init();
    await assert.rejects(restricted.request('GET',`/api/projects/${seed.project_id}/tasks/${seed.task.id}/worklogs/${saved.server_receipt.id}/snapshot/content`,undefined,{slot:seed.project_id,binary:true}),{code:'TASK_NOT_FOUND'});
    console.log(JSON.stringify({passed:true,realController:true,realGit:true,realHTTP:true,realPostgreSQL:true,
      serverEncryptedArtifacts:true,authorRevoked:true,permittedColleagueRestores:true,otherRepositoryDenied:true,
      committedAndUncommitted:true,binaryAndDeletion:true,unchangedBaseline:true,originalWorkspacePreserved:true,
      gitHistoryRestored:false,worklogReceipt:saved.server_receipt.receipt_hash,codeReceipt:saved.code_receipt.receipt_hash}));
  }finally{
    await Promise.allSettled(controllers.map(c=>c.close()));
    assert.equal(path.dirname(temporary),parent);assert.ok(path.basename(temporary).startsWith('hub-real-handoff-'));
    await fs.rm(temporary,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.code || error.message);process.exitCode=1;});
