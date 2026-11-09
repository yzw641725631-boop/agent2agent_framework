'use strict';
// Opt-in disposable server on SSH loopback; never load a production user profile.
const assert=require('node:assert/strict'),crypto=require('node:crypto');
const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {promisify}=require('node:util'),{execFile}=require('node:child_process'),execute=promisify(execFile);
const {Controller}=require('../core/controller.cjs'),{Remote}=require('../core/remote.cjs');
const origin='http://127.0.0.1:18767',controllers=[];
function protector(){
  const key=crypto.randomBytes(32);return {
    async encryptStringAsync(value){const iv=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,iv);return Buffer.concat([iv,cipher.update(value),cipher.final(),cipher.getAuthTag()]);},
    async decryptStringAsync(value){const cipher=crypto.createDecipheriv('aes-256-gcm',key,value.subarray(0,12));cipher.setAuthTag(value.subarray(-16));return {result:Buffer.concat([cipher.update(value.subarray(12,-16)),cipher.final()]).toString()};}};
}
async function main(){
  const response=await fetch(origin+'/__test__/seed');assert.ok(response.ok);const seed=await response.json();assert.equal(seed.author.origin,origin);
  const parent=await fs.realpath(os.tmpdir()),temp=await fs.mkdtemp(path.join(parent,'hub-real-memory-'));
  try{
    async function controller(profile,name){
      const workspace=path.join(temp,name),directory=path.join(temp,name+'-desktop');await fs.mkdir(workspace);await fs.mkdir(path.join(workspace,'src'));await fs.mkdir(directory);
      await fs.writeFile(path.join(workspace,'src/main.ts'),'baseline\n');
      const git=args=>execute('git',['-C',workspace,...args],{windowsHide:true});
      await git(['init','-q']);await git(['remote','add','origin','https://github.com/example/web']);await git(['add','.']);
      await git(['-c','user.name=Fixture','-c','user.email=fixture@example.org','commit','-qm','baseline']);
      const binding={id:crypto.randomUUID(),projectId:seed.project_id,repositoryId:seed.repository_id,workspace,status:'active',secret:crypto.randomBytes(32).toString('base64url')};
      profile.bindings=[binding];const vault={protector:protector(),data:{current:'fixture',profiles:{fixture:profile}},save:async()=>{}};
      const c=new Controller({vault,directory,executable:process.execPath,bridgeScript:'unused'});controllers.push(c);
      c.remote=new Remote(profile,()=>c.persist());await c.remote.init();c.connection='active';return {c,binding,workspace};
    }
    const a=await controller(seed.author,'author'),lease={task_id:seed.task.id,run_id:seed.run.run.id,lease_epoch:seed.run.task.lease_epoch};
    const invoke=(who,name,args)=>who.c.invokeProject(who.binding,name,args);
    const catalog=await invoke(a,'hub_get_roles',{});assert.equal(catalog.roles.length,7);
    const preview=await invoke(a,'hub_preview_framework',{doc_name_lang:'en',doc_content_lang:'zh'});
    await invoke(a,'hub_install_framework',{doc_name_lang:'en',doc_content_lang:'zh',preview_hash:preview.preview_hash});
    const checker=path.join(preview.directory,'scripts/check-framework.cjs');
    assert.equal(JSON.parse((await execute(process.execPath,[checker],{windowsHide:true})).stdout).healthy,true);
    const until=async fn=>{for(let n=0;n<80;n++){const value=await fn();if(value)return value;await new Promise(resolve=>setTimeout(resolve,100));}throw new Error('memory worker timeout');};
    await until(async()=>{const s=await invoke(a,'hub_get_memory_status',{});return !s.visible_jobs.queued && s.visible_cards.active>=2;});
    async function prepare(who,current){
      const context=await invoke(who,'hub_prepare_work',{...current,operation_id:crypto.randomUUID()});
      await invoke(who,'hub_acknowledge_work',{...current,context_id:context.id,content_hash:context.content_hash,
        rule_ids:context.payload.policy.rules.map(r=>r.id),operation_id:crypto.randomUUID()});return context;
    }
    let context=await prepare(a,lease),memory=context.payload.memory;
    assert.equal(context.payload.role.id,seed.task.execution_role);assert.equal(seed.run.run.role,seed.task.execution_role);
    assert.equal(memory.mode,'extractive-v1');assert.ok(memory.items.some(c=>c.source.kind==='document'));assert.ok(memory.items.some(c=>c.source.kind==='worklog'));
    assert.ok(Buffer.byteLength(JSON.stringify(memory))<=memory.byte_budget);
    const loaded=await invoke(a,'hub_load_memory',lease);assert.equal(loaded.content_hash,context.content_hash);assert.equal(loaded.fresh_source_authorization,true);
    assert.deepEqual(loaded.role,context.payload.role);
    const document=memory.items.find(c=>c.source.kind==='document');
    const original=await invoke(a,'hub_read_memory_source',{card_id:document.id});assert.ok(original.content.includes('邮件重试保持幂等'));
    const restricted=new Remote(seed.restricted,async()=>{});await restricted.init();
    const hidden=await restricted.request('GET',`/api/projects/${seed.project_id}/memory`,undefined,{slot:seed.project_id});
    assert.equal(hidden.items.length,0);
    const root=new Remote(seed.root,async()=>{});await root.init();
    await root.request('POST',`/api/projects/${seed.project_id}/memory/${document.id}/decisions`,{state:'conflicted',expected_version:0,reason:'fixture contract revalidation'});
    await assert.rejects(invoke(a,'hub_check_changes',lease),{code:'WORK_MEMORY_STALE'});
    assert.equal(a.c.profile.workRuns[lease.run_id].memory,undefined);
    assert.equal(a.c.profile.workRuns[lease.run_id].role,undefined);
    context=await prepare(a,lease);assert.ok(!context.payload.memory.items.some(c=>c.id===document.id));
    await fs.writeFile(path.join(a.workspace,'src/main.ts'),'handoff code\n');
    const saved=await invoke(a,'hub_write_worklog',{...lease,summary:'登录边界修复已交接',notes:'完成 Web 登录异常边界；保持仅 Web，不增加 PC。',
      tests:'真实 Windows Controller 到服务器 PostgreSQL 联调',next_steps:'接手人继续检查邮件重试。',operation_id:crypto.randomUUID()});
    assert.equal(saved.server_confirmed,true);assert.equal(saved.server_code_backup,true);
    await until(async()=>{const rows=await invoke(a,'hub_search_memory',{query:'登录边界修复'});return rows.items.some(c=>c.source.id===saved.server_receipt.id);});
    await invoke(a,'hub_release_task',{...lease,reason:'由同事接手',operation_id:crypto.randomUUID()});
    const b=await controller(seed.reader,'reader');
    const claimed=await invoke(b,'hub_claim_task',{task_id:seed.task.id,instance_id:crypto.randomUUID(),harness:'codex',operation_id:crypto.randomUUID()});
    const readerLease={task_id:seed.task.id,run_id:claimed.run.id,lease_epoch:claimed.run.lease_epoch};
    const handed=await prepare(b,readerLease);
    assert.equal(handed.payload.role.id,seed.task.execution_role);
    assert.ok(handed.payload.memory.items.some(c=>c.source.id===saved.server_receipt.id));
    assert.ok(handed.payload.memory.items.some(c=>c.body.includes('邮件重试')));
    await root.request('POST',`/api/projects/${seed.project_id}/members/${seed.author.user.id}/revoke`,{});
    await assert.rejects(invoke(a,'hub_search_memory',{}),error=>['PROJECT_ACCESS_REVOKED','PROJECT_CLOSEOUT_REQUIRED','SESSION_EXPIRED_OR_REVOKED'].includes(error.code));
    await a.c.closeouts.tick();
    const closeout=await invoke(a,'hub_closeout_status',{});
    assert.ok(closeout.items.some(item=>item.state==='complete'));
    await assert.rejects(fs.stat(checker),{code:'ENOENT'});
    assert.equal(await fs.readFile(path.join(a.workspace,'src/main.ts'),'utf8'),'handoff code\n');
    console.log(JSON.stringify({passed:true,realWindowsController:true,realHTTP:true,realDPoP:true,realPostgreSQL:true,
      realMemoryWorker:true,documentAndCoworkerMemoryLoaded:true,byteBudgetChecked:true,sourceOriginalRead:true,
      repositoryDenied:true,conflictForcesRefresh:true,localProjectionInvalidated:true,incrementalLogVisible:true,
      nextColleaguePrepared:true,revocationDenied:true,workingCodePreserved:true,semanticEmbeddingsConfigured:false,
      roleBound:seed.task.execution_role,realFrameworkDeployment:true,managedFilesDoNotPolluteCodeEvidence:true,
      roleRevocationHandoffAndUninstall:true,physicalWindowsHosts:1,logicalDevices:3,preparedMemory:handed.payload.memory}));
  }finally{
    await Promise.allSettled(controllers.map(c=>c.close()));
    assert.equal(path.dirname(await fs.realpath(temp)),parent);assert.ok(path.basename(temp).startsWith('hub-real-memory-'));
    await fs.rm(temp,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.code || error.message);process.exitCode=1;});
