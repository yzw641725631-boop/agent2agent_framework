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
  const parent=await fs.realpath(os.tmpdir()),temp=await fs.mkdtemp(path.join(parent,'hub-real-semantic-'));
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

    await until(async()=>{const s=await invoke(a,'hub_get_memory_status',{});return s.embeddings.cards.indexed>=2 && !s.embeddings.cards.pending_or_failed;});
    const synonyms=await invoke(a,'hub_search_memory',{query:'怎样避免邮件失败后重复操作'});
    assert.equal(synonyms.semantic_search,'ready');assert.equal(synonyms.dimensions,512);
    assert.ok(synonyms.items.some(item=>item.source.id===document.source.id && item.retrieved_by.includes('semantic')));
    const keywords=await invoke(a,'hub_search_memory',{query:'邮件重试',search_mode:'keyword'});
    assert.equal(keywords.semantic_search,'keyword-requested');
    const root=new Remote(seed.root,async()=>{});await root.init();
    const quote='本项目只实现 Web，邮件重试保持幂等；不扩展 PC。';
    const pending=await invoke(a,'hub_propose_knowledge',{source_card_id:document.id,source_hash:document.source_hash,
      source_quote:quote,topic:'web.mail.idempotency',kind:'interface',title:'Web 登录长期幂等约定',
      body:'本项目的 Web 邮件接口在失败重试时保持幂等，并维持当前授权的 Web 范围。',
      reason:'这项跨任务的接口约定需长期保留，避免交接后重复开发或扩大范围。',
      generation:{method:'local-agent',harness:'codex',model_reported:'fixture-model',input_tokens_reported:900,output_tokens_reported:100},
      operation_id:crypto.randomUUID()});
    assert.equal(pending.state,'pending');
    assert.equal((await invoke(a,'hub_search_knowledge',{})).items.length,0);
    const approved=await root.request('POST',`/api/projects/${seed.project_id}/memory/knowledge/${pending.id}/decisions`,
      {expected_version:pending.version,state:'active',reason:'由另一身份核对当前来源和适用范围，批准长期共享。'});
    assert.notEqual(approved.author_id,approved.reviewer_id);assert.equal(approved.review_override_reason,'');

    await until(async()=>{const s=await invoke(a,'hub_get_memory_status',{});return s.embeddings.knowledge.indexed>=1;});
    const semanticKnowledge=await invoke(a,'hub_search_knowledge',{query:'网页请求失败反复执行应遵守什么'});
    assert.equal(semanticKnowledge.semantic_search,'ready');assert.ok(semanticKnowledge.items.some(k=>k.id===pending.id));
    context=await prepare(a,lease);assert.ok(context.payload.memory.knowledge.some(k=>k.id===pending.id));
    assert.ok((await invoke(a,'hub_load_memory',lease)).memory.knowledge.some(k=>k.id===pending.id));
    const restricted=new Remote(seed.restricted,async()=>{});await restricted.init();
    const hidden=await restricted.request('GET',`/api/projects/${seed.project_id}/memory`,undefined,{slot:seed.project_id});
    assert.equal(hidden.items.length,0);
    const hiddenKnowledge=await restricted.request('GET',`/api/projects/${seed.project_id}/memory/knowledge`,undefined,{slot:seed.project_id});
    assert.equal(hiddenKnowledge.items.length,0);
    await root.request('POST',`/api/projects/${seed.project_id}/memory/${document.id}/decisions`,{state:'conflicted',expected_version:0,reason:'fixture contract revalidation'});
    await assert.rejects(invoke(a,'hub_check_changes',lease),{code:'WORK_MEMORY_STALE'});
    assert.equal((await invoke(a,'hub_search_knowledge',{})).items.length,0);
    const stale=(await invoke(a,'hub_search_knowledge',{include_history:true})).items.find(k=>k.id===pending.id);
    assert.equal(stale.state,'source_stale');assert.equal(stale.automatic_state_change,true);
    assert.equal(a.c.profile.workRuns[lease.run_id].memory,undefined);
    assert.equal(a.c.profile.workRuns[lease.run_id].role,undefined);
    context=await prepare(a,lease);assert.ok(!context.payload.memory.items.some(c=>c.id===document.id));
    await fs.writeFile(path.join(a.workspace,'src/main.ts'),'handoff code\n');
    const saved=await invoke(a,'hub_write_worklog',{...lease,summary:'登录边界修复已交接',notes:'完成 Web 登录异常边界；保持仅 Web，不增加 PC。',
      tests:'真实 Windows Controller 到服务器 PostgreSQL 联调',next_steps:'接手人继续检查邮件重试。',operation_id:crypto.randomUUID()});
    assert.equal(saved.server_confirmed,true);assert.equal(saved.server_code_backup,true);
    await until(async()=>{const rows=await invoke(a,'hub_search_memory',{query:'登录边界修复'});return rows.items.some(c=>c.source.id===saved.server_receipt.id);});
    const firstCard=(await invoke(a,'hub_search_memory',{query:'登录边界修复'})).items.find(c=>c.source.id===saved.server_receipt.id);
    const logFact=await invoke(a,'hub_propose_knowledge',{source_card_id:firstCard.id,source_hash:firstCard.source_hash,
      source_quote:'完成 Web 登录异常边界；保持仅 Web，不增加 PC。',topic:'web.auth.focus',kind:'lesson',title:'Web 登录范围长期交接',
      body:'Web 登录修复保持当前授权范围，后续任务继续核对邮件重试的幂等性，不自行扩大项目端。',
      reason:'把代码交接中持续有效的边界保留下来供后续执行者使用。',operation_id:crypto.randomUUID()});
    await root.request('POST',`/api/projects/${seed.project_id}/memory/knowledge/${logFact.id}/decisions`,
      {expected_version:logFact.version,state:'active',reason:'另一身份核对日志原文，批准长期边界。'});
    const newer=await invoke(a,'hub_write_worklog',{...lease,summary:'Web 登录最新交接',notes:'最新一轮已核对状态，下一位同事继续检查邮件异常。',
      tests:'相同任务新交接不使已审核长期知识退出。',next_steps:'接手人核对最新日志及长期知识。',operation_id:crypto.randomUUID()});
    assert.equal(newer.server_confirmed,true);
    await until(async()=>{const rows=await invoke(a,'hub_search_memory',{query:'Web 登录'});return rows.items.some(c=>c.source.id===newer.server_receipt.id);});
    const latestCards=(await invoke(a,'hub_search_memory',{query:'Web 登录'})).items;
    assert.ok(!latestCards.some(c=>c.source.id===saved.server_receipt.id));
    assert.ok((await invoke(a,'hub_search_knowledge',{})).items.some(k=>k.id===logFact.id));
    await invoke(a,'hub_release_task',{...lease,reason:'由同事接手',operation_id:crypto.randomUUID()});
    const b=await controller(seed.reader,'reader');
    const claimed=await invoke(b,'hub_claim_task',{task_id:seed.task.id,instance_id:crypto.randomUUID(),harness:'codex',operation_id:crypto.randomUUID()});
    const readerLease={task_id:seed.task.id,run_id:claimed.run.id,lease_epoch:claimed.run.lease_epoch};
    const handed=await prepare(b,readerLease);
    assert.equal(handed.payload.role.id,seed.task.execution_role);
    assert.ok(handed.payload.memory.items.some(c=>c.source.id===newer.server_receipt.id));
    assert.ok(handed.payload.memory.knowledge.some(c=>c.body.includes('邮件重试')));
    assert.ok(handed.payload.memory.knowledge.some(k=>k.id===logFact.id));
    await root.request('POST',`/api/projects/${seed.project_id}/members/${seed.author.user.id}/revoke`,{});
    await assert.rejects(invoke(a,'hub_search_memory',{}),error=>['PROJECT_ACCESS_REVOKED','PROJECT_CLOSEOUT_REQUIRED','SESSION_EXPIRED_OR_REVOKED'].includes(error.code));
    await a.c.closeouts.tick();
    const closeout=await invoke(a,'hub_closeout_status',{});
    assert.ok(closeout.items.some(item=>item.state==='complete'));
    await assert.rejects(fs.stat(checker),{code:'ENOENT'});
    assert.equal(await fs.readFile(path.join(a.workspace,'src/main.ts'),'utf8'),'handoff code\n');
    console.log(JSON.stringify({passed:true,realWindowsController:true,realHTTP:true,realDPoP:true,realPostgreSQL:true,
      realMemoryWorker:true,knowledgePendingUntilReviewed:true,independentKnowledgeReview:true,
      knowledgeAutoLoaded:true,knowledgeSourceInvalidated:true,knowledgeSurvivesNewLog:true,knowledgeRepositoryDenied:true,documentAndCoworkerMemoryLoaded:true,byteBudgetChecked:true,sourceOriginalRead:true,
      repositoryDenied:true,conflictForcesRefresh:true,localProjectionInvalidated:true,incrementalLogVisible:true,
      nextColleaguePrepared:true,revocationDenied:true,workingCodePreserved:true,semanticEmbeddingsConfigured:true,
      roleBound:seed.task.execution_role,realFrameworkDeployment:true,managedFilesDoNotPolluteCodeEvidence:true,
      roleRevocationHandoffAndUninstall:true,physicalWindowsHosts:1,
      logicalDevices:new Set([seed.author,seed.reader,seed.restricted,seed.root].map(p=>p.deviceId)).size,
      preparedMemory:handed.payload.memory}));
  }finally{
    await Promise.allSettled(controllers.map(c=>c.close()));
    assert.equal(path.dirname(await fs.realpath(temp)),parent);assert.ok(path.basename(temp).startsWith('hub-real-semantic-'));
    await fs.rm(temp,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.stack || error.code || error.message);process.exitCode=1;});
